"""No answer about the state directory, no run (#639).

The deploy watcher, the merge bot, the autopilot loop and the issue runner
keep their logs, the deployment marker, the review journal and the stall alarm
in the directory the container mounts at /app/data, and they ask one reader
where that is: `tools/autopilot/lib/data_dir.sh`. This module pins what each
of them does when the reader does not say - it stops, before its first file
and before its first command. None falls back to `<repo>/data`: a watcher
keeping its markers in one directory while the container writes another is the
split the reader exists to prevent.

Each script runs for real, as a copy in a throwaway checkout, so the reader it
asks is the copy beside it and can be swapped for a broken one. The
environment is built from nothing rather than inherited: these scripts merge
pull requests and start agents, and a test that got past the refusal must find
only stubs that fail - no `gh`, no credentials, no lock of the real
deployment's.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTOPILOT = REPO_ROOT / "tools" / "autopilot"
READER = AUTOPILOT / "lib" / "data_dir.sh"

# script -> the arguments that take it as far as its first file.
SCRIPTS = {
    "deploy_watcher.sh": [],
    "merge_bot.sh": ["--dry-run"],
    "autopilot.sh": ["--dry-run"],
    "run_issue.sh": ["1"],
}

# Everything these scripts could reach for. Each stub records the call and
# fails, so a run that got past the reader stops at its first command.
STUBBED = ("gh", "git", "docker", "claude", "codex", "rx", "uv", "curl", "timeout")

TIMEOUT_SECONDS = 60


def _stage(tmp_path: Path, script: str) -> tuple[Path, Path]:
    """A throwaway checkout holding one script and the real reader."""
    checkout = tmp_path / "checkout"
    lib = checkout / "tools" / "autopilot" / "lib"
    lib.mkdir(parents=True)
    (lib / "data_dir.sh").write_text(
        READER.read_text(encoding="utf-8"), encoding="utf-8"
    )
    staged = checkout / "tools" / "autopilot" / script
    staged.write_text(
        (AUTOPILOT / script).read_text(encoding="utf-8"), encoding="utf-8"
    )
    return checkout, staged


def _run(tmp_path: Path, checkout: Path, staged: Path, script: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    for name in STUBBED:
        stub = bin_dir / name
        # The shebang at byte 0 (#284).
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\nexit 1\n')
        stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "AUTOPILOT_REPO_DIR": str(checkout),
        "AUTOPILOT_REPO_SLUG": "example/invalid",
        "AUTOPILOT_LOCK_DIR": str(tmp_path / "deploy.lock.d"),
        "AUTOPILOT_MERGE_LOCK_DIR": str(tmp_path / "merge.lock.d"),
        "AUTOPILOT_LOCK_DIR_MAIN": str(tmp_path / "main.lock.d"),
        "AUTOPILOT_WORKTREE_ROOT": str(tmp_path / "worktrees"),
        "AUTOPILOT_HEALTH_URL": "http://127.0.0.1:9/healthz",
    }
    result = subprocess.run(
        ["/bin/bash", str(staged), *SCRIPTS[script]],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
        cwd=tmp_path,
    )
    return result, calls


def _refuses(checkout: Path, tmp_path: Path) -> None:
    """docker-compose.yml does not read the variable yet, so naming it is
    refused. The directory it names exists: following it would be possible."""
    state = tmp_path / "state"
    state.mkdir()
    (checkout / ".env").write_text(f"DB_PASSWORD=x\nIDEALISTA_DATA_DIR={state}\n")


def _linked_worktree(checkout: Path, tmp_path: Path) -> None:
    (checkout / ".git").write_text("gitdir: /somewhere/.git/worktrees/issue-1\n")


def _missing(checkout: Path, tmp_path: Path) -> None:
    (checkout / "tools" / "autopilot" / "lib" / "data_dir.sh").unlink()


def _answers_nothing(checkout: Path, tmp_path: Path) -> None:
    (checkout / "tools" / "autopilot" / "lib" / "data_dir.sh").write_text("")


def _answers_a_relative_path(checkout: Path, tmp_path: Path) -> None:
    (checkout / "tools" / "autopilot" / "lib" / "data_dir.sh").write_text(
        "#!/bin/bash\necho data\n"
    )


NO_ANSWER = {
    "the reader refuses": _refuses,
    "a linked git worktree": _linked_worktree,
    "the reader is missing": _missing,
    "the reader answers nothing": _answers_nothing,
    "the reader answers a relative path": _answers_a_relative_path,
}


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("script", sorted(SCRIPTS))
@pytest.mark.parametrize("why", sorted(NO_ANSWER))
def test_without_an_answer_the_script_stops_before_it_touches_anything(
    tmp_path, script, why
):
    checkout, staged = _stage(tmp_path, script)
    NO_ANSWER[why](checkout, tmp_path)
    before = _tree(tmp_path)

    result, calls = _run(tmp_path, checkout, staged, script)

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "refusing to run" in result.stderr, result.stderr
    assert not calls.exists(), calls.read_text()
    # Nothing written: not in <checkout>/data on a guess, not in the directory
    # the variable names, not anywhere else under the run's own root.
    after = [p for p in _tree(tmp_path) if not p.startswith("bin")]
    assert after == before


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_with_an_answer_the_same_run_gets_as_far_as_its_first_file(tmp_path, script):
    """The control for the test above: the same staged copy, the same
    arguments and the same failing stubs, with a reader that answers. The run
    opens its log in `<checkout>/data` - so a refusal that left that directory
    absent stopped something that would otherwise have happened."""
    checkout, staged = _stage(tmp_path, script)

    result, _calls = _run(tmp_path, checkout, staged, script)

    assert "refusing to run" not in result.stderr, result.stderr
    assert (checkout / "data").is_dir(), (result.stdout, result.stderr)
