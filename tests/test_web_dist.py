"""The built page in app/web/dist: present, built from the current web/ source, self-contained."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "app" / "web" / "dist"


def _index() -> str:
    assert (DIST / "index.html").exists(), "app/web/dist is missing: cd web && npm ci && npm run build"
    return (DIST / "index.html").read_text(encoding="utf-8")


def _source_hash() -> str:
    """Same hash as sourceHash() in web/vite.config.ts: every tracked file under web/, LF endings."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    r = subprocess.run(["git", "ls-files", "-z", "web/"], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        pytest.skip("not a git checkout")
    h = hashlib.sha256()
    for f in sorted(p for p in r.stdout.decode("utf-8").split("\0") if p):
        h.update(f.encode("utf-8") + b"\0")
        h.update((ROOT / f).read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def test_page_is_built_from_the_current_source():
    m = re.search(r'<meta name="source-hash" content="([0-9a-f]+)">', _index())
    assert m, "app/web/dist/index.html has no source-hash: rebuild with cd web && npm run build"
    assert m.group(1) == _source_hash(), "web/ changed since the last build: cd web && npm run build"


def test_page_keeps_what_runs_before_first_paint():
    html = _index()
    assert 'localStorage.getItem("theme")' in html and "try {" in html
    assert '<meta name="author" content="Darshan Singh, Crosshire' in html
    assert '<meta name="copyright" content="Crosshire">' in html
    assert '<link rel="icon" href="data:,">' in html
    assert "<title>Crosshire</title>" in html  # the server swaps in the settings brand


def test_page_loads_nothing_from_other_sites():
    html = _index()
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs
    for ref in refs:
        if ref == "data:,":
            continue
        assert not re.match(r"^(https?:)?//", ref), ref
        assert (DIST / ref.lstrip("/")).exists(), ref
    css = "".join(p.read_text(encoding="utf-8") for p in (DIST / "assets").glob("*.css"))
    assert "fonts.googleapis.com" not in css and "fonts.gstatic.com" not in css
