import io, json, sys, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import updater


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_apply_zip_replaces_code_and_protects_data(tmp_path):
    (tmp_path / "app.py").write_text("old")
    (tmp_path / "VERSION").write_text("8\n")
    (tmp_path / "config.yaml").write_text("model: {api_key: SECRET}")
    (tmp_path / "course").mkdir(); (tmp_path / "course" / "manifest.json").write_text("{}")
    (tmp_path / "drafts").mkdir(); (tmp_path / "drafts" / "d.json").write_text("{}")
    (tmp_path / "profile").mkdir(); (tmp_path / "profile" / "voice.md").write_text("mine")
    z = make_zip({"app.py": "new", "templates/home.html": "<h1>", "Course Kit.command": "#!/bin/bash\n", "VERSION": "9\n",
                  "config.yaml": "model: {api_key: ''}", "course/manifest.json": "{\"evil\": 1}", "profile/voice.md": "placeholder",
                  "../escape.txt": "x"})
    written = updater.apply_zip(z, tmp_path, "v9")
    assert (tmp_path / "app.py").read_text() == "new"
    assert (tmp_path / "templates" / "home.html").read_text() == "<h1>"
    assert (tmp_path / "VERSION").read_text().strip() == "9"
    assert (tmp_path / "config.yaml").read_text() == "model: {api_key: SECRET}"      # untouched
    assert (tmp_path / "course" / "manifest.json").read_text() == "{}"
    assert (tmp_path / "profile" / "voice.md").read_text() == "mine"
    assert not (tmp_path.parent / "escape.txt").exists()
    assert sorted(written) == ["Course Kit.command", "app.py", "templates/home.html"]
    import os, stat
    assert os.stat(tmp_path / "Course Kit.command").st_mode & stat.S_IXUSR


def test_check_and_apply_with_fake_github(tmp_path):
    (tmp_path / "VERSION").write_text("8\n"); (tmp_path / "app.py").write_text("old")
    release = {"tag_name": "v9", "body": "fixes", "published_at": "2026-11-01T00:00:00Z",
               "assets": [{"name": "course-kit.zip", "url": "https://api.github.com/repos/o/r/releases/assets/1"}]}
    calls = []
    def fake_fetch(url, token, accept="application/vnd.github+json", timeout=60):
        calls.append((url, token, accept))
        if url.endswith("/releases/latest"):
            return json.dumps(release).encode()
        if url.endswith("/assets/1"):
            assert accept == "application/octet-stream"
            return make_zip({"app.py": "new", "VERSION": "9\n"})
        raise AssertionError(url)
    info = updater.check("o/r", "tok", "8", fetch=fake_fetch)
    assert info["available"] and info["latest"] == "9" and info["current"] == "8" and info["notes"] == "fixes"
    assert calls[0][1] == "tok"
    res = updater.apply("o/r", "tok", tmp_path, fetch=fake_fetch)
    assert res["version"] == "9" and (tmp_path / "app.py").read_text() == "new" and (tmp_path / "VERSION").read_text().strip() == "9"
    # same version -> nothing available
    assert not updater.check("o/r", "tok", "9", fetch=fake_fetch)["available"]


def test_check_errors_are_friendly():
    with pytest.raises(updater.UpdateError):
        updater.check("", "", "8")
    import urllib.error
    def denied(url, token, accept="", timeout=60):
        raise updater.UpdateError("GitHub said no. Check the repo name and the update token in Settings.")
    with pytest.raises(updater.UpdateError, match="GitHub said no"):
        updater.check("o/r", "bad", "8", fetch=denied)
