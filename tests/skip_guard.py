"""Refuse a skip nobody approved (issue #314, retrospective on #297/#306/#309/#310).

A skipped test reports success. That is the whole problem: five times in one
day a defect survived because the thing meant to catch it could not fail, and
the last of them -- the network guard shipped in #308 -- hid in exactly this
shape. Every test in `tests/test_network_guard.py` is gated on
`skipif(not network_guard.installed())`, so dropping the one `install()` call
in `tests/conftest.py` turned all 29 of them into skips: measured on 12dee8c,
`29 skipped in 0.03s`, exit 0. The only trace was a skip count, and nothing
reads a skip count -- not `.github/workflows/ci.yml` (`pytest -v`, exit status
only), not `tools/ci/local_ci.sh`.

So this guard pins *which* modules may skip and *why*. A skip from a module
nobody listed, or for a reason nobody listed, fails the session and names the
test. Adding a genuinely conditional test costs one line here, on purpose: the
friction is the point, because the alternative is a number that silently grows.

What this catches, and what it does not:

* It catches a module that *starts* skipping -- a mechanism switched off, an
  import guard swallowing a new dependency, a `skipif` whose condition rotted
  into always-true.
* It does **not** catch an approved skip firing for the wrong reason: a
  `skipif` cannot say whether `PYTEST_ALLOW_NETWORK` was set deliberately or
  the guard simply failed to install, because the reason text is the same
  either way. `tests/test_network_guard_is_installed.py` is what answers that
  question, and the two are meant to be read together.

Entries that never fire are tolerated rather than reported as rot, because the
skip set legitimately differs between here and CI: `tests/test_postgres_
migrations.py` skips locally without `TEST_DATABASE_URL_POSTGRES` and runs in
CI, which sets it along with `REQUIRE_POSTGRES_TESTS=1`. Measured on 7c96493:
20 skips locally, 0 in CI, from the same tree.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# module path -> the reason substrings that module is allowed to skip for.
# A reason is matched as a substring so line numbers and interpolated paths in
# the message do not have to be repeated here.
ALLOWED: Dict[str, Tuple[str, ...]] = {
    "tests/test_postgres_migrations.py": (
        "set TEST_DATABASE_URL_POSTGRES to a throwaway PostgreSQL server",
    ),
    "tests/test_inflight_marker.py": ("no /proc here",),
    # The escape hatch of the network guard (#307/#308). Deliberate, and
    # indistinguishable from an accidental one by its text -- see the module
    # docstring above and tests/test_network_guard_is_installed.py.
    "tests/test_network_guard.py": ("the network guard is switched off",),
    "tests/test_network_guard_is_installed.py": ("the guard is off by request",),
}

_offenses: List[Tuple[str, str, str]] = []

_SKIPPED_PREFIX = re.compile(r"^Skipped:\s*", re.IGNORECASE)


def reset() -> None:
    _offenses.clear()


def is_allowed(module_path: str, reason: str) -> bool:
    """Whether this module is allowed to skip for this reason."""
    allowed = ALLOWED.get(module_path)
    if not allowed:
        return False
    return any(fragment in reason for fragment in allowed)


def note_skip(nodeid: str, module_path: str, reason: str) -> None:
    """Record a skip, unless the pin above accounts for it."""
    reason = _SKIPPED_PREFIX.sub("", (reason or "").strip())
    if is_allowed(module_path, reason):
        return
    _offenses.append((nodeid, module_path, reason))


def offenses() -> List[Tuple[str, str, str]]:
    return list(_offenses)


def summary_lines() -> List[str]:
    """What to print when the session failed on an unapproved skip."""
    if not _offenses:
        return []
    lines = [
        "A skipped test reports success, so a skip nobody approved fails the run.",
        "Either the mechanism under test is switched off -- which is the defect "
        "this guard exists for -- or the skip is legitimate and belongs in "
        "ALLOWED in tests/skip_guard.py, one line, deliberately.",
        "",
    ]
    for nodeid, module_path, reason in _offenses:
        lines.append(f"  {nodeid}")
        lines.append(f"      module: {module_path}")
        lines.append(f"      reason: {reason or '(none given)'}")
    return lines
