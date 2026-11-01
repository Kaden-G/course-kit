"""A fake Moodle built from the fixtures, so fetch_moodle.py can be exercised end to end offline."""
import threading, zipfile, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

FX = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.4\n%fake\n"
XLSX = b"PK\x03\x04fake-xlsx"

ROUTES_HTML = {
    ("/course/view.php", "42"): "course_view_4x.html",
    ("/mod/page/view.php", "701"): "page_view.html",
    ("/mod/forum/view.php", "501"): "forum_view.html",
    ("/mod/forum/view.php", "703"): "forum_view.html",
    ("/mod/quiz/view.php", "704"): "page_view.html",
    ("/mod/assign/view.php", "705"): "assign_view.html",
    ("/mod/book/view.php", "602584"): "book_view.html",
    ("/mod/page/view.php", "7401"): "module_page.html",
    ("/mod/page/view.php", "7402"): "module_page_2.html",
    ("/course/view.php", "43"): "course_view_hollow.html",   # tiles-style: sections, no items, state api works
    ("/course/view.php", "44"): "course_view_hollow.html",   # same, but state api disabled -> section pages
}


class H(BaseHTTPRequestHandler):
    base = "http://127.0.0.1:0/"
    hits = []
    state_api_disabled = False

    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        H.hits.append("POST " + self.path)
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        if u.path == "/lib/ajax/service.php":
            if q.get("sesskey") != ["AbC123xyz"] or H.state_api_disabled or b'"courseid": 44' in body or b'"courseid":44' in body:
                return self._send(b'[{"error": true, "exception": {"message": "Invalid parameter"}}]', "application/json")
            state = (FX / "course_state.json").read_text().replace("https://elearning.example/", H.base)
            import json
            return self._send(json.dumps([{"error": False, "data": state}]).encode(), "application/json")
        self.send_response(404); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        H.hits.append(self.path)
        key = (u.path, q.get("id", [None])[0])
        if u.path == "/mod/book/view.php":
            ch = q.get("chapterid", ["1"])[0]
            html = (FX / "book_view.html").read_text().replace("https://elearning.example/", H.base).replace("__CH__", ch)
            return self._send(html.encode())
        if u.path == "/libdl.html":
            return self._send(b"<html><body><script>location.href='/libfile/rubric.pdf'</script>waiting</body></html>")
        if u.path.startswith("/libfile/"):
            return self._send(PDF, "application/pdf", {"Content-Disposition": 'attachment; filename="Participation Rubric.pdf"'})
        if u.path == "/course/view.php" and q.get("section"):
            n = q["section"][0]
            if key[1] == "43" and n == "4":
                fx = "tiles_section_4.html"
            elif key[1] == "44":
                fx = "course_view_4x.html"      # one-section-per-page course: whole 4x page (sections 0 and 7)
            else:
                fx = "tiles_section_empty.html"
            html = (FX / fx).read_text().replace("https://elearning.example/", H.base).replace("__N__", n)
            return self._send(html.encode())
        if u.path == "/mod/url/view.php":
            target = {"707": H.base + "pluginfile.php/70/mod_url/content/rubric.pdf",
                      "708": "https://external.example/doc",
                      "709": H.base + "libdl.html?f=ABC"}.get(q.get("id", [""])[0])
            if target:
                self.send_response(303); self.send_header("Location", target); self.end_headers(); return
        if key in ROUTES_HTML:
            html = (FX / ROUTES_HTML[key]).read_text().replace("https://elearning.example/", H.base)
            return self._send(html.encode())
        if u.path == "/mod/forum/discuss.php":
            html = (FX / "forum_discuss_4x.html").read_text().replace("https://elearning.example/", H.base)
            return self._send(html.encode())
        if u.path == "/mod/resource/view.php":
            return self._send(PDF, "application/pdf", {"Content-Disposition": 'attachment; filename="sustainment.pdf"'})
        if u.path.startswith("/pluginfile.php/"):
            name = u.path.rsplit("/", 1)[-1]
            return self._send(XLSX if name.endswith("xlsx") else PDF, "application/octet-stream")
        if u.path == "/course/downloadcontent.php":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("hello.txt", "hi")
            return self._send(buf.getvalue(), "application/zip", {"Content-Disposition": 'attachment; filename="course.zip"'})
        self.send_response(404); self.end_headers()


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    H.base = f"http://127.0.0.1:{srv.server_port}/"
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    return srv, H.base
