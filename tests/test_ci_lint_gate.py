"""The ruff gate must live in CI, not only in one machine's tooling (#81).

Until this job existed, the only thing enforcing ruff was a PostToolUse hook
in an *untracked* `.claude/settings.json` on a single Mac, plus an opt-in
pre-push hook. Every other author - `codex/*` branches, the GitHub web
editor, a fresh clone - could land unlinted code, and nothing said so.

These tests pin the three properties that make the workflow job a real gate
instead of a decoration:

1. the workflow declares a job *named* `ruff`. That name is the status-check
   context branch protection refers to, so a silent rename would either stop
   the gate reporting or wedge every merge waiting for a context that never
   arrives;
2. it runs exactly the commands `tools/ci/local_ci.sh` runs, so a red CI run
   reproduces locally verbatim - the parity CLAUDE.md and CONTRIBUTING.md
   both already promise the reader;
3. ruff is a locked dev dependency, so `uv run ruff` is one pinned version
   for CI, the pre-push hook and the developer alike. That is not cosmetic:
   ruff's *default* rule set changed from 59 rules in 0.15.x to 413 in
   0.16.0, so an unpinned gate would pass or fail on the same code depending
   on whichever ruff happened to be on PATH.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCAL_GATE = REPO_ROOT / "tools" / "ci" / "local_ci.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

# Both halves of the lint gate. `ruff check` is the issue's requirement;
# `ruff format --check` is included because the local gate has always run it
# and the repository is clean under it, so leaving it out of CI would keep
# exactly the hole #81 is about.
RUFF_COMMANDS = ("uv run ruff check .", "uv run ruff format --check .")

# Single-line `run:` values only; a block scalar (`run: |`) is skipped, which
# is correct here - the no-source-bundles job is the only one that uses it.
_RUN_STEP = re.compile(r"^[ \t]*run:[ \t]*(?P<cmd>[^|>\s].*?)[ \t]*$", re.MULTILINE)


def _ci_run_commands() -> list[str]:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    return [match.group("cmd") for match in _RUN_STEP.finditer(text)]


def test_ci_declares_a_job_named_ruff():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^[ \t]+name: ruff$", text, re.MULTILINE), (
        "no job named 'ruff' in .github/workflows/ci.yml - that is the "
        "status-check context branch protection is configured against"
    )


def test_ci_runs_both_ruff_commands():
    commands = _ci_run_commands()
    missing = [cmd for cmd in RUFF_COMMANDS if cmd not in commands]
    assert not missing, (
        f"CI no longer runs {missing}; lint would be enforced only by the "
        f"local hooks again. Workflow commands found: {commands}"
    )


def test_ci_and_the_local_gate_run_the_same_ruff_commands():
    """A red CI run has to be reproducible with the documented local command."""
    gate = LOCAL_GATE.read_text(encoding="utf-8")
    missing = [cmd for cmd in RUFF_COMMANDS if cmd not in gate]
    assert not missing, (
        f"tools/ci/local_ci.sh no longer runs {missing}, so it stopped "
        "mirroring CI - CLAUDE.md and CONTRIBUTING.md promise it does"
    )


def test_ruff_is_a_locked_dev_dependency():
    """`uv run ruff` must resolve to the locked version, never to PATH."""
    assert re.search(
        r'^\s*"ruff[><=~]', PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE
    ), (
        "ruff is not declared in pyproject.toml's dev dependency group; "
        "`uv run ruff` would fall back to whatever is installed on PATH"
    )
    assert re.search(
        r'^name = "ruff"$', UV_LOCK.read_text(encoding="utf-8"), re.MULTILINE
    ), (
        "ruff is missing from uv.lock, so `uv sync --frozen` in CI would not "
        "install it and the version would stop being pinned"
    )
