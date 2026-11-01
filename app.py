#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "jinja2", "python-multipart", "pyyaml", "openpyxl"]
# ///
"""Course Kit dashboard: fetch a section, draft from recipe cards, review and adjust, approve.
Runs only on this computer at http://127.0.0.1:8765"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

import llm
import updater

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
PROMPTS = ROOT / "prompts"
PROFILE = ROOT / "profile"
DRAFTS = ROOT / "drafts"
templates = Jinja2Templates(directory=str(ROOT / "templates"))
RESTART_EXIT_CODE = 75   # the .command launcher restarts the app when it sees this


def kit_version() -> str:
    p = ROOT / "VERSION"
    return p.read_text().strip() if p.exists() else "0"
app = FastAPI()

LIMITS = {"readings": 90_000, "forum": 40_000, "module": 12_000, "quiz": 6_000, "wargame": 30_000,
          "classmate": 8_000, "rubric": 8_000, "about": 3_000, "voice": 12_000}
CHIPS = [
    ("shorter", "Shorter", "Cut it by about a third. Keep the citations and the main argument."),
    ("longer", "Longer", "Expand by about a third with one more example or supporting point from the readings."),
    ("formal", "More formal", "Make the register more formal and doctrinal. Remove contractions and casual phrasing."),
    ("plain", "Plainer", "Make it plainer and more direct. Shorter sentences, fewer abstract nouns."),
    ("cite", "Add a citation", "Add one more specific citation from the readings where a claim currently has none."),
    ("pushback", "Push back harder", "Take a clearer position and disagree more directly where the material supports it."),
    ("me", "Sound more like me", "Rewrite to match the voice samples more closely in tone, rhythm, and vocabulary."),
    ("opening", "New opening", "Replace the first paragraph with a stronger, more specific opening. Keep the rest."),
]


# ---------- config / profile ----------
def load_cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text()) if CONFIG.exists() else {}


def save_cfg(cfg: dict):
    CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def course_dir(cfg: dict) -> Path:
    return (ROOT / cfg.get("out_dir", "./course")).resolve()


def profile_text(name: str) -> str:
    p = PROFILE / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------- manifest ----------
def load_manifest(cfg: dict) -> dict:
    p = course_dir(cfg) / "manifest.json"
    if not p.exists():
        return {"sections": []}
    return json.loads(p.read_text(encoding="utf-8"))


def walk(recs: list[dict]):
    for r in recs:
        yield r
        yield from walk(r.get("children", []))


def index_by_cmid(man: dict) -> dict[int, dict]:
    idx = {}
    for s in man.get("sections", []):
        for r in walk(s.get("modules", [])):
            if r.get("cmid") is not None and "duplicate_of" not in r:
                idx[r["cmid"]] = r
    return idx


def read_md(cfg: dict, rel: str | None) -> str:
    if not rel:
        return ""
    p = course_dir(cfg) / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


def module_bundle(cfg: dict, man: dict, section: int, cmid: int) -> dict | None:
    """Everything a module page reaches: its own text, readings (books/pages), forum, quiz, files."""
    idx = index_by_cmid(man)
    top_level = {r.get("cmid") for s in man["sections"] for r in s.get("modules", [])}
    sec = next((s for s in man["sections"] if s["number"] == section), None)
    if not sec:
        return None
    mod = next((r for r in sec["modules"] if r.get("cmid") == cmid), None) or idx.get(cmid)
    if not mod:
        return None
    mod = idx.get(mod["duplicate_of"], mod) if "duplicate_of" in mod else mod
    bundle = {"section": sec, "module": mod, "readings": [], "forums": [], "quizzes": [], "files": [], "pages": []}

    def resolve(r):
        return idx.get(r["duplicate_of"], r) if "duplicate_of" in r else r

    def collect(r, depth=0):
        for raw in r.get("children", []):
            c = resolve(raw)
            t = c.get("type")
            if t == "page" and c.get("cmid") in top_level and c.get("cmid") != cmid:
                continue   # a neighbouring module, not part of this one
            if t == "book":
                ch = str(raw.get("chapterid") or "")
                chapters = c.get("chapters") or {}
                if ch and ch in chapters:
                    bundle["readings"].append({"name": raw.get("name") or c["name"], "md": chapters[ch], "cmid": c["cmid"], "chapterid": ch})
                elif not any(x.get("cmid") == c["cmid"] and not x.get("chapterid") for x in bundle["readings"]):
                    bundle["readings"].append({"name": c["name"], "md": c.get("md"), "cmid": c["cmid"], "chapterid": None})
                continue
            elif t == "forum":
                bundle["forums"].append(c)
            elif t == "quiz":
                bundle["quizzes"].append(c)
            elif t == "page":
                bundle["pages"].append(c)
            for f in c.get("files", []):
                bundle["files"].append(f)
            if depth < 3:
                collect(c, depth + 1)
    for f in mod.get("files", []):
        bundle["files"].append(f)
    collect(mod)
    bundle["files"] = list(dict.fromkeys(bundle["files"]))
    seen, uniq = set(), []
    for r in bundle["readings"]:
        k = (r["cmid"], r["chapterid"])
        if k not in seen:
            seen.add(k); uniq.append(r)
    bundle["readings"] = uniq
    return bundle


def own_posts(cfg: dict, man: dict, limit: int = 4) -> list[dict]:
    """The student's own forum posts across the course, longest first: ready-made voice samples."""
    me = (cfg.get("student_name") or "").strip().lower()
    if not me:
        return []
    out = []
    for r in index_by_cmid(man).values():
        if r.get("type") == "forum":
            for p in parse_forum_posts(read_md(cfg, r.get("md"))):
                if p["author"].strip().lower() == me and len(p["body"]) > 200:
                    out.append(p)
    out.sort(key=lambda p: len(p["body"]), reverse=True)
    return out[:limit]


def voice_text(cfg: dict, man: dict) -> str:
    pasted = profile_text("voice")
    if pasted.strip() and not pasted.startswith("Paste two or three"):
        return pasted
    mine = own_posts(cfg, man)
    if mine:
        return "\n\n---\n\n".join(p["body"] for p in mine)
    return ""


def announcements(cfg: dict, man: dict) -> dict:
    """The instructor's Announcements forum: its posts and attachments (the weekly baseline lives there)."""
    idx = index_by_cmid(man)
    forum = next((r for r in idx.values() if r.get("type") == "forum" and "announcement" in r.get("name", "").lower()), None)
    if not forum:
        return {"forum": None, "section": None, "posts": [], "files": [], "baseline": None}
    section = next((s["number"] for s in man["sections"] for r in walk(s["modules"]) if r is forum), None)
    files = [course_dir(cfg) / f for f in forum.get("files", [])]
    xl = sorted((f for f in files if f.suffix.lower() in (".xlsx", ".xlsm") and f.exists()),
                key=lambda f: f.stat().st_mtime, reverse=True)
    return {"forum": forum, "section": section, "posts": parse_forum_posts(read_md(cfg, forum.get("md"))),
            "files": files, "baseline": xl[0] if xl else None}


def parse_forum_posts(md: str) -> list[dict]:
    posts, current = [], None
    for line in md.splitlines():
        m = re.match(r"^### (.*)$", line)
        if m:
            current = {"subject": m.group(1).strip(), "author": "", "time": "", "body": []}
            posts.append(current)
            continue
        if current is None:
            continue
        m = re.match(r"^\*\*(.+?)\*\* · (.*)$", line)
        if m and not current["author"]:
            current["author"], current["time"] = m.group(1), m.group(2)
            continue
        current["body"].append(line)
    for p in posts:
        p["body"] = "\n".join(p["body"]).strip()
    return [p for p in posts if p["body"] or p["subject"]]


def xlsx_dump(path: Path, max_rows=80, max_cols=26) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        return f"(could not read {path.name}: {e})"
    out = [f"FILE {path.name}"]
    for ws in wb.worksheets:
        out.append(f"SHEET {ws.title}")
        for row in ws.iter_rows(min_row=1, max_row=max_rows, max_col=max_cols):
            cells = [f"{c.coordinate}={c.value!r}" for c in row if c.value not in (None, "")]
            if cells:
                out.append("  " + " | ".join(cells))
    return "\n".join(out)


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n\n[... trimmed {len(text) - n} characters ...]"


# ---------- prompts / drafts ----------
def load_card(name: str) -> dict:
    raw = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    meta = yaml.safe_load(m.group(1)) if m else {}
    return {"name": name, "meta": meta, "body": (m.group(2) if m else raw).strip()}


def list_cards() -> list[dict]:
    return [load_card(p.stem) for p in sorted(PROMPTS.glob("*.md"))]


def fill(body: str, slots: dict) -> str:
    def rep(m):
        return slots.get(m.group(1), f"(no {m.group(1)} available)")
    return re.sub(r"\{\{(\w+)\}\}", rep, body)


def build_slots(cfg: dict, bundle: dict, classmate: dict | None = None) -> dict:
    readings = "\n\n".join(f"=== {r['name']} ===\n{read_md(cfg, r.get('md'))}" for r in bundle["readings"] + bundle["pages"])
    forum = "\n\n".join(read_md(cfg, f.get("md")) for f in bundle["forums"])
    quiz = "\n\n".join(read_md(cfg, q.get("md")) for q in bundle["quizzes"])
    xl = [course_dir(cfg) / f for f in bundle["files"] if f.lower().endswith((".xlsx", ".xlsm"))]
    ann = announcements(cfg, load_manifest(cfg))
    wargame_parts = []
    if ann["baseline"]:
        wargame_parts.append("=== INSTRUCTOR BASELINE (newest attachment in Announcements; build on THIS) ===\n" + xlsx_dump(ann["baseline"]))
    for pth in xl:
        if pth != ann["baseline"]:
            wargame_parts.append(f"=== {pth.name} (from the module) ===\n" + xlsx_dump(pth))
    ann_text = "\n\n".join(f"**{p['author']}** · {p['time']}\n{p['subject']}\n\n{p['body']}" for p in ann["posts"][-5:])
    return {
        "announcements": clip(ann_text, LIMITS["forum"]),
        "module": clip(read_md(cfg, bundle["module"].get("md")), LIMITS["module"]),
        "readings": clip(readings, LIMITS["readings"]),
        "forum": clip(forum, LIMITS["forum"]),
        "quiz": clip(quiz, LIMITS["quiz"]),
        "wargame": clip("\n\n".join(wargame_parts), LIMITS["wargame"]) if wargame_parts else "",
        "classmate": clip(f"**{classmate['author']}**\n\n{classmate['body']}", LIMITS["classmate"]) if classmate else "",
        "rubric": clip(profile_text("rubric"), LIMITS["rubric"]),
        "about": clip(profile_text("about"), LIMITS["about"]),
        "voice": clip(voice_text(cfg, load_manifest(cfg)), LIMITS["voice"]),
        "length": cfg.get("length", "250-350 words"),
    }


def draft_path(did: str) -> Path:
    return DRAFTS / f"{did}.json"


def load_draft(did: str) -> dict:
    return json.loads(draft_path(did).read_text(encoding="utf-8"))


def save_draft(d: dict):
    DRAFTS.mkdir(exist_ok=True)
    draft_path(d["id"]).write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def list_drafts() -> list[dict]:
    DRAFTS.mkdir(exist_ok=True)
    ds = [load_draft(p.stem) for p in DRAFTS.glob("*.json")]
    return sorted(ds, key=lambda d: d["updated"], reverse=True)


SYSTEM = "You draft course writing for a student who will review and edit it. Return only the requested text, no commentary."


def new_draft(cfg: dict, card: dict, bundle: dict, classmate: dict | None) -> dict:
    slots = build_slots(cfg, bundle, classmate)
    prompt = fill(card["body"], slots)
    text = llm.chat(cfg.get("model", {}), SYSTEM, prompt)
    now = time.strftime("%Y-%m-%d %H:%M")
    d = {"id": uuid.uuid4().hex[:10], "card": card["name"], "title": card["meta"].get("title", card["name"]),
         "section": bundle["section"]["number"], "section_name": bundle["section"]["name"],
         "cmid": bundle["module"]["cmid"], "module": bundle["module"]["name"],
         "classmate": classmate["author"] if classmate else None, "status": "draft",
         "created": now, "updated": now,
         "versions": [{"text": text, "note": "first draft", "ts": now}]}
    save_draft(d)
    return d


def adjust_draft(cfg: dict, d: dict, instruction: str, current_text: str) -> dict:
    slots = {"voice": clip(voice_text(cfg, load_manifest(cfg)), LIMITS["voice"]), "rubric": clip(profile_text("rubric"), LIMITS["rubric"])}
    prompt = (f"Revise the draft below according to this instruction: {instruction}\n\n"
              f"Keep everything the instruction does not touch. Return only the revised draft.\n\n"
              f"Student's voice samples:\n{slots['voice']}\n\nRubric:\n{slots['rubric']}\n\n"
              f"DRAFT:\n{current_text}")
    text = llm.chat(cfg.get("model", {}), SYSTEM, prompt)
    now = time.strftime("%Y-%m-%d %H:%M")
    d["versions"].append({"text": text, "note": instruction, "ts": now})
    d["updated"] = now
    save_draft(d)
    return d


# ---------- fetch job ----------
class FetchJob:
    def __init__(self):
        self.lines: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, section: str | None):
        if self.running():
            return
        cmd = ["uv", "run", str(ROOT / "fetch_moodle.py")]
        if section:
            cmd += ["--section", str(int(section))]
        self.lines = [f"$ {' '.join(cmd)}"]
        self.proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            with self.lock:
                self.lines.append(line.rstrip())
        with self.lock:
            self.lines.append(f"[finished with code {self.proc.wait()}]")


fetch_job = FetchJob()


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    cfg = load_cfg()
    man = load_manifest(cfg)
    model = llm.resolve(cfg.get("model", {}))
    ann = announcements(cfg, man)
    return templates.TemplateResponse(request, "home.html", {
        "cfg": cfg, "man": man, "drafts": list_drafts()[:12], "model": model, "ann": ann,
        "version": kit_version(), "update_cfg": cfg.get("update", {}) or {},
        "has_key": bool(model["api_key"]) or model["provider"] == "custom", "fetch_running": fetch_job.running()})


@app.post("/fetch")
def fetch(section: str = Form("")):
    fetch_job.start(section.strip() or None)
    return RedirectResponse("/", status_code=303)


@app.get("/fetch/log")
def fetch_log():
    with fetch_job.lock:
        return JSONResponse({"running": fetch_job.running(), "lines": fetch_job.lines[-200:]})


@app.get("/module/{section}/{cmid}", response_class=HTMLResponse)
def module(request: Request, section: int, cmid: int):
    cfg = load_cfg()
    man = load_manifest(cfg)
    b = module_bundle(cfg, man, section, cmid)
    if not b:
        return HTMLResponse("Not found", status_code=404)
    posts = []
    for f in b["forums"]:
        posts += parse_forum_posts(read_md(cfg, f.get("md")))
    prompt_authors = {posts[0]["author"]} if posts else set()
    me = (cfg.get("student_name") or "").strip().lower()
    classmates = [p for p in posts[1:] if p["author"] not in prompt_authors and p["author"].strip().lower() != me]
    drafts = [d for d in list_drafts() if d["cmid"] == cmid]
    ann = announcements(cfg, man)
    return templates.TemplateResponse(request, "module.html", {
        "cfg": cfg, "b": b, "posts": posts, "classmates": classmates, "cards": list_cards(), "drafts": drafts,
        "baseline": ann["baseline"].name if ann["baseline"] else None,
        "has_xlsx": bool(ann["baseline"]) or any(f.lower().endswith((".xlsx", ".xlsm")) for f in b["files"])})


@app.post("/draft")
def make_draft(section: int = Form(...), cmid: int = Form(...), card: str = Form(...), classmate: str = Form("")):
    cfg = load_cfg()
    man = load_manifest(cfg)
    b = module_bundle(cfg, man, section, cmid)
    cm = None
    if classmate:
        posts = []
        for f in b["forums"]:
            posts += parse_forum_posts(read_md(cfg, f.get("md")))
        cm = next((p for p in posts if p["author"] == classmate), None)
    try:
        d = new_draft(cfg, load_card(card), b, cm)
    except llm.LLMError as e:
        return HTMLResponse(f"<p style='font-family:sans-serif;padding:2rem'>{e}<br><br><a href='/settings'>Open Settings</a> · <a href='javascript:history.back()'>Back</a></p>", status_code=502)
    return RedirectResponse(f"/draft/{d['id']}", status_code=303)


@app.get("/draft/{did}", response_class=HTMLResponse)
def draft(request: Request, did: str):
    d = load_draft(did)
    return templates.TemplateResponse(request, "draft.html", {"d": d, "chips": CHIPS, "latest": d["versions"][-1]})


@app.post("/draft/{did}/adjust")
def adjust(did: str, instruction: str = Form(""), chip: str = Form(""), text: str = Form("")):
    cfg = load_cfg()
    d = load_draft(did)
    if text.strip() and text.strip() != d["versions"][-1]["text"].strip():
        now = time.strftime("%Y-%m-%d %H:%M")
        d["versions"].append({"text": text.strip(), "note": "edited by hand", "ts": now})
        d["updated"] = now
        save_draft(d)
    instr = next((c[2] for c in CHIPS if c[0] == chip), "") or instruction.strip()
    if instr:
        try:
            adjust_draft(cfg, d, instr, d["versions"][-1]["text"])
        except llm.LLMError as e:
            return HTMLResponse(f"<p style='font-family:sans-serif;padding:2rem'>{e}<br><br><a href='/draft/{did}'>Back</a></p>", status_code=502)
    return RedirectResponse(f"/draft/{did}", status_code=303)


@app.post("/draft/{did}/revert")
def revert(did: str, version: int = Form(...)):
    d = load_draft(did)
    v = d["versions"][version]
    now = time.strftime("%Y-%m-%d %H:%M")
    d["versions"].append({"text": v["text"], "note": f"restored version {version + 1}", "ts": now})
    d["updated"] = now
    save_draft(d)
    return RedirectResponse(f"/draft/{did}", status_code=303)


@app.post("/draft/{did}/approve")
def approve(did: str, text: str = Form(...)):
    cfg = load_cfg()
    d = load_draft(did)
    now = time.strftime("%Y-%m-%d %H:%M")
    if text.strip() != d["versions"][-1]["text"].strip():
        d["versions"].append({"text": text.strip(), "note": "final edit", "ts": now})
    d["status"] = "approved"
    d["updated"] = now
    save_draft(d)
    sec = next((s for s in load_manifest(cfg)["sections"] if s["number"] == d["section"]), None)
    outdir = course_dir(cfg) / (sec["dir"] if sec else f"section-{d['section']:02d}") / "outbox"
    outdir.mkdir(parents=True, exist_ok=True)
    fname = f"{d['card']}-{d['cmid']}-{time.strftime('%Y%m%d-%H%M')}.md"
    (outdir / fname).write_text(d["versions"][-1]["text"], encoding="utf-8")
    d["outbox"] = str((outdir / fname).relative_to(course_dir(cfg)))
    save_draft(d)
    return RedirectResponse(f"/draft/{did}", status_code=303)


@app.get("/draft/{did}/text", response_class=PlainTextResponse)
def draft_text(did: str):
    return load_draft(did)["versions"][-1]["text"]


@app.get("/update/check")
def update_check():
    cfg = load_cfg()
    u = cfg.get("update", {}) or {}
    try:
        return JSONResponse({"ok": True, **updater.check(u.get("repo", ""), u.get("token", ""), kit_version())})
    except updater.UpdateError as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/update/apply")
def update_apply():
    cfg = load_cfg()
    u = cfg.get("update", {}) or {}
    try:
        res = updater.apply(u.get("repo", ""), u.get("token", ""), ROOT)
    except updater.UpdateError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    threading.Timer(1.5, lambda: os._exit(RESTART_EXIT_CODE)).start()
    return JSONResponse({"ok": True, **res})


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    cfg = load_cfg()
    model = cfg.get("model", {})
    man = load_manifest(cfg)
    authors = sorted({p["author"] for r in index_by_cmid(man).values() if r.get("type") == "forum"
                      for p in parse_forum_posts(read_md(cfg, r.get("md"))) if p["author"]})
    return templates.TemplateResponse(request, "settings.html", {
        "cfg": cfg, "model": model, "presets": llm.PRESETS, "resolved": llm.resolve(model),
        "authors": authors, "own_count": len(own_posts(cfg, man, limit=99)),
        "about": profile_text("about"), "voice": profile_text("voice"), "rubric": profile_text("rubric")})


@app.post("/settings")
def save_settings(provider: str = Form("gemini"), api_key: str = Form(""), name: str = Form(""), base_url: str = Form(""),
                  length: str = Form("250-350 words"), course_url: str = Form(""), student_name: str = Form(""),
                  update_repo: str = Form(""), update_token: str = Form(""),
                  about: str = Form(""), voice: str = Form(""), rubric: str = Form("")):
    cfg = load_cfg()
    m = cfg.get("model", {}) or {}
    m["provider"] = provider
    if api_key.strip():
        m["api_key"] = api_key.strip()
    m["name"] = name.strip() or llm.PRESETS.get(provider, {}).get("name", "")
    m["base_url"] = base_url.strip()
    cfg["model"] = m
    cfg["length"] = length.strip() or "250-350 words"
    cfg["student_name"] = student_name.strip()
    up = cfg.get("update", {}) or {}
    if update_repo.strip():
        up["repo"] = update_repo.strip()
    if update_token.strip():
        up["token"] = update_token.strip()
    cfg["update"] = up
    if course_url.strip():
        cfg["course_url"] = course_url.strip()
    save_cfg(cfg)
    PROFILE.mkdir(exist_ok=True)
    for k, v in (("about", about), ("voice", voice), ("rubric", rubric)):
        (PROFILE / f"{k}.md").write_text(v, encoding="utf-8")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test")
def test_model():
    cfg = load_cfg()
    try:
        reply = llm.chat(cfg.get("model", {}), "Reply with the single word OK.", "Ping.", temperature=0)
        return JSONResponse({"ok": True, "reply": reply[:80]})
    except llm.LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)})


def main():
    import uvicorn
    port = 8765
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
