#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0", "beautifulsoup4", "markdownify", "pyyaml"]
# ///
"""Pull one Moodle course into a local folder tree, using YOUR real Chrome session.

First run: a Chrome window opens on the course login page. Log in by hand
(CAC, username/password, whatever the site wants). The session is saved in a
browser profile folder next to this script, so later runs are silent.

Usage:
  python fetch_moodle.py --config config.yaml --tree          # just print the section tree (first test)
  python fetch_moodle.py --config config.yaml                 # fetch everything
  python fetch_moodle.py --config config.yaml --section 7     # one section/week only
  python fetch_moodle.py --config config.yaml --force         # refetch even if already saved
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import yaml
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout

import parse_moodle as pm

LOGIN_WAIT_SECONDS = 15 * 60
MAX_LINK_DEPTH = 2   # section page -> module page -> book/forum/quiz linked from it


def log(msg: str):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Fetcher:
    def __init__(self, cfg: dict, force: bool):
        self.cfg = cfg
        if cfg.get("course_url"):
            self.base, cid = pm.split_course_url(cfg["course_url"])
            self.course_id = int(cfg["course_id"]) if cfg.get("course_id") else cid  # may be None until login
            self.start_url = cfg["course_url"]
        else:
            self.base = cfg["base_url"].rstrip("/") + "/"
            self.course_id = int(cfg["course_id"])
            self.start_url = urljoin(self.base, f"course/view.php?id={self.course_id}")
        self.out = Path(cfg.get("out_dir", "./course")).resolve()
        self.pace = float(cfg.get("pace_seconds", 1.5))
        self.force = force
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self.errors: list[str] = []
        self.seen_cmids: set[int] = set()
        self.top_level_cmids: set[int] = set()   # module pages listed in the sections; never nested under each other

    # ---------- browser / login ----------
    def start(self, pw):
        b = self.cfg.get("browser", {})
        profile = Path(b.get("profile_dir", "./.browser-profile")).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self.keep_session_cookies(profile)
        kwargs = dict(user_data_dir=str(profile), headless=bool(b.get("headless", False)),
                      accept_downloads=True, viewport={"width": 1280, "height": 900})
        channel = b.get("channel", "chrome")
        try:
            self.ctx = pw.chromium.launch_persistent_context(channel=channel, **kwargs)
            log(f"launched system browser channel={channel}")
        except Exception as e:  # system Chrome missing -> bundled Chromium (CAC may not work there)
            log(f"could not launch channel={channel} ({e.__class__.__name__}); falling back to bundled chromium")
            self.ctx = pw.chromium.launch_persistent_context(**kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    @staticmethod
    def keep_session_cookies(profile: Path):
        """Moodle's login cookie is a session cookie. Chrome only keeps those across restarts when
        'continue where you left off' is on, so set that preference in our private profile."""
        prefs_path = profile / "Default" / "Preferences"
        prefs = {}
        if prefs_path.exists():
            try:
                prefs = json.loads(prefs_path.read_text())
            except Exception:
                prefs = {}
        prefs.setdefault("session", {})["restore_on_startup"] = 1
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(json.dumps(prefs))

    def ensure_logged_in(self) -> str:
        """Open the start URL, wait for a human login if needed, return the course page HTML."""
        self.page.goto(self.start_url, wait_until="domcontentloaded")
        deadline = time.time() + LOGIN_WAIT_SECONDS
        nagged = False
        while time.time() < deadline:
            try:
                html = self.page.content()
                on_login = "login" in self.page.url.lower()
            except Exception:  # page is mid-navigation (login redirects, CAC handshake); try again shortly
                time.sleep(1)
                continue
            if not on_login and pm.is_logged_in(html):
                if self.course_id is None:
                    self.course_id = pm.course_id_from_html(html)
                    if self.course_id is None:
                        raise SystemExit("Logged in, but could not find the course id on that page. "
                                         "Use a URL from inside the course, or set course_id in config.yaml.")
                    log(f"course id = {self.course_id}")
                course_url = urljoin(self.base, f"course/view.php?id={self.course_id}")
                if "course/view.php" not in self.page.url:
                    self.page.goto(course_url, wait_until="domcontentloaded")
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=8000)
                    except PWTimeout:
                        pass
                    html = self.page.content()
                log("logged in; course page loaded")
                return html
            if not nagged:
                log(">>> Please log in using the browser window (CAC / username). I'll wait up to 15 minutes.")
                nagged = True
            time.sleep(2)
        raise SystemExit("Timed out waiting for login.")

    # ---------- helpers ----------
    def get_html(self, url: str) -> str:
        time.sleep(self.pace)
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        return self.page.content()

    def download(self, url: str, dest_dir: Path, hint: str = "", via_browser_if_html: bool = False) -> Path | None:
        """Download with the browser's cookies via the request context (no new tab)."""
        if not pm.host_allowed(url, self.base):
            return None   # never fetch from unrelated hosts
        time.sleep(self.pace)
        try:
            resp = self.ctx.request.get(url, max_redirects=10)
        except Exception as e:
            self.errors.append(f"download failed {url}: {e}")
            return None
        if not resp.ok:
            self.errors.append(f"download HTTP {resp.status} {url}")
            return None
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            if via_browser_if_html:
                return self.download_via_browser(url, dest_dir)
            return None  # not a file; caller will treat as a page
        name = None
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        if m:
            name = m.group(1).strip()
        if not name:
            name = resp.url.split("?")[0].rsplit("/", 1)[-1] or hint or "file.bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists() and not self.force:
            return dest
        dest.write_bytes(resp.body())
        log(f"  saved {dest.relative_to(self.out)} ({len(resp.body())//1024} KB)")
        return dest

    def download_via_browser(self, url: str, dest_dir: Path) -> Path | None:
        """Some 'library' pages hand out the file through a script instead of a direct link. Open the page in
        the real browser and catch the download it triggers."""
        page = self.ctx.new_page()
        try:
            with page.expect_download(timeout=20000) as dl_info:
                try:
                    page.goto(url, wait_until="commit")
                except Exception:
                    pass   # navigation to an attachment raises; the download event is what matters
            dl = dl_info.value
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / (dl.suggested_filename or "download.bin")
            if dest.exists() and not self.force:
                return dest
            dl.save_as(str(dest))
            log(f"  saved {dest.relative_to(self.out)} (via browser)")
            return dest
        except Exception as e:
            self.errors.append(f"browser download failed {url}: {e.__class__.__name__}")
            return None
        finally:
            page.close()

    def resolve_url_module(self, url: str | None) -> str | None:
        """Where a mod/url item points, read from the redirect header so off-site targets are never visited."""
        if not url:
            return None
        time.sleep(self.pace)
        try:
            resp = self.ctx.request.get(url + "&redirect=1", max_redirects=0)
        except Exception as e:
            self.errors.append(f"url module {url}: {e}")
            return None
        if 300 <= resp.status < 400:
            return resp.headers.get("location")
        if resp.ok:
            links = [u for _, u in pm.parse_pluginfile_links(resp.text(), self.base)]
            return links[0] if links else None
        return None

    def write(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # ---------- module handlers ----------
    def handle_module(self, mod: pm.Module, sec_dir: Path, depth: int = 0) -> dict:
        rec = {"cmid": mod.cmid, "type": mod.modname, "name": mod.name, "url": mod.url, "files": [], "md": None}
        if mod.chapterid is not None:
            rec["chapterid"] = mod.chapterid
        if mod.cmid is not None:
            if mod.cmid in self.seen_cmids:
                rec["duplicate_of"] = mod.cmid   # already fetched elsewhere this run
                return rec
            self.seen_cmids.add(mod.cmid)
        raw_dir, md_dir, files_dir = sec_dir / "raw", sec_dir / "md", sec_dir / "files"
        base = f"{mod.modname}-{mod.cmid or 'x'}-{pm.slug(mod.name)}"
        if mod.cmid is not None:   # the same item may have been saved under another link text in an earlier run
            prior = sorted(md_dir.glob(f"{mod.modname}-{mod.cmid}-*.md")) if md_dir.exists() else []
            if prior and not self.force:
                base = prior[0].stem
        md_path = md_dir / f"{base}.md"
        if md_path.exists() and not self.force and mod.modname not in ("forum",):
            rec["md"] = str(md_path.relative_to(self.out))
            if mod.modname == "book":
                rec["chapters"] = {p.stem.split("-ch", 1)[1].split("-", 1)[0]: str(p.relative_to(self.out))
                                   for p in md_dir.glob(f"book-{mod.cmid}-ch*.md")}
            raw_path = raw_dir / f"{base}.html"
            if mod.modname == "page" and raw_path.exists() and depth < MAX_LINK_DEPTH:
                self.follow_links(mod, raw_path.read_text(encoding="utf-8"), rec, sec_dir, depth)
            return rec
        if mod.modname == "label":
            if mod.description_md:
                self.write(md_path, f"# {mod.name}\n\n{mod.description_md}\n")
                rec["md"] = str(md_path.relative_to(self.out))
            return rec
        if mod.modname == "url":
            target = self.resolve_url_module(mod.url)
            if target and pm.host_allowed(target, self.base):
                p = self.download(target, files_dir, hint=pm.slug(mod.name), via_browser_if_html=True)
                if p:
                    rec["files"].append(str(p.relative_to(self.out)))
            self.write(md_path, f"# {mod.name}\n\nLink target: {target or mod.url}\n\n{mod.description_md}\n")
            rec["md"] = str(md_path.relative_to(self.out))
            rec["target"] = target
            return rec
        if not mod.url:
            return rec
        if mod.modname == "resource":
            p = self.download(mod.url + "&redirect=1", files_dir, hint=pm.slug(mod.name))
            if p:
                rec["files"].append(str(p.relative_to(self.out)))
                return rec
        html = self.get_html(mod.url)
        if pm.PLACEHOLDER_NAME_RE.match(mod.name):   # linked with no link text (an image link, say)
            title = pm.page_title(html)
            if title:
                mod.name = title
                rec["name"] = title
                base = f"{mod.modname}-{mod.cmid or 'x'}-{pm.slug(mod.name)}"
                md_path = md_dir / f"{base}.md"
        self.write(raw_dir / f"{base}.html", html)
        header = f"# {mod.name}\n\n_type: {mod.modname} · source: {mod.url}_\n\n"
        if mod.modname == "forum":
            discussions = pm.parse_forum_view(html, self.base)
            chunks = [header, mod.description_md, ""]
            for d in discussions:
                dhtml = self.get_html(d["url"])
                self.write(raw_dir / f"{base}-d{d['discussion_id']}.html", dhtml)
                posts = pm.parse_forum_discussion(dhtml)
                chunks.append(f"\n## Discussion: {d['title']}\n")
                for fname, furl in pm.parse_pluginfile_links(dhtml, self.base):   # attachments (the weekly baseline!)
                    fp = self.download(furl, files_dir, hint=pm.slug(fname))
                    if fp:
                        rel = str(fp.relative_to(self.out))
                        if rel not in rec["files"]:
                            rec["files"].append(rel)
                        chunks.append(f"_attachment: {fp.name}_\n")
                for p in posts:
                    chunks.append(f"\n### {p['subject'] or '(no subject)'}\n**{p['author']}** · {p['time']}\n\n{p['body_md']}\n")
            rec["discussions"] = len(discussions)
            self.write(md_path, "\n".join(chunks))
        elif mod.modname == "book":
            chapters = pm.parse_book_chapters(html, mod.url)
            log(f"  book {mod.name[:50]}: {len(chapters)} chapters")
            chunks = [header]
            rec["chapters"] = {}
            for title, curl, chapterid in chapters:
                chtml = self.get_html(curl)
                body = pm.parse_page(chtml)
                chunks.append(f"\n\n## {title}\n\n{body}")
                if chapterid is not None:
                    ch_path = md_dir / f"book-{mod.cmid}-ch{chapterid}-{pm.slug(title, 40)}.md"
                    self.write(ch_path, f"# {mod.name}: {title}\n\n_type: book chapter · source: {curl}_\n\n{body}\n")
                    rec["chapters"][str(chapterid)] = str(ch_path.relative_to(self.out))
            if not chapters:
                chunks.append(pm.parse_page(html))
            self.write(md_path, "\n".join(chunks))
        else:  # page, quiz (intro only), assign, folder, resource-as-page, anything else
            self.write(md_path, header + pm.parse_page(html))
            for fname, furl in pm.parse_pluginfile_links(html, self.base):
                p = self.download(furl, files_dir, hint=pm.slug(fname))
                if p:
                    rec["files"].append(str(p.relative_to(self.out)))
        rec["md"] = str(md_path.relative_to(self.out))
        # items linked from inside this page's text (module pages link to the hidden books/forums/quizzes)
        if mod.modname == "page" and depth < MAX_LINK_DEPTH:
            self.follow_links(mod, html, rec, sec_dir, depth)
        return rec

    def follow_links(self, mod: pm.Module, html: str, rec: dict, sec_dir: Path, depth: int):
        children = [c for c in pm.linked_modules(html, self.base)
                    if c.cmid not in self.top_level_cmids or c.cmid == mod.cmid]
        children = [c for c in children if c.cmid != mod.cmid]
        if not children:
            return
        log(f"  {mod.name[:60]} links to {len(children)} items")
        rec["children"] = []
        for child in children:
            child.linked_from = mod.name
            try:
                rec["children"].append(self.handle_module(child, sec_dir, depth + 1))
            except Exception as e:
                self.errors.append(f"{child.modname} {child.name}: {e}")
                log(f"  !! {child.modname} {child.name}: {e}")

    # ---------- course structure, three ways ----------
    def sections_via_state_api(self, course_html: str) -> list[pm.Section]:
        """Moodle 4.x: ask the same endpoint the course index uses. Returns [] on older sites."""
        sesskey = pm.sesskey_from_html(course_html)
        if not sesskey:
            return []
        js = """async ([base, sesskey, courseid]) => {
            const r = await fetch(base + 'lib/ajax/service.php?sesskey=' + sesskey + '&info=core_courseformat_get_state', {
                method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify([{index: 0, methodname: 'core_courseformat_get_state', args: {courseid: courseid}}])});
            return await r.text(); }"""
        try:
            txt = self.page.evaluate(js, [self.base, sesskey, self.course_id])
            resp = json.loads(txt)
            if not isinstance(resp, list) or resp[0].get("error"):
                return []
            data = resp[0]["data"]
            state = json.loads(data) if isinstance(data, str) else data
            self.write(self.out / "raw-course-state.json", json.dumps(state, indent=1))
            return pm.sections_from_state(state, self.base)
        except Exception as e:
            log(f"state api not available ({e.__class__.__name__}); trying next method")
            return []

    def sections_via_section_pages(self, sections: list[pm.Section]) -> list[pm.Section]:
        """Tiles/grid/one-section-per-page formats: each section has its own page."""
        out = []
        for s in sections:
            url = urljoin(self.base, f"course/view.php?id={self.course_id}&section={s.number}")
            html = self.get_html(url)
            self.write(self.out / "raw-sections" / f"section-{s.number:02d}.html", html)
            parsed = [x for x in pm.parse_course(html, self.base) if x.modules]
            if parsed:
                hit = next((x for x in parsed if x.number == s.number), parsed[0])
                hit.number = s.number
                if hit.name.startswith("Section ") and not s.name.startswith("Section "):
                    hit.name = s.name
                out.append(hit)
            else:
                out.append(s)
        return out

    def enrich_from_section_pages(self, sections: list[pm.Section]):
        """Visit each section's own page: full label text, files linked from it, and items only reachable by link."""
        for s in sections:
            url = urljoin(self.base, f"course/view.php?id={self.course_id}&section={s.number}")
            html = self.get_html(url)
            self.write(self.out / "raw-sections" / f"section-{s.number:02d}.html", html)
            for parsed in pm.parse_course(html, self.base):
                if parsed.number != s.number:
                    continue
                if parsed.summary_md and not s.summary_md:
                    s.summary_md = parsed.summary_md
                for m in parsed.modules:
                    mine = next((x for x in s.modules if x.cmid == m.cmid), None)
                    if mine is None:
                        s.modules.append(m)
                    elif m.description_md and len(m.description_md) > len(mine.description_md):
                        mine.description_md = m.description_md
                        if mine.name.endswith("...") and len(m.name) > len(mine.name):
                            mine.name = m.name
            for m in pm.linked_modules(html, self.base):
                if not s.has(m.cmid):
                    m.linked_from = f"section {s.number}"
                    s.modules.append(m)
            s.files = pm.parse_pluginfile_links(html, self.base)

    def course_structure(self, course_html: str) -> list[pm.Section]:
        fmt = pm.course_format_from_html(course_html)
        log(f"course format: {fmt or 'unknown'}")
        sections = pm.parse_course(course_html, self.base)
        result = None
        if sections and any(s.modules for s in sections):
            log(f"structure from course page: {len(sections)} sections")
            result = sorted(sections, key=lambda x: x.number)
        if result is None:
            via_state = self.sections_via_state_api(course_html)
            if via_state and any(s.modules for s in via_state):
                log(f"structure from course-state api: {len(via_state)} sections")
                result = via_state
        if result is None and sections:
            log("course page lists sections but no items; trying one page per section")
            via_pages = self.sections_via_section_pages(sections)
            if any(s.modules for s in via_pages):
                result = sorted(via_pages, key=lambda x: x.number)
        if result is not None:
            log("visiting each section page for linked items and full label text")
            self.enrich_from_section_pages(result)
            return result
        raise SystemExit("Could not find any course items. Send course/raw-course.html (and raw-sections/ if present) "
                         "back so the parser can be adjusted.")

    # ---------- course ----------
    def load_structure(self) -> list[pm.Section]:
        if getattr(self, "_sections", None):
            return self._sections
        course_html = self.ensure_logged_in()
        self.out.mkdir(parents=True, exist_ok=True)
        self.write(self.out / "raw-course.html", course_html)
        self._course_html = course_html
        self._sections = self.course_structure(course_html)
        return self._sections

    def run(self, only_section: int | None, tree_only: bool, quiet: bool = False):
        sections = self.load_structure()
        for s in ([] if quiet else sections):
            print(f"[{s.number:02d}] {s.name}")
            for m in s.modules:
                tag = "  (linked)" if m.linked_from else ""
                print(f"      {m.modname:10s} {m.name}{tag}")
            for name, _ in s.files:
                print(f"      {'file':10s} {name}  (linked)")
        if tree_only:
            return
        dl = pm.find_download_course_link(self._course_html, self.base)
        if dl:
            log("site offers 'Download course content' - grabbing the zip too")
            self.download(dl, self.out / "download-course-content")
        manifest_path = self.out / "manifest.json"
        manifest = {"course_id": self.course_id, "base_url": self.base, "sections": []}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception:
                pass
        manifest["fetched"] = time.strftime("%Y-%m-%d %H:%M")
        manifest["sections"] = [x for x in manifest.get("sections", [])
                                if only_section is not None and x["number"] != only_section]
        # module pages listed in the sections link to each other (next/previous); never nest those under each other
        self.top_level_cmids = {m.cmid for sec in sections for m in sec.modules if m.cmid is not None and m.modname == "page"}
        for s in sections:
            if only_section is not None and s.number != only_section:
                continue
            sec_dir = self.out / f"section-{s.number:02d}-{pm.slug(s.name, 32)}"
            log(f"section {s.number}: {s.name} ({len(s.modules)} items)")
            srec = {"number": s.number, "name": s.name, "dir": sec_dir.name, "modules": []}
            if s.summary_md:
                self.write(sec_dir / "md" / "00-section-summary.md", f"# {s.name}\n\n{s.summary_md}\n")
            srec["files"] = []
            for fname, furl in s.files:
                p = self.download(furl, sec_dir / "files", hint=pm.slug(fname))
                if p:
                    srec["files"].append(str(p.relative_to(self.out)))
            for m in s.modules:
                try:
                    srec["modules"].append(self.handle_module(m, sec_dir))
                except Exception as e:
                    self.errors.append(f"{m.modname} {m.name}: {e}")
                    log(f"  !! {m.modname} {m.name}: {e}")
            manifest["sections"].append(srec)
        manifest["sections"].sort(key=lambda x: x["number"])
        manifest["errors"] = self.errors
        self.write(manifest_path, json.dumps(manifest, indent=2))
        log(f"done. manifest at {manifest_path}; {len(self.errors)} errors")
        self.errors = []
        self.seen_cmids = set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--section", type=int, default=None)
    ap.add_argument("--tree", action="store_true", help="only print the section tree")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--interactive", action="store_true",
                    help="ask for section numbers one after another, keeping the browser (and login) open")
    a = ap.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    f = Fetcher(cfg, force=a.force)
    with sync_playwright() as pw:
        f.start(pw)
        try:
            if a.interactive:
                f.run(None, tree_only=True)
                while True:
                    ans = input("\nSection number to fetch (blank = whole course, q = quit): ").strip().lower()
                    if ans == "q":
                        break
                    sec = int(ans) if ans.isdigit() else None
                    f.run(sec, tree_only=False, quiet=True)
                    print("\nDone with that one. The browser stays open so you won't need to log in again.")
            else:
                f.run(a.section, a.tree)
        finally:
            f.ctx.close()


if __name__ == "__main__":
    main()
