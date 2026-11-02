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
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import quote, urlparse

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
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

PORT = 8765
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """The dashboard holds an API key and can start a browser, so only pages served from this same local
    address may talk to it. A web page in another tab cannot post to it (no cross-site form posts), and a
    DNS-rebinding trick that points some other name at 127.0.0.1 is refused by the Host check."""
    host = (request.headers.get("host") or "").strip()
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.startswith("[") and "]:" in host else host
    if hostname.lower() not in LOCAL_HOSTS:
        return PlainTextResponse(f"Course Kit only answers at http://127.0.0.1:{PORT}", status_code=421)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and (urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            return PlainTextResponse("Refused: request came from another website.", status_code=403)
        if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
            return PlainTextResponse("Refused: request came from another website.", status_code=403)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def show_http_error(request: Request, exc: HTTPException):
    import html as _h
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><body style='font-family:-apple-system,sans-serif;padding:2rem'>"
                        f"<h2>{exc.status_code}</h2><p>{_h.escape(str(exc.detail))}</p><p><a href='/'>Back to the dashboard</a></p></body>",
                        status_code=exc.status_code)


@app.exception_handler(Exception)
async def show_error(request: Request, exc: Exception):
    import traceback, html as _h
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    body = (f"<!doctype html><meta charset='utf-8'><body style='font-family:-apple-system,sans-serif;padding:2rem;max-width:900px'>"
            f"<h2>Course Kit hit an error</h2><p>Send Kaden a screenshot of this page.</p>"
            f"<p><b>Version</b> {kit_version()} · <b>Folder</b> <code>{_h.escape(str(ROOT))}</code> · <b>Page</b> <code>{_h.escape(str(request.url.path))}</code></p>"
            f"<p><b>{_h.escape(type(exc).__name__)}</b>: {_h.escape(str(exc))}</p>"
            f"<pre style='background:#111;color:#eee;padding:12px;border-radius:8px;white-space:pre-wrap;font-size:12px'>{_h.escape(tb)}</pre>"
            f"<p><a href='/'>Back to the dashboard</a></p></body>")
    print(tb, file=sys.stderr)
    return HTMLResponse(body, status_code=500)

LIMITS = {"readings": 90_000, "forum": 40_000, "module": 12_000, "quiz": 6_000, "wargame": 30_000,
          "classmate": 8_000, "rubric": 8_000, "about": 3_000, "voice": 12_000, "announcements": 40_000}
ADJUST_LIMIT = 50_000   # per slot of source material sent along with a revision request
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
def ensure_config():
    """First run: create config.yaml from config.example.yaml. Release zips never include config.yaml,
    so unzipping a new build over the folder can never wipe the saved key or settings."""
    example = ROOT / "config.example.yaml"
    if not CONFIG.exists() and example.exists():
        CONFIG.write_text(example.read_text())


def load_cfg() -> dict:
    ensure_config()
    cfg = (yaml.safe_load(CONFIG.read_text()) if CONFIG.exists() else {}) or {}
    # settings added in later versions fall back to the example config, so an older config.yaml still works
    example = ROOT / "config.example.yaml"
    if example.exists():
        try:
            defaults = yaml.safe_load(example.read_text()) or {}
        except Exception:
            defaults = {}
        for key, val in defaults.items():
            if key not in cfg or cfg[key] in (None, ""):
                cfg[key] = val
            elif isinstance(val, dict) and isinstance(cfg[key], dict):
                for k2, v2 in val.items():
                    if k2 not in cfg[key] or cfg[key][k2] in (None, ""):
                        cfg[key][k2] = v2
    return cfg


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
    try:
        man = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"sections": [], "error": f"manifest.json could not be read ({e.__class__.__name__}); run a fetch again."}
    man.setdefault("sections", [])
    for sec in man["sections"]:
        sec.setdefault("modules", []); sec.setdefault("files", []); sec.setdefault("name", f"Section {sec.get('number')}")
    return man


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
    # a forum, quiz or book listed directly in the section is its own module page: it is the thing to draft from
    if mod.get("type") == "forum":
        bundle["forums"].append(mod)
    elif mod.get("type") == "quiz":
        bundle["quizzes"].append(mod)
    elif mod.get("type") == "book":
        bundle["readings"].append({"name": mod["name"], "md": mod.get("md"), "cmid": mod.get("cmid"), "chapterid": None})

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


def norm(text: str) -> str:
    """Browsers submit textarea content with CRLF line endings; drafts are stored with LF. Compare and save
    the LF form, or every chip click would look like a hand edit."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def draft_path(did: str) -> Path:
    return DRAFTS / f"{did}.json"


def load_draft(did: str) -> dict:
    """The draft, or a 404 page for an id that is not on disk (an old bookmark, a deleted draft)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", did or "") or not draft_path(did).exists():
        raise HTTPException(404, "That draft does not exist any more.")
    try:
        d = json.loads(draft_path(did).read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"drafts/{did}.json could not be read ({e.__class__.__name__}). Send Kaden that file.")
    d.setdefault("versions", []) or d["versions"].append({"text": "", "note": "empty", "ts": d.get("updated", "")})
    d.setdefault("status", "draft"); d.setdefault("updated", d.get("created", "")); d.setdefault("module", "")
    return d


def save_draft(d: dict):
    DRAFTS.mkdir(exist_ok=True)
    draft_path(d["id"]).write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def list_drafts() -> list[dict]:
    """Every draft, newest first. One unreadable file is skipped (and reported on stderr), not fatal."""
    DRAFTS.mkdir(exist_ok=True)
    ds = []
    for p in DRAFTS.glob("*.json"):
        try:
            ds.append(load_draft(p.stem))
        except Exception as e:
            print(f"skipping unreadable draft {p.name}: {e}", file=sys.stderr)
    return sorted(ds, key=lambda d: d.get("updated", ""), reverse=True)


def card_exists(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]+", name or "")) and (PROMPTS / f"{name}.md").exists()


def forum_posts(cfg: dict, bundle: dict) -> list[dict]:
    posts = []
    for f in bundle["forums"]:
        posts += parse_forum_posts(read_md(cfg, f.get("md")))
    return posts


def pick_classmate(posts: list[dict], post_index: str = "", author: str = "") -> dict | None:
    """The post to reply to: by index into the thread when the module page sent one, otherwise the longest
    post by that author (older links and saved drafts only know the name)."""
    if post_index.strip().isdigit() and int(post_index) < len(posts):
        return posts[int(post_index)]
    mine = [p for p in posts if author and p["author"] == author]
    return max(mine, key=lambda p: len(p["body"]), default=None)


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


def adjust_context(cfg: dict, d: dict) -> str:
    """The source material the draft was written from, so a revision ("add a citation") can quote the real
    readings instead of inventing one. Each recipe card lists what it needs; only those slots are sent."""
    try:
        needs = (load_card(d["card"])["meta"] or {}).get("needs") or []
    except Exception:
        needs = []
    if not needs or d.get("cmid") is None:
        return ""
    man = load_manifest(cfg)
    b = module_bundle(cfg, man, d.get("section"), d["cmid"])
    if not b:
        return ""
    cm = pick_classmate(forum_posts(cfg, b), author=d.get("classmate") or "") if d.get("classmate") else None
    slots = build_slots(cfg, b, cm)
    if d.get("classmate") and "classmate" not in needs:
        needs = list(needs) + ["classmate"]
    parts = [f"=== {n.upper()} ===\n{clip(slots[n], ADJUST_LIMIT)}" for n in needs if slots.get(n)]
    return "\n\n".join(parts)


def adjust_draft(cfg: dict, d: dict, instruction: str, current_text: str) -> dict:
    slots = {"voice": clip(voice_text(cfg, load_manifest(cfg)), LIMITS["voice"]), "rubric": clip(profile_text("rubric"), LIMITS["rubric"])}
    context = adjust_context(cfg, d)
    prompt = (f"Revise the draft below according to this instruction: {instruction}\n\n"
              f"Keep everything the instruction does not touch. Return only the revised draft.\n\n"
              f"Student's voice samples:\n{slots['voice']}\n\nRubric:\n{slots['rubric']}\n\n"
              + (f"Source material the draft was written from. Ground any new claim or citation in this; never invent a source:\n{context}\n\n" if context else "")
              + f"DRAFT:\n{current_text}")
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
        home = str(Path.home())
        env = dict(os.environ)
        env["PATH"] = f"{home}/.local/bin:{home}/.cargo/bin:" + env.get("PATH", "")
        uv = shutil.which("uv", path=env["PATH"])
        cmd = [uv, "run", str(ROOT / "fetch_moodle.py")] if uv else [sys.executable, str(ROOT / "fetch_moodle.py")]
        if section:
            cmd += ["--section", str(int(section))]
        with self.lock:
            self.lines = [f"$ {' '.join(cmd)}"]
            if not uv:
                self.lines.append("(uv is not installed; using the same Python that runs the dashboard)")
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            with self.lock:
                self.lines.append(f"[could not start the fetcher: {e}]")
            self.proc = None
            return
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen):
        for line in proc.stdout:
            with self.lock:
                self.lines.append(line.rstrip())
        code = proc.wait()
        with self.lock:
            self.lines.append(f"[finished with code {code}]" if code == 0 else
                              f"[finished with code {code}. Something went wrong; the lines above say what. "
                              f"Anything fetched before the error is saved.]")


fetch_job = FetchJob()


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    cfg = load_cfg()
    man = load_manifest(cfg)
    model = llm.resolve(cfg.get("model", {}))
    ann = announcements(cfg, man)
    drafts = list_drafts()
    return templates.TemplateResponse(request, "home.html", {
        "cfg": cfg, "man": man, "drafts": drafts[:12], "draft_count": len(drafts), "model": model, "ann": ann,
        "version": kit_version(), "update_cfg": cfg.get("update", {}) or {},
        "has_key": bool(model["api_key"]) or model["provider"] == "custom", "fetch_running": fetch_job.running(),
        "has_course_url": bool((cfg.get("course_url") or "").strip())})


@app.post("/fetch")
def fetch(section: str = Form("")):
    s = section.strip()
    if s and not s.isdigit():
        return RedirectResponse("/?err=" + quote("The section must be a number, like 7. Leave it blank for the whole course."), status_code=303)
    fetch_job.start(s or None)
    return RedirectResponse("/", status_code=303)


@app.get("/drafts", response_class=HTMLResponse)
def drafts_page(request: Request):
    return templates.TemplateResponse(request, "drafts.html", {"drafts": list_drafts()})


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
    posts = forum_posts(cfg, b)
    prompt_authors = {posts[0]["author"]} if posts else set()   # the instructor opened the thread
    me = (cfg.get("student_name") or "").strip().lower()
    classmates = [{"idx": i, **p} for i, p in enumerate(posts)
                  if i > 0 and p["author"] not in prompt_authors and p["author"].strip().lower() != me and p["body"].strip()]
    drafts = [d for d in list_drafts() if d.get("cmid") == cmid]
    ann = announcements(cfg, man)
    return templates.TemplateResponse(request, "module.html", {
        "cfg": cfg, "b": b, "posts": posts, "classmates": classmates, "cards": list_cards(), "drafts": drafts,
        "baseline": ann["baseline"].name if ann["baseline"] else None,
        "has_xlsx": bool(ann["baseline"]) or any(f.lower().endswith((".xlsx", ".xlsm")) for f in b["files"])})


@app.post("/draft")
def make_draft(section: int = Form(...), cmid: int = Form(...), card: str = Form(...), classmate: str = Form(""), post: str = Form("")):
    cfg = load_cfg()
    man = load_manifest(cfg)
    b = module_bundle(cfg, man, section, cmid)
    if not b:
        raise HTTPException(404, "That module is not in the fetched course any more. Fetch the section again.")
    if not card_exists(card):
        raise HTTPException(404, f"No recipe card named {card!r} in the prompts folder.")
    cm = pick_classmate(forum_posts(cfg, b), post, classmate) if (classmate or post) else None
    if card == "reply" and not cm:
        raise HTTPException(404, "Pick a classmate's post to reply to.")
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
    text = norm(text)
    if text and text != norm(d["versions"][-1]["text"]):
        now = time.strftime("%Y-%m-%d %H:%M")
        d["versions"].append({"text": text, "note": "edited by hand", "ts": now})
        d["updated"] = now
        save_draft(d)
    instr = next((c[2] for c in CHIPS if c[0] == chip), "") or instruction.strip()
    if instr:
        try:
            adjust_draft(cfg, d, instr, d["versions"][-1]["text"])
        except llm.LLMError as e:
            return HTMLResponse(f"<p style='font-family:sans-serif;padding:2rem'>{e}<br><br><a href='/draft/{did}'>Back</a></p>", status_code=502)
    return RedirectResponse(f"/draft/{did}", status_code=303)


@app.post("/draft/{did}/delete")
def delete_draft(did: str):
    load_draft(did)   # 404 if it is already gone
    draft_path(did).unlink(missing_ok=True)
    return RedirectResponse("/drafts", status_code=303)


@app.post("/draft/{did}/revert")
def revert(did: str, version: int = Form(...)):
    d = load_draft(did)
    if not 0 <= version < len(d["versions"]):
        raise HTTPException(404, "That version does not exist.")
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
    text = norm(text)
    if text and text != norm(d["versions"][-1]["text"]):
        d["versions"].append({"text": text, "note": "final edit", "ts": now})
    d["status"] = "approved"
    d["updated"] = now
    save_draft(d)
    sec = next((s for s in load_manifest(cfg)["sections"] if s["number"] == d["section"]), None)
    outdir = course_dir(cfg) / ((sec or {}).get("dir") or f"section-{int(d['section'] or 0):02d}") / "outbox"
    outdir.mkdir(parents=True, exist_ok=True)
    fname = f"{d['card']}-{d['cmid']}-{time.strftime('%Y%m%d-%H%M')}.md"
    (outdir / fname).write_text(d["versions"][-1]["text"], encoding="utf-8")
    d["outbox"] = str((outdir / fname).relative_to(course_dir(cfg)))
    save_draft(d)
    return RedirectResponse(f"/draft/{did}", status_code=303)


@app.get("/draft/{did}/text", response_class=PlainTextResponse)
def draft_text(did: str):
    return load_draft(did)["versions"][-1]["text"]


@app.get("/__kit")
def kit_info():
    return JSONResponse({"root": str(ROOT), "version": kit_version()})


@app.post("/__quit")
def kit_quit():
    """Lets a newer launch replace a stale copy (one left running after the folder was moved or updated)."""
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return JSONResponse({"ok": True})


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
    keys = model.get("keys") if isinstance(model.get("keys"), dict) else {}
    saved_keys = sorted(k for k, v in keys.items() if v) or ([model.get("provider") or "gemini"] if model.get("api_key") else [])
    return templates.TemplateResponse(request, "settings.html", {
        "cfg": cfg, "model": model, "presets": llm.PRESETS, "resolved": llm.resolve(model), "root": str(ROOT),
        "config_exists": CONFIG.exists(), "saved_keys": saved_keys,
        "authors": authors, "own_count": len(own_posts(cfg, man, limit=99)),
        "about": profile_text("about"), "voice": profile_text("voice"), "rubric": profile_text("rubric")})


@app.post("/settings")
def save_settings(provider: str = Form("gemini"), api_key: str = Form(""), name: str = Form(""), base_url: str = Form(""),
                  length: str = Form("250-350 words"), course_url: str = Form(""), student_name: str = Form(""),
                  update_repo: str = Form(""), update_token: str = Form(""),
                  about: str = Form(""), voice: str = Form(""), rubric: str = Form(""), then_test: str = Form(""),
                  clear_key: str = Form("")):
    cfg = load_cfg()
    m = cfg.get("model", {}) or {}
    if provider not in llm.PRESETS:
        provider = "custom"
    keys = m.get("keys") if isinstance(m.get("keys"), dict) else {}
    if m.get("api_key") and not keys:   # config from an older build: that one key belonged to the provider saved then
        keys[m.get("provider") or "gemini"] = m["api_key"]
    if clear_key:
        keys.pop(provider, None)
    if api_key.strip():
        keys[provider] = api_key.strip()
    m["provider"] = provider
    m["keys"] = keys
    m["api_key"] = keys.get(provider, "")   # the key in use; kept here too so a config file stays easy to read
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
    return RedirectResponse("/settings?saved=1" + ("&test=1" if then_test else ""), status_code=303)


@app.post("/settings/test")
def test_model():
    cfg = load_cfg()
    try:
        reply = llm.chat(cfg.get("model", {}), "Reply with the single word OK.", "Ping.")
        return JSONResponse({"ok": True, "reply": reply[:80]})
    except llm.LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)})


def main():
    import socket
    import uvicorn
    port = PORT
    url = f"http://127.0.0.1:{port}"
    ensure_config()
    if not CONFIG.exists():
        print(f"WARNING: no config.yaml in {ROOT}. Was the folder moved while Course Kit was running?")
    import urllib.request
    with socket.socket() as sck:
        port_busy = sck.connect_ex(("127.0.0.1", port)) == 0
    if port_busy:
        other = None
        try:
            with urllib.request.urlopen(f"{url}/__kit", timeout=3) as r:
                other = json.loads(r.read())
        except Exception:
            pass
        if other and other.get("root") == str(ROOT):
            print(f"Course Kit is already running from this folder. Opening {url}.")
            webbrowser.open(url)
            return
        print(f"Another Course Kit is running from {other.get('root') if other else 'an older build or a moved folder'}; stopping it.")
        try:
            urllib.request.urlopen(urllib.request.Request(f"{url}/__quit", method="POST"), timeout=3)
        except Exception:
            pass
        for _ in range(40):
            time.sleep(0.25)
            with socket.socket() as sck:
                if sck.connect_ex(("127.0.0.1", port)) != 0:
                    break
        else:
            print("Could not stop it. Quit Terminal completely (Cmd+Q, then Terminate) and run Course Kit again.")
            return
    print(f"Course Kit folder: {ROOT}")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
