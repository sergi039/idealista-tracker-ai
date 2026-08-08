"""Session-scoped guard against Config mutation that escapes a test.

`Config` (config.py) keeps several mutable containers as *class* attributes -
`DEFAULT_SCORING_WEIGHTS`, `SCORING_PROFILES`, `COMBINED_MIX` and friends. They
are shared by the whole process, so anything that binds one by reference and
mutates it in place pollutes every later consumer. #44 was exactly that bug:
`ScoringService.__init__` did `self.weights = Config.DEFAULT_SCORING_WEIGHTS`
and then `.update()`d it, permanently injecting the combined-mix rows
(`investment` / `lifestyle`) into the criterion set.

Nothing in the suite watched for it. `TestConfigWeightsNotMutated`
(tests/test_scoring_service.py) takes its `before` snapshot *inside* each test,
which correctly pins the source-level invariant but compares dirty against
dirty once an earlier module has already polluted the dict.

This guard snapshots the containers in `pytest_configure`, before any test body
runs, and compares in `pytest_sessionfinish`. Deliberately session-scoped, not
per-test: tests/test_security_and_scoring.py legitimately runs
`patch.object(Config, "SCORING_PROFILES", ...)` / `"COMBINED_MIX"` inside a
context manager that restores itself, and a per-test guard would flag that. Only
mutation that *escapes* a test is the failure mode worth catching.

Scope is deliberately mutable containers only. Scalar class attributes are
excluded: several modules assign `Config.AUTO_TRAVEL_ENRICHMENT = False`,
`Config.SALE_ONLY = True` and similar without restoring them, and those
rebindings are not the aliasing bug this guards.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

import pytest

from config import Config

# In-place mutation of one of these is the failure mode. Tuples/frozensets and
# scalars cannot be aliased and then mutated, so they are out of scope.
MUTABLE_TYPES: tuple[type, ...] = (dict, list, set)

# Values are pasted into the failure message; keep single lines readable.
MAX_VALUE_REPR = 120

_snapshot: dict[str, Any] | None = None
_problems: list[str] = []
_reported = False


def _mutable_config_attributes() -> dict[str, Any]:
    """Every mutable container Config owns, by attribute name.

    Deliberately not a hard-coded list: `SCORING_PROFILES` and `COMBINED_MIX`
    get the same protection as `DEFAULT_SCORING_WEIGHTS`, and so does any
    container added to config.py later.
    """
    return {
        name: value
        for name, value in vars(Config).items()
        if not name.startswith("__") and isinstance(value, MUTABLE_TYPES)
    }


def _fmt(value: Any) -> str:
    text = repr(value)
    if len(text) > MAX_VALUE_REPR:
        text = text[: MAX_VALUE_REPR - 3] + "..."
    return text


def _join(entries: Iterable[str]) -> str:
    return ", ".join(entries)


def _dict_changes(
    before: dict[Any, Any], after: dict[Any, Any], prefix: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Added / removed / changed keys, recursing into nested dicts.

    Nested mappings (SCORING_PROFILES) report dotted paths such as
    `investment.environment` so the message names the key that actually moved
    rather than dumping two whole sub-dicts.
    """
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []

    for key, value in after.items():
        if key not in before:
            added.append(f"{prefix}{key}={_fmt(value)}")

    for key, value in before.items():
        if key not in after:
            removed.append(f"{prefix}{key}={_fmt(value)}")
            continue
        new_value = after[key]
        if isinstance(value, dict) and isinstance(new_value, dict):
            sub_added, sub_removed, sub_changed = _dict_changes(
                value, new_value, prefix=f"{prefix}{key}."
            )
            added.extend(sub_added)
            removed.extend(sub_removed)
            changed.extend(sub_changed)
        elif value != new_value:
            changed.append(f"{prefix}{key}: {_fmt(value)} -> {_fmt(new_value)}")

    return added, removed, changed


def _describe(name: str, before: Any, after: Any) -> list[str]:
    """Human-readable detail lines for one mutated attribute."""
    lines = [f"Config.{name} was mutated during the test session:"]

    if isinstance(before, dict) and isinstance(after, dict):
        added, removed, changed = _dict_changes(before, after)
        if added:
            lines.append(f"  added keys:   {_join(added)}")
        if removed:
            lines.append(f"  removed keys: {_join(removed)}")
        if changed:
            lines.append(f"  changed keys: {_join(changed)}")
    elif isinstance(before, set) and isinstance(after, set):
        added_items = sorted(_fmt(item) for item in after - before)
        removed_items = sorted(_fmt(item) for item in before - after)
        if added_items:
            lines.append(f"  added items:   {_join(added_items)}")
        if removed_items:
            lines.append(f"  removed items: {_join(removed_items)}")
    else:
        lines.append(f"  before: {_fmt(before)}")
        lines.append(f"  after:  {_fmt(after)}")

    return lines


def _detect_mutations() -> list[str]:
    """Compare live Config containers against the snapshot."""
    if _snapshot is None:  # pragma: no cover - configure always runs first
        return []

    current = _mutable_config_attributes()
    problems: list[str] = []

    for name, before in _snapshot.items():
        if name not in current:
            problems.append(
                f"Config.{name} disappeared during the test session "
                f"(was {_fmt(before)})."
            )
            continue
        after = current[name]
        if before != after:
            problems.extend(_describe(name, before, after))

    for name in current:
        if name not in _snapshot:
            problems.append(
                f"Config.{name} appeared during the test session "
                f"(now {_fmt(current[name])})."
            )

    return problems


def _message() -> list[str]:
    return _problems + [
        "",
        "A test left a Config class attribute mutated. Copy the container "
        "before mutating it (see #44/#45), or restore it in the test.",
    ]


def pytest_configure(config: pytest.Config) -> None:
    """Snapshot the mutable Config containers before any test body runs."""
    global _snapshot
    if _snapshot is None:
        _snapshot = {
            name: copy.deepcopy(value)
            for name, value in _mutable_config_attributes().items()
        }


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run when a mutation escaped a test."""
    global _problems
    _problems = _detect_mutations()
    if not _problems:
        return

    # Raising here does not fail the run: wrap_session() has already computed
    # the exit status by the time this hook is called, and returns
    # session.exitstatus after it. Setting that attribute is what makes the
    # process exit non-zero. Never downgrade an already-failing status.
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Print the diagnosis next to the final stats line.

    Runs from inside the terminal reporter's own pytest_sessionfinish wrapper,
    i.e. after the warnings summary, so the message is the last thing before
    the pass/fail counts rather than buried above them.
    """
    global _reported
    if not _problems:
        return
    terminalreporter.write_sep(
        "=", "config mutation guard: FAILED", red=True, bold=True
    )
    for line in _message():
        terminalreporter.write_line(line, red=True)
    _reported = True


def pytest_unconfigure(config: pytest.Config) -> None:
    """Last resort when there is no terminal summary to write into."""
    if _problems and not _reported:  # pragma: no cover - needs -p no:terminal
        print("\n".join(["config mutation guard: FAILED", *_message()]))
