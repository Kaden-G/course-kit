import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright
import mock_moodle
from fetch_moodle import Fetcher


def test_end_to_end(tmp_path):
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + "mod/book/view.php?id=602584&chapterid=176352", "out_dir": str(tmp_path / "course"), "pace_seconds": 0,
               "browser": {"channel": "definitely-not-installed", "headless": True,
                           "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                f.run(only_section=None, tree_only=False)
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()
    out = tmp_path / "course"
    man = json.loads((out / "manifest.json").read_text())
    assert man["errors"] == []
    assert [s["number"] for s in man["sections"]] == [0, 7]
    wk7 = man["sections"][1]
    bytype = {m["type"]: m for m in wk7["modules"]}
    d = out / wk7["dir"]
    # page -> markdown with the pluginfile attachment downloaded
    assert "sustainment" in (out / bytype["page"]["md"]).read_text()
    assert any(p.endswith("syllabus.pdf") for p in bytype["page"]["files"])
    # resource -> file via redirect=1, named from Content-Disposition
    assert bytype["resource"]["files"] == [f"{wk7['dir']}/files/sustainment.pdf"]
    assert (out / bytype["resource"]["files"][0]).read_bytes().startswith(b"%PDF")
    # forum -> both discussions, both posts, classmate captured
    fmd = (out / bytype["forum"]["md"]).read_text()
    assert bytype["forum"]["discussions"] == 2
    assert "Jane Classmate" in fmd and "## Discussion: Introductions" in fmd
    assert [Path(p).name for p in bytype["forum"]["files"]] == ["baseline_wk6.xlsx"] and "_attachment: baseline_wk6.xlsx_" in fmd
    # assign -> the wargame xlsx came down once
    assert [Path(p).name for p in bytype["assign"]["files"]] == ["wargame_turn7.xlsx"]
    # label and url handled without network
    assert "Baseline for Turn 6" in (out / bytype["label"]["md"]).read_text()
    assert bytype["url"]["target"].endswith("rubric.pdf") and [Path(p).name for p in bytype["url"]["files"]] == ["rubric.pdf"]
    # download-course-content zip picked up
    assert (out / "download-course-content" / "course.zip").exists()
    # raw html kept for parser debugging
    assert (d / "raw").glob("*.html")


def test_login_wait_survives_navigation_race(tmp_path):
    """page.content() raising mid-navigation must not abort the login wait (seen on the real site)."""
    from playwright.sync_api import Error as PWError
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + "course/view.php?id=42", "out_dir": str(tmp_path / "course"),
               "pace_seconds": 0, "browser": {"channel": "nope", "headless": True,
                                              "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                real_content = f.page.content
                calls = {"n": 0}

                def flaky():
                    calls["n"] += 1
                    if calls["n"] <= 2:
                        raise PWError("Page.content: Unable to retrieve content because the page is navigating")
                    return real_content()
                f.page.content = flaky
                html = f.ensure_logged_in()
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()
    assert calls["n"] >= 3 and "Week 7: Logistics" in html


def _tree(tmp_path, course_id):
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + f"course/view.php?id={course_id}", "out_dir": str(tmp_path / "course"),
               "pace_seconds": 0, "browser": {"channel": "nope", "headless": True,
                                              "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                html = f.ensure_logged_in()
                f.out.mkdir(parents=True, exist_ok=True)
                return f.course_structure(html), f
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()


def test_hollow_course_page_uses_state_api(tmp_path):
    secs, f = _tree(tmp_path, 43)
    assert [s.number for s in secs] == [3, 4, 7]                       # sorted, not tile order
    assert secs[0].name == "Week 3: Maneuver"
    assert [(m.modname, m.cmid) for m in secs[2].modules] == [("forum", 703), ("label", 706)]
    assert secs[2].modules[0].url.endswith("mod/forum/view.php?id=703")
    assert (f.out / "raw-course-state.json").exists()
    # tiles: section 4's label links to a module page and a forum; placeholders dropped; label text kept in full
    wk4 = secs[1]
    assert [(m.modname, m.cmid, bool(m.linked_from)) for m in wk4.modules] == [
        ("page", 7401, False), ("page", 7402, False), ("label", 7406, False), ("forum", 703, True)]
    assert "Module 1: MCDP 1" in wk4.modules[2].description_md
    assert [n for n, _ in wk4.files] == ["Wargame baseline"]


def test_hollow_course_page_falls_back_to_section_pages(tmp_path):
    secs, f = _tree(tmp_path, 44)
    nums = [s.number for s in secs]
    assert nums == [3, 4, 7]
    wk7 = next(s for s in secs if s.number == 7)
    assert wk7.name == "Week 7: Logistics" and len(wk7.modules) == 7
    assert (f.out / "raw-sections" / "section-07.html").exists()


def test_tiles_section_full_fetch(tmp_path):
    mock_moodle.H.hits.clear()
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + "course/view.php?id=43", "out_dir": str(tmp_path / "course"),
               "pace_seconds": 0, "browser": {"channel": "nope", "headless": True,
                                              "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                f.run(only_section=4, tree_only=False)
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()
    out = tmp_path / "course"
    man = json.loads((out / "manifest.json").read_text())
    assert man["errors"] == [] and [s["number"] for s in man["sections"]] == [4]
    sec = man["sections"][0]
    assert [Path(p).name for p in sec["files"]] == ["wargame_baseline.xlsx"]
    bycm = {m["cmid"]: m for m in sec["modules"]}
    page, page2, label = bycm[7401], bycm[7402], bycm[7406]
    assert page["name"] == "Week 4 Overview" and page2["name"] == "Module 2"
    # second hop: links followed in order; the NEXT-module image link (7402) is not nested here
    kids = {c["type"]: c for c in page["children"]}
    assert [c["type"] for c in page["children"]] == ["book", "forum", "quiz"]
    # the book came down once, every chapter in its own file, and the link's chapter rides on the child record
    assert kids["book"]["chapterid"] == 2 and kids["book"]["name"] == "Lesson 1 Reading"
    assert sorted(kids["book"]["chapters"]) == ["1", "2"]
    assert "Body of chapter 2" in (out / kids["book"]["chapters"]["2"]).read_text()
    assert "Body of chapter 1" in (out / kids["book"]["chapters"]["1"]).read_text()
    combined = (out / kids["book"]["md"]).read_text()
    assert "## Ch 1" in combined and "## Ch 2" in combined and "chapterid=1)" not in combined   # TOC not in the text
    assert kids["forum"]["discussions"] == 2 and Path(kids["forum"]["md"]).name.startswith("forum-703-")
    assert [Path(p).name for p in page["files"]] == ["module1.pdf"]
    # module 2 is fetched as its own top-level item, pointing at chapter 1 of the same book
    k2 = {c["type"]: c for c in page2["children"]}
    assert k2["book"]["duplicate_of"] == 602584 and k2["book"]["chapterid"] == 1
    assert k2["forum"]["duplicate_of"] == 703
    assert "Module 1: MCDP 1" in (out / label["md"]).read_text()
    # the forum linked from the label too is not fetched twice
    assert bycm[703]["duplicate_of"] == 703
    assert sum("forum/view.php?id=703" in h for h in mock_moodle.H.hits) == 1
    assert sum("mod/book/view.php?id=602584&chapterid=2" in h for h in mock_moodle.H.hits) == 1


def test_offsite_url_module_is_recorded_not_followed(tmp_path):
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + "course/view.php?id=42", "out_dir": str(tmp_path / "course"),
               "pace_seconds": 0, "browser": {"channel": "nope", "headless": True,
                                              "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                f.ensure_logged_in()
                f.out.mkdir(parents=True, exist_ok=True)
                target = f.resolve_url_module(base + "mod/url/view.php?id=708")
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()
    assert target == "https://external.example/doc"


def test_profile_prefs_keep_session_cookies(tmp_path):
    Fetcher.keep_session_cookies(tmp_path / "prof")
    prefs = json.loads((tmp_path / "prof" / "Default" / "Preferences").read_text())
    assert prefs["session"]["restore_on_startup"] == 1
    (tmp_path / "prof" / "Default" / "Preferences").write_text('{"other": true, "session": {"x": 2}}')
    Fetcher.keep_session_cookies(tmp_path / "prof")
    prefs = json.loads((tmp_path / "prof" / "Default" / "Preferences").read_text())
    assert prefs["other"] is True and prefs["session"] == {"x": 2, "restore_on_startup": 1}


def test_manifest_merges_across_section_runs(tmp_path):
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + "course/view.php?id=42", "out_dir": str(tmp_path / "course"),
               "pace_seconds": 0, "browser": {"channel": "nope", "headless": True,
                                              "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                f.run(7, tree_only=False)
                f.run(0, tree_only=False)       # second section in the same sitting
                f.run(7, tree_only=False)       # refetch replaces, doesn't duplicate
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()
    man = json.loads((tmp_path / "course" / "manifest.json").read_text())
    assert [s["number"] for s in man["sections"]] == [0, 7]


def test_library_page_download_via_browser(tmp_path):
    srv, base = mock_moodle.start()
    try:
        cfg = {"course_url": base + "course/view.php?id=42", "out_dir": str(tmp_path / "course"),
               "pace_seconds": 0, "browser": {"channel": "nope", "headless": True,
                                              "profile_dir": str(tmp_path / "profile")}}
        f = Fetcher(cfg, force=False)
        with sync_playwright() as pw:
            f.start(pw)
            try:
                f.ensure_logged_in()
                f.out.mkdir(parents=True, exist_ok=True)
                import parse_moodle as pm
                mod = pm.Module(cmid=709, modname="url", name="Participation Rubric - Online", url=base + "mod/url/view.php?id=709")
                rec = f.handle_module(mod, f.out / "section-01-x")
            finally:
                f.ctx.close()
    finally:
        srv.shutdown()
    assert [Path(p).name for p in rec["files"]] == ["Participation Rubric.pdf"]
    assert (tmp_path / "course" / rec["files"][0]).read_bytes().startswith(b"%PDF")
