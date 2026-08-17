"""Root-level data files never enter the image (#368).

#327 aligned `.dockerignore` with `.gitignore`. Its complement is a file that
is neither tracked nor ignored: git shows `??`, mirroring `.gitignore` cannot
catch it, and `COPY . .` takes it. Measured 2026-08-16 — `irpf2022.html`
(2.4 MB) and `planos.html` sat in the repository root for a day and entered
every build made from that tree.

The fix is a handful of root-only patterns. Docker's `.dockerignore` matching
is Go `filepath.Match` against the path relative to the context root, and `*`
never crosses `/`, so `*.html` excludes `irpf2022.html` and leaves
`templates/*.html` alone. That is the property this file pins: the patterns are
present, they are root-only (no `**`), and nothing in the file excludes the
directories the app is served from.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

DOCKERIGNORE = Path(__file__).resolve().parents[1] / ".dockerignore"

# Extensions a session or a human leaves next to the code and the image never
# needs at the root. Add here when the list in .dockerignore grows.
ROOT_DATA_PATTERNS = (
    "*.html",
    "*.json",
    "*.csv",
    "*.txt",
    "*.pdf",
    "*.xls",
    "*.xlsx",
    "*.docx",
)

# What the image is built from at the root and in the served directories.
MUST_SURVIVE = (
    "app.py",
    "main.py",
    "config.py",
    "models.py",
    "pyproject.toml",
    "uv.lock",
    "templates/properties.html",
    "templates/property_detail.html",
    "static/css/style.css",
    "static/js/main.js",
    "data/ine_municipal.json",  # excluded by `data/` on purpose, mounted at runtime
)


def _patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _root_only_match(pattern: str, path: str) -> bool:
    """Docker semantics for a pattern without `**`: root-relative, `*` stops at `/`."""
    if "**" in pattern:
        raise AssertionError(
            f"{pattern!r} is not root-only; this helper models root-only patterns"
        )
    if "/" in path.strip("/") and "/" not in pattern:
        return False
    return fnmatch.fnmatchcase(path, pattern)


def test_root_data_patterns_are_present_and_root_only():
    patterns = _patterns()
    for pattern in ROOT_DATA_PATTERNS:
        assert pattern in patterns, f"{pattern} missing from .dockerignore"
        assert "**" not in pattern


def test_the_measured_files_are_excluded():
    patterns = _patterns()
    for stray in (
        "irpf2022.html",
        "planos.html",
        "export.csv",
        "notes.txt",
        "dump.json",
    ):
        assert any(_root_only_match(p, stray) for p in patterns if "**" not in p), (
            f"{stray} would ride into the image"
        )


def test_the_build_inputs_are_not_excluded():
    """The root patterns must not reach templates/, static/ or the root sources."""
    patterns = [p for p in _patterns() if "**" not in p]
    for needed in MUST_SURVIVE:
        if needed.startswith("data/"):
            continue  # `data/` is excluded deliberately (runtime mount), not by these patterns
        hits = [p for p in patterns if _root_only_match(p, needed)]
        assert not hits, f"{needed} would be excluded by {hits}"
    # `templates/x.html` is not `x.html`: the root pattern must not match it.
    assert not _root_only_match("*.html", "templates/properties.html")
    assert _root_only_match("*.html", "irpf2022.html")
