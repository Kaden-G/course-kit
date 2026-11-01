import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parse_moodle as pm

FX = Path(__file__).parent / "fixtures"
BASE = "https://elearning.example/"
def fx(n): return (FX / n).read_text()


def test_course_4x():
    secs = pm.parse_course(fx("course_view_4x.html"), BASE)
    assert [s.number for s in secs] == [0, 7]
    wk7 = secs[1]
    assert wk7.name == "Week 7: Logistics"
    types = [m.modname for m in wk7.modules]
    assert types == ["page", "resource", "forum", "quiz", "assign", "label", "url"]
    names = [m.name for m in wk7.modules]
    assert names[1] == "Reading: Sustainment"           # accesshide "File" stripped
    assert wk7.modules[1].cmid == 702
    assert wk7.modules[1].description_md == "Read chapters 3-4."
    assert wk7.modules[5].description_md.startswith("Baseline for Turn 6")
    assert secs[0].summary_md == "Welcome to the course."
    assert pm.find_download_course_link(fx("course_view_4x.html"), BASE).endswith("courseid=42")
    assert pm.is_logged_in(fx("course_view_4x.html"))


def test_course_3x():
    secs = pm.parse_course(fx("course_view_3x.html"), BASE)
    assert len(secs) == 1 and secs[0].number == 3 and secs[0].name == "Week 3"
    assert [(m.modname, m.name, m.cmid) for m in secs[0].modules] == [
        ("page", "Lecture Notes", 301), ("folder", "Handouts", 302)]
    assert secs[0].summary_md == "Week three summary."
    assert pm.find_download_course_link(fx("course_view_3x.html"), BASE) is None


def test_forum_view_dedup():
    d = pm.parse_forum_view(fx("forum_view.html"), BASE)
    assert [x["discussion_id"] for x in d] == [77, 78]
    assert d[0]["url"].endswith("d=77")


def test_forum_discussion_4x():
    posts = pm.parse_forum_discussion(fx("forum_discuss_4x.html"))
    assert len(posts) == 2
    assert posts[0]["author"] == "Prof Smith" and posts[0]["subject"] == "Week 7 Prompt"
    assert "**sustainment**" in posts[0]["body_md"]
    assert posts[1]["parent_id"] == "9001" and posts[1]["author"] == "Jane Classmate"
    assert "* fuel" in posts[1]["body_md"]


def test_forum_discussion_3x():
    posts = pm.parse_forum_discussion(fx("forum_discuss_3x.html"))
    assert len(posts) == 1
    assert posts[0]["author"] == "Bob Oldschool" and posts[0]["body_md"] == "Hello from 3.x"


def test_page_and_links():
    md = pm.parse_page(fx("page_view.html"))
    assert md.startswith("## Week 7 Overview")
    assert "prev next" not in md and "menu" not in md and "footer" not in md
    assert "*sustainment*" in md
    links = pm.parse_pluginfile_links(fx("page_view.html"), BASE)
    assert links == [("syllabus.pdf", "https://elearning.example/pluginfile.php/55/mod_page/content/1/syllabus.pdf")]
    assert not pm.is_logged_in(fx("page_view.html"))


def test_assign_links_dedup():
    links = pm.parse_pluginfile_links(fx("assign_view.html"), BASE)
    assert len(links) == 1 and links[0][0] == "wargame_turn7.xlsx"


def test_slug():
    assert pm.slug("Week 7: Logistics & Sustainment!") == "week-7-logistics-sustainment"


def test_course_id_and_base_from_any_url():
    html = fx("book_view.html")
    assert pm.course_id_from_html(html) == 42
    assert pm.course_id_from_html(html.replace("course-42", "course-x")) == 42   # breadcrumb fallback
    assert pm.course_id_from_html("<html><body></body></html>") is None
    base, cid = pm.split_course_url("https://elearning.mcele.usmc.mil/moodle/mod/book/view.php?id=602584&chapterid=176352")
    assert base == "https://elearning.mcele.usmc.mil/moodle/" and cid is None
    base, cid = pm.split_course_url("https://host/moodle/course/view.php?id=77")
    assert base == "https://host/moodle/" and cid == 77
    chapters = pm.parse_book_chapters(html, BASE + "mod/book/view.php?id=602584")
    assert [(c[0], c[2]) for c in chapters] == [("Ch 1", 1), ("Ch 2", 2)]
    assert chapters[1][1] == BASE + "mod/book/view.php?id=602584&chapterid=2"      # relative TOC link resolved against the book page


def test_page_title_fallback():
    assert pm.page_title(fx("module_page.html")) == "Module 1: MCDP 1 - Nature & Theory of War"   # course code stripped
    assert pm.page_title("<html><head><title>Readings | 8671 | CDET</title></head><body><div role='main'><p>x</p></div></body></html>") == "Readings"
    assert pm.page_title("<html><body><div role='main'><h2>Only a heading</h2></div></body></html>") == "Only a heading"
    assert pm.PLACEHOLDER_NAME_RE.match("page 602563") and not pm.PLACEHOLDER_NAME_RE.match("Module 1: MCDP 1")


def test_forum_author_from_by_line_and_address():
    posts = pm.parse_forum_discussion(fx("forum_discuss_4x.html"))
    assert [p["author"] for p in posts] == ["Prof Smith", "Jane Classmate"]


def test_linked_modules_keep_chapter():
    mods = pm.linked_modules(fx("module_page.html"), BASE)
    book = next(m for m in mods if m.modname == "book")
    assert book.chapterid == 2 and book.url == BASE + "mod/book/view.php?id=602584" and book.name == "Lesson 1 Reading"
    nxt = next(m for m in mods if m.modname == "page")
    assert nxt.cmid == 7402 and nxt.name == "page 7402"       # image link, no text


def test_host_allowed():
    base = "https://elearning.mcele.usmc.mil/moodle/"
    assert pm.host_allowed("https://elearning.mcele.usmc.mil/moodle/pluginfile.php/1/x.pdf", base)
    assert pm.host_allowed("https://portal.mcele.usmc.mil/libdl.html?f=4613792278EE", base)
    assert not pm.host_allowed("https://www.usmc.mil/doc.pdf", base)
    assert not pm.host_allowed("https://evil.example/mcele.usmc.mil/x", base)
