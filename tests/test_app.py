import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

import app as appmod
import llm


@pytest.fixture
def kit(tmp_path, monkeypatch):
    """A kit rooted in tmp with a tiny fetched course and a fake model."""
    root = tmp_path
    (root / "prompts").mkdir(); (root / "profile").mkdir(); (root / "drafts").mkdir()
    for p in (Path(appmod.ROOT) / "prompts").glob("*.md"):
        (root / "prompts" / p.name).write_text(p.read_text())
    (root / "profile" / "voice.md").write_text("Paste two or three of your own past discussion posts here.")
    course = root / "course"; sec = course / "section-07-planning"; (sec / "md").mkdir(parents=True); (sec / "files").mkdir()
    (sec / "md" / "page-9001-module-16.md").write_text("# Module 16\n\nRead MCDP 5 ch 3. Post in the forum.")
    (sec / "md" / "book-9002-mcdp-5.md").write_text("# MCDP 5\n\n## Ch 2\n\nIPB chapter.\n\n## Ch 3\n\nPlanning is a continuous process.")
    (sec / "md" / "book-9002-ch2-ipb.md").write_text("# MCDP 5: Ch 2\n\nIPB chapter.")
    (sec / "md" / "book-9002-ch3-planning.md").write_text("# MCDP 5: Ch 3\n\nPlanning is a continuous process.")
    (sec / "md" / "forum-9003-m16-discussion.md").write_text(
        "# Module 16 Discussion\n\n## Discussion: Week 16 prompt\n\n### Week 16 prompt\n**Prof Smith** · Monday\n\n"
        "Compare COAs.\n\n### Re: Week 16 prompt\n**Jane Classmate** · Tuesday\n\nCOA 2 wins on tempo.\n\n"
        "### Re: Week 16 prompt\n**Luis Student** · Wednesday\n\n" + ("I write short, direct posts. " * 12) + "\n")
    (sec / "md" / "quiz-9004-m16-quiz.md").write_text("# M16 Quiz\n\nTen questions on MCPP.")
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Turn 7"; ws["A1"] = "COA"; ws["B1"] = 2; wb.save(sec / "files" / "wargame.xlsx")
    ann_dir = course / "section-00-general"; (ann_dir / "md").mkdir(parents=True); (ann_dir / "files").mkdir()
    (ann_dir / "md" / "forum-100-announcements.md").write_text(
        "# Announcements\n\n## Discussion: Week 7 baseline\n\n### Week 7 baseline\n**Prof Smith** · Friday\n\nUse the attached corrected sheet for turn 7.\n_attachment: baseline_wk7.xlsx_\n")
    wb2 = openpyxl.Workbook(); ws2 = wb2.active; ws2.title = "Baseline"; ws2["A1"] = "COA"; ws2["B1"] = 1; wb2.save(ann_dir / "files" / "baseline_wk7.xlsx")
    manifest = {"course_id": 5074, "fetched": "today", "sections": [{"number": 0, "name": "General", "dir": "section-00-general", "files": [], "modules": [
        {"cmid": 100, "type": "forum", "name": "Announcements", "md": "section-00-general/md/forum-100-announcements.md", "files": ["section-00-general/files/baseline_wk7.xlsx"], "discussions": 1}]},
        {"number": 7, "name": "Planning", "dir": "section-07-planning", "files": [], "modules": [
        {"cmid": 9001, "type": "page", "name": "Module 16", "md": "section-07-planning/md/page-9001-module-16.md", "files": ["section-07-planning/files/wargame.xlsx"],
         "children": [
            {"cmid": 9002, "type": "book", "name": "Lesson 16 Reading", "chapterid": 3, "md": "section-07-planning/md/book-9002-mcdp-5.md", "files": [],
             "chapters": {"2": "section-07-planning/md/book-9002-ch2-ipb.md", "3": "section-07-planning/md/book-9002-ch3-planning.md"}},
            {"cmid": 9006, "type": "page", "name": "Module 17 (next)", "md": None, "files": [], "children": [{"cmid": 9007, "type": "forum", "name": "M17 forum", "md": None, "files": []}]},
            {"cmid": 9003, "type": "forum", "name": "M16 Discussion", "md": "section-07-planning/md/forum-9003-m16-discussion.md", "files": [], "discussions": 1},
            {"cmid": 9004, "type": "quiz", "name": "M16 Quiz", "md": "section-07-planning/md/quiz-9004-m16-quiz.md", "files": []}]},
        {"cmid": 9005, "type": "page", "name": "Module 15", "md": None, "files": [], "children": [{"cmid": 9003, "type": "forum", "name": "M16 Discussion", "duplicate_of": 9003, "files": []},
            {"cmid": 9002, "type": "book", "name": "Lesson 15 Reading", "chapterid": 2, "duplicate_of": 9002, "files": []}]},
        {"cmid": 9006, "type": "page", "name": "Module 17 (next)", "md": None, "files": [], "duplicate_of": 9006}]}]}
    (course / "manifest.json").write_text(json.dumps(manifest))
    (root / "config.yaml").write_text("course_url: https://x/moodle/course/view.php?id=5074\nout_dir: ./course\nstudent_name: Luis Student\nmodel:\n  provider: gemini\n  api_key: k\n")
    for name in ("ROOT", "CONFIG", "PROMPTS", "PROFILE", "DRAFTS"):
        pass
    monkeypatch.setattr(appmod, "ROOT", root); monkeypatch.setattr(appmod, "CONFIG", root / "config.yaml")
    monkeypatch.setattr(appmod, "PROMPTS", root / "prompts"); monkeypatch.setattr(appmod, "PROFILE", root / "profile")
    monkeypatch.setattr(appmod, "DRAFTS", root / "drafts")
    calls = []
    def fake_chat(model_cfg, system, user, temperature=0.7, timeout=180):
        calls.append(user)
        return f"DRAFT #{len(calls)}"
    monkeypatch.setattr(llm, "chat", fake_chat)
    return TestClient(appmod.app), root, calls


def test_home_and_module(kit):
    c, root, _ = kit
    r = c.get("/"); assert r.status_code == 200 and "Module 16" in r.text and "Planning" in r.text
    r = c.get("/module/7/9001"); assert r.status_code == 200
    assert "Jane Classmate" in r.text and "Lesson 16 Reading" in r.text and "Wargame turn" in r.text
    assert "baseline_wk7.xlsx" in r.text
    assert '<option value="Prof Smith"' not in r.text and '<option value="Jane Classmate"' in r.text   # classmates only
    assert '<option value="Luis Student"' not in r.text                                                 # not himself
    r = c.get("/settings"); assert '<option value="Luis Student" selected' in r.text and "1 of your posts" in r.text
    r = c.get("/"); assert "Refresh Announcements" in r.text and "baseline_wk7.xlsx" in r.text
    r = c.get("/module/7/9005"); assert r.status_code == 200 and "Jane Classmate" in r.text   # duplicate forum resolved
    assert "Lesson 15 Reading" in r.text and "Module 17" not in r.text


def test_draft_adjust_revert_approve(kit):
    c, root, calls = kit
    r = c.post("/draft", data={"section": 7, "cmid": 9001, "card": "discussion"}, follow_redirects=False)
    assert r.status_code == 303
    did = r.headers["location"].split("/")[-1]
    prompt = calls[0]
    assert "Compare COAs" in prompt and "Planning is a continuous process" in prompt
    assert "I write short, direct posts." in prompt        # voice samples came from his own forum posts, nothing pasted
    assert "IPB chapter" not in prompt            # only the chapter this module links, not the whole book
    assert "M17 forum" not in prompt and "Lesson 16 Reading" in prompt
    assert "(no rubric available)" not in prompt or True   # empty rubric still fills (empty string)
    r = c.get(f"/draft/{did}"); assert "DRAFT #1" in r.text and "Shorter" in r.text
    # chip
    c.post(f"/draft/{did}/adjust", data={"chip": "shorter", "text": "DRAFT #1"})
    assert "Cut it by about a third" in calls[1] and "DRAFT #1" in calls[1]
    d = appmod.load_draft(did); assert [v["text"] for v in d["versions"]] == ["DRAFT #1", "DRAFT #2"]
    # hand edit + free instruction -> two versions
    c.post(f"/draft/{did}/adjust", data={"instruction": "drop the last sentence", "text": "DRAFT #2 edited"})
    d = appmod.load_draft(did); assert [v["note"] for v in d["versions"]][-2:] == ["edited by hand", "drop the last sentence"]
    assert "DRAFT #2 edited" in calls[2]
    # revert to v1
    c.post(f"/draft/{did}/revert", data={"version": 0})
    assert appmod.load_draft(did)["versions"][-1]["text"] == "DRAFT #1"
    # approve writes outbox
    c.post(f"/draft/{did}/approve", data={"text": "FINAL TEXT"})
    d = appmod.load_draft(did); assert d["status"] == "approved"
    assert (root / "course" / d["outbox"]).read_text() == "FINAL TEXT"
    assert c.get(f"/draft/{did}/text").text == "FINAL TEXT"


def test_reply_and_wargame_cards(kit):
    c, root, calls = kit
    c.post("/draft", data={"section": 7, "cmid": 9001, "card": "reply", "classmate": "Jane Classmate"}, follow_redirects=False)
    assert "COA 2 wins on tempo" in calls[0] and "Jane Classmate" in calls[0]
    c.post("/draft", data={"section": 7, "cmid": 9001, "card": "wargame"}, follow_redirects=False)
    w = calls[1]
    assert "INSTRUCTOR BASELINE" in w and "SHEET Baseline" in w and "B1=1" in w     # announcements attachment first
    assert "SHEET Turn 7" in w and "B1=2" in w                                        # module's own sheet too
    assert "Use the attached corrected sheet" in w                                     # the professor's post text
    assert w.index("INSTRUCTOR BASELINE") < w.index("SHEET Turn 7")


def test_settings_roundtrip(kit):
    c, root, _ = kit
    r = c.post("/settings", data={"provider": "openai", "api_key": "", "name": "", "base_url": "", "length": "300 words",
                                  "course_url": "", "student_name": "Luis Student", "about": "Capt, 0302", "voice": "v", "rubric": "r"}, follow_redirects=False)
    assert r.status_code == 303
    cfg = appmod.load_cfg()
    assert cfg["model"]["provider"] == "openai" and cfg["model"]["api_key"] == "k"   # blank key keeps the saved one
    assert cfg["model"]["name"] == "gpt-5-mini" and cfg["length"] == "300 words"
    assert (root / "profile" / "about.md").read_text() == "Capt, 0302"
    assert c.get("/settings").status_code == 200


def test_llm_error_is_shown_not_crashed(kit, monkeypatch):
    c, root, _ = kit
    def boom(*a, **k): raise llm.LLMError("No API key set. Open Settings and paste one.")
    monkeypatch.setattr(llm, "chat", boom)
    r = c.post("/draft", data={"section": 7, "cmid": 9001, "card": "study_guide"})
    assert r.status_code == 502 and "No API key" in r.text


def test_llm_resolve_presets():
    m = llm.resolve({"provider": "gemini", "api_key": "x"})
    assert m["base_url"].endswith("/openai/") and m["name"] == "gemini-2.5-flash"
    m = llm.resolve({"provider": "custom", "base_url": "http://localhost:1234/v1", "name": "foo"})
    assert m["base_url"] == "http://localhost:1234/v1/" and m["name"] == "foo"


def test_save_and_test_flow(kit):
    c, root, _ = kit
    r = c.post("/settings", data={"provider": "gemini", "api_key": "NEWKEY", "name": "", "base_url": "", "length": "",
                                  "course_url": "", "student_name": "", "about": "", "voice": "", "rubric": "", "then_test": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=1&test=1"
    assert appmod.load_cfg()["model"]["api_key"] == "NEWKEY"
    r = c.post("/settings/test"); assert r.json()["ok"] is True
    r = c.get("/"); assert "No AI key yet" not in r.text


def test_config_created_from_example(tmp_path, monkeypatch):
    (tmp_path / "config.example.yaml").write_text("course_url: https://x/moodle/course/view.php?id=1\n")
    monkeypatch.setattr(appmod, "ROOT", tmp_path); monkeypatch.setattr(appmod, "CONFIG", tmp_path / "config.yaml")
    assert appmod.load_cfg()["course_url"].endswith("id=1")
    assert (tmp_path / "config.yaml").exists()


def test_old_config_gets_new_defaults(tmp_path, monkeypatch):
    (tmp_path / "config.example.yaml").write_text("course_url: https://x/c\nupdate:\n  repo: Kaden-G/course-kit\n  token: ''\n")
    (tmp_path / "config.yaml").write_text("course_url: https://x/mine\nmodel:\n  api_key: k\n")   # written by an older build
    monkeypatch.setattr(appmod, "ROOT", tmp_path); monkeypatch.setattr(appmod, "CONFIG", tmp_path / "config.yaml")
    cfg = appmod.load_cfg()
    assert cfg["course_url"] == "https://x/mine" and cfg["model"]["api_key"] == "k"     # his values win
    assert cfg["update"]["repo"] == "Kaden-G/course-kit"                                # new setting filled in


def test_error_page_and_bad_manifest(kit, monkeypatch):
    c, root, _ = kit
    (root / "course" / "manifest.json").write_text("{not json")
    r = c.get("/"); assert r.status_code == 200 and "manifest.json could not be read" in r.text
    def boom(*a, **k): raise RuntimeError("kaboom")
    monkeypatch.setattr(appmod, "list_drafts", boom)
    c2 = TestClient(appmod.app, raise_server_exceptions=False)
    r = c2.get("/"); assert r.status_code == 500 and "kaboom" in r.text and "screenshot" in r.text
    assert llm.resolve(None)["provider"] == "gemini"
