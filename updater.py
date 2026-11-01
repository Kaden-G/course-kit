"""Self-update from GitHub Releases. The dashboard calls check() and apply(); nothing here touches
the user's own data: course/, drafts/, profile/, config.yaml and the browser profile are never replaced."""
from __future__ import annotations

import io
import json
import os
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

PROTECTED = {"course", "drafts", "profile", "config.yaml", ".browser-profile", "dist", ".git", "server.log"}
ASSET_NAME = "course-kit.zip"


class UpdateError(Exception):
    pass


def _get(url: str, token: str, accept: str = "application/vnd.github+json", timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "course-kit",
                                               **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise UpdateError("GitHub said no. Check the repo name and the update token in Settings.") from None
        raise UpdateError(f"GitHub returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise UpdateError(f"Could not reach GitHub: {e.reason}") from None


def normalize(tag: str) -> str:
    return tag.strip().lstrip("vV")


def check(repo: str, token: str, current: str, fetch=_get) -> dict:
    if not repo:
        raise UpdateError("No update repo set in Settings.")
    data = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest", token))
    latest = normalize(data.get("tag_name", ""))
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    return {"current": normalize(current), "latest": latest, "available": bool(asset) and latest != normalize(current),
            "notes": (data.get("body") or "").strip()[:2000], "asset_url": asset["url"] if asset else None,
            "published": data.get("published_at")}


def apply_zip(zip_bytes: bytes, root: Path, version: str) -> list[str]:
    """Unpack the release over root, skipping protected paths. Files are swapped in with os.replace so a
    running script keeps its old inode. Returns the list of files written."""
    written = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = Path(info.filename)
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                continue
            if rel.parts[0] in PROTECTED or rel.name == "VERSION":
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".upd-")
            with os.fdopen(fd, "wb") as f:
                f.write(z.read(info))
            if dest.suffix == "" and dest.name.endswith(".command") or dest.name.endswith(".command") or dest.suffix == ".sh":
                os.chmod(tmp, 0o755)
            elif dest.exists():
                os.chmod(tmp, os.stat(dest).st_mode & 0o777)
            os.replace(tmp, dest)
            written.append(str(rel))
    (root / "VERSION").write_text(normalize(version) + "\n")
    return written


def apply(repo: str, token: str, root: Path, fetch=_get) -> dict:
    info = check(repo, token, (root / "VERSION").read_text().strip() if (root / "VERSION").exists() else "0", fetch)
    if not info["asset_url"]:
        raise UpdateError("The latest release has no course-kit.zip attached.")
    blob = fetch(info["asset_url"], token, accept="application/octet-stream", timeout=300)
    files = apply_zip(blob, root, info["latest"])
    return {"version": info["latest"], "files": len(files)}
