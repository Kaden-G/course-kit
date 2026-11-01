"""Pure parsing functions for Moodle HTML. No network, so this is fully testable offline.

Handles both Moodle 3.x (li.activity .activityinstance .instancename) and
Moodle 4.x (li.activity .activityname a.aalink) course-page markup, plus the
3.8+ forum post markup and the older div.forumpost fallback.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as _md

MOD_RE = re.compile(r"\bmodtype_([a-z0-9_]+)\b")


@dataclass
class Module:
    cmid: int | None
    modname: str
    name: str
    url: str | None
    description_md: str = ""
    linked_from: str | None = None   # set when discovered via a link inside another item (tiles/label courses)
    chapterid: int | None = None     # for links to one chapter of a book


@dataclass
class Section:
    number: int
    name: str
    summary_md: str = ""
    modules: list[Module] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)   # (name, url) pluginfiles linked from section text

    def has(self, cmid: int | None) -> bool:
        return cmid is not None and any(m.cmid == cmid for m in self.modules)

    def to_dict(self):
        return asdict(self)


def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    for hidden in el.select(".accesshide, .sr-only"):
        hidden.decompose()
    return " ".join(el.get_text(" ", strip=True).split())


def _first(el: Tag, *selectors: str) -> Tag | None:
    """select_one with a comma list returns document order, not priority order; this is priority order."""
    for sel in selectors:
        found = el.select_one(sel)
        if found is not None:
            return found
    return None


def html_to_md(fragment: str | Tag | None) -> str:
    if fragment is None:
        return ""
    html = str(fragment)
    md = _md(html, heading_style="ATX", strip=["script", "style"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md


def cmid_from_url(url: str | None) -> int | None:
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    try:
        return int(qs.get("id", [None])[0])
    except (TypeError, ValueError):
        return None


def parse_course(html: str, base_url: str) -> list[Section]:
    """Return the section tree (sections -> modules) of a course/view.php page."""
    soup = BeautifulSoup(html, "html.parser")
    sections: list[Section] = []
    for li in soup.select("li.section"):
        number = None
        sid = li.get("id", "")
        m = re.match(r"section-(\d+)", sid)
        if m:
            number = int(m.group(1))
        elif li.get("data-number"):
            number = int(li["data-number"])
        if number is None:
            continue
        name_el = li.select_one(".sectionname, h3.sectionname, .section-title")
        name = li.get("data-sectionname") or _text(name_el) or f"Section {number}"
        summary = li.select_one(".summary, .summarytext")
        section = Section(number=number, name=name, summary_md=html_to_md(summary))
        for act in li.select("li.activity"):
            classes = " ".join(act.get("class", []))
            mm = MOD_RE.search(classes)
            modname = mm.group(1) if mm else (act.get("data-modname") or "unknown")
            link = act.select_one(".activityname a, .activityinstance a, a.aalink")
            if link is None and modname != "label":   # labels have no link of their own; links inside are content
                link = act.select_one("a[href*='/mod/']")
            url = urljoin(base_url, link["href"]) if link and link.get("href") else None
            name_el = act.select_one(".activityname, .instancename, .activitytitle")
            name = _text(name_el) or _text(link) or modname
            desc = act.select_one(".activity-altcontent, .contentafterlink, .description, .activity-description")
            cmid = cmid_from_url(url)
            if cmid is None and act.get("data-id"):
                try:
                    cmid = int(act["data-id"])
                except ValueError:
                    pass
            desc_md = html_to_md(desc)
            if modname == "unknown" and not url:
                continue   # theme/format placeholder, not an item
            if modname == "label" and not desc_md:
                continue   # empty label (spacer/image-only)
            section.modules.append(Module(cmid=cmid, modname=modname, name=name, url=url,
                                          description_md=desc_md))
        sections.append(section)
    return sections


def find_download_course_link(html: str, base_url: str) -> str | None:
    """Moodle 3.10+ 'Download course content' button, if the admin enabled it."""
    soup = BeautifulSoup(html, "html.parser")
    a = soup.select_one("a[href*='downloadcontent.php'], [data-downloadcourse] a, a[data-downloadcourse]")
    if a and a.get("href"):
        return urljoin(base_url, a["href"])
    return None


def course_id_from_html(html: str) -> int | None:
    """Any page inside a course carries body class 'course-<id>'; breadcrumb link is the fallback."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if body is not None:
        for c in body.get("class", []):
            m = re.fullmatch(r"course-(\d+)", c)
            if m:
                return int(m.group(1))
    a = soup.select_one("a[href*='course/view.php?id=']")
    return cmid_from_url(a["href"]) if a else None


def split_course_url(url: str) -> tuple[str, int | None]:
    """('https://host/moodle/', course_id_or_None) from any URL inside a Moodle course."""
    m = re.match(r"^(https?://.+?/)(?:course/|mod/|my/|user/)", url)
    base = m.group(1) if m else url.rstrip("/") + "/"
    cid = cmid_from_url(url) if "course/view.php" in url else None
    return base, cid


def sesskey_from_html(html: str) -> str | None:
    m = re.search(r'"sesskey"\s*:\s*"([A-Za-z0-9]+)"', html)
    return m.group(1) if m else None


def course_format_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if body is None:
        return None
    for c in body.get("class", []):
        if c.startswith("format-"):
            return c[len("format-"):]
    return None


def sections_from_state(state: dict, base_url: str) -> list[Section]:
    """Build the section tree from Moodle 4.x core_courseformat_get_state JSON."""
    cms = {}
    for cm in state.get("cm", []):
        try:
            cms[int(cm["id"])] = cm
        except (KeyError, ValueError, TypeError):
            continue
    out = []
    for sec in state.get("section", []):
        try:
            number = int(sec.get("number", sec.get("section")))
        except (TypeError, ValueError):
            continue
        s = Section(number=number, name=_html.unescape((sec.get("title") or sec.get("rawtitle") or f"Section {number}").strip()))
        for cmid in sec.get("cmlist", []):
            cm = cms.get(int(cmid))
            if not cm:
                continue
            modname = cm.get("module") or cm.get("modname") or "unknown"
            url = cm.get("url") or None
            if not url and modname not in ("label",):
                url = urljoin(base_url, f"mod/{modname}/view.php?id={cm['id']}")
            s.modules.append(Module(cmid=int(cm["id"]), modname=modname,
                                    name=_html.unescape((cm.get("name") or modname).strip()), url=url))
        out.append(s)
    out.sort(key=lambda x: x.number)
    return out


def is_logged_in(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if body is None:
        return False
    if "notloggedin" in body.get("class", []):
        return False
    return bool(soup.select_one(".usermenu, [data-region='user-menu'], #usermenu, .userbutton"))


def main_content(html: str) -> Tag | None:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ("div[role='main']", "#region-main", "section#region-main", "#page-content", "main"):
        el = soup.select_one(sel)
        if el:
            for junk in el.select("nav, .activity-navigation, .drawer, .secondary-navigation, .book_toc, "
                                  "script, style, form.form-inline, .breadcrumb, #goto-top-link"):
                junk.decompose()
            return el
    return soup.body


def parse_page(html: str) -> str:
    """Markdown of the main content of a mod/page, mod/book chapter, quiz intro, etc."""
    return html_to_md(main_content(html))


def parse_pluginfile_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(name, url) for every pluginfile link on the page (folders, assignments, intros)."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href*='pluginfile.php']"):
        href = urljoin(base_url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        out.append((_text(a) or href.rsplit("/", 1)[-1], href))
    return out


def parse_book_chapters(html: str, page_url: str) -> list[tuple[str, str, int | None]]:
    """(title, absolute url, chapterid) for each chapter in the book's table of contents."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select(".book_toc a[href*='chapterid='], .book_toc a[href*='mod/book/view.php']"):
        url = urljoin(page_url, a["href"])
        q = parse_qs(urlparse(url).query)
        try:
            chapterid = int(q.get("chapterid", [None])[0])
        except (TypeError, ValueError):
            chapterid = None
        if chapterid in seen:
            continue
        seen.add(chapterid)
        out.append((_text(a), url, chapterid))
    return out


def parse_forum_view(html: str, base_url: str) -> list[dict]:
    """Discussions listed on mod/forum/view.php."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href*='discuss.php?d=']"):
        href = urljoin(base_url, a["href"])
        d = parse_qs(urlparse(href).query).get("d", [None])[0]
        if not d or d in seen:
            continue
        seen.add(d)
        title = _text(a)
        if not title:
            continue
        out.append({"discussion_id": int(d), "title": title, "url": href.split("#")[0]})
    return out


def parse_forum_discussion(html: str) -> list[dict]:
    """Posts in a discussion thread. Works for Moodle 3.8+ articles and older div.forumpost."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    articles = soup.select("article[data-region='post'], article.forum-post-container")
    if articles:
        for art in articles:
            subject = _text(_first(art, "[data-region-content='forum-post-core-subject']", "h3", ".subject"))
            author = _text(_first(art, "header a[href*='user/view.php']", "address a", ".author a",
                                  "[data-region-content='forum-post-core-author'] a", "a[href*='user/view.php']", "address"))
            when = _text(_first(art, "time", ".time"))
            body = _first(art, "[data-region-content='forum-post-core-content'] .post-content-container",
                          ".post-content-container", ".post-content")
            posts.append({
                "post_id": art.get("data-post-id") or art.get("id", "").lstrip("p"),
                "parent_id": art.get("data-parent-id"),
                "subject": subject, "author": author, "time": when, "body_md": html_to_md(body),
            })
        return posts
    for div in soup.select("div.forumpost"):
        posts.append({
            "post_id": div.get("id", "").lstrip("p"),
            "parent_id": None,
            "subject": _text(div.select_one(".subject")),
            "author": _text(_first(div, ".author a", ".author")),
            "time": _text(div.select_one(".author")).split("-")[-1].strip(),
            "body_md": html_to_md(div.select_one(".content .posting, .posting, .content")),
        })
    return posts


MOD_LINK_RE = re.compile(r"/mod/([a-z0-9_]+)/view\.php\?(?:[^#]*&)?id=(\d+)")


def linked_modules(html: str, base_url: str) -> list[Module]:
    """Every activity linked from the text of a page (labels in tiles courses link to hidden books/forums/quizzes)."""
    root = main_content(html)
    if root is None:
        return []
    out, seen = [], set()
    for a in root.select("a[href]"):
        if "aalink" in a.get("class", []) or a.find_parent(class_=re.compile(r"activityname|activityinstance")):
            continue  # that's an item's own title link, not a link inside content
        href = urljoin(base_url, a["href"])
        m = MOD_LINK_RE.search(href)
        if not m:
            continue
        modname, cmid = m.group(1), int(m.group(2))
        q = parse_qs(urlparse(href).query)
        chapterid = None
        try:
            chapterid = int(q.get("chapterid", [None])[0])
        except (TypeError, ValueError):
            pass
        key = (cmid, chapterid)
        if key in seen:
            continue
        seen.add(key)
        # keep the chapter-less URL for books so the whole book gets fetched once; the chapter rides along
        url = urljoin(base_url, f"mod/{modname}/view.php?id={cmid}")
        name = _text(a) or a.get("title") or f"{modname} {cmid}"
        out.append(Module(cmid=cmid, modname=modname, name=name, url=url, chapterid=chapterid))
    return out


PLACEHOLDER_NAME_RE = re.compile(r"^[a-z0-9_]+ \d+$")


def page_title(html: str) -> str | None:
    """Best display name for a fetched page. Moodle's <title> is '{course shortname}: {activity name} | {site}',
    so take the activity name from there; fall back to the first heading in the content."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        t = soup.title.string.split("|")[0].strip()
        head, sep, rest = t.partition(": ")
        if sep and " " not in head.strip():   # 'MCCDET867127MI09NPLA: Module 9: ...' -> 'Module 9: ...'
            t = rest.strip()
        if t:
            return t
    root = main_content(html)
    if root is not None:
        t = _text(root.select_one("h1, h2, h3"))
        if t:
            return t
    return None


def host_allowed(url: str, base_url: str) -> bool:
    """Same host as the site, or a sibling host under the same parent domain (portal.x.mil next to elearning.x.mil)."""
    h, b = urlparse(url).hostname or "", urlparse(base_url).hostname or ""
    if not h or not b:
        return False
    if h == b:
        return True
    parent = b.split(".", 1)[1] if b.count(".") >= 2 else b
    return h == parent or h.endswith("." + parent)


def slug(s: str, n: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "untitled"
