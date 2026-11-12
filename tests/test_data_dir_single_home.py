"""The host path of the deployment's state has exactly one home (issue #639).

The app container persists one directory, mounted at `/app/data`. On the host
it was spelled `./data` in docker-compose.yml and `${REPO_DIR}/data` in seven
scripts - the deploy watcher, the merge bot, the issue runner, the backfill
status and supervisor tools, the attachment backup. Eight copies of "inside
the git working copy", which is the fact that let a `pytest` run in the
checkout overwrite the production mail cursor.

It is one variable now, `IDEALISTA_DATA_DIR` in the checkout's `.env`:
Compose interpolates the bind mount from it and every host tool asks
`tools/autopilot/lib/data_dir.sh`. Unset, both say `<repo>/data`, so nothing
moves until a machine's `.env` says so.

Three things are pinned here:

* the reader's contract and the watcher's use of it, by running the two shell
  harnesses (pytest is what CI runs; a harness nobody runs proves nothing);
* that Compose's default still renders the directory it always did;
* that no script has grown a ninth copy of the path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "tools" / "autopilot" / "lib" / "data_dir.sh"
LIB_TEST = REPO_ROOT / "tools" / "autopilot" / "lib" / "data_dir_test.sh"
WATCHER_TEST = REPO_ROOT / "tools" / "autopilot" / "deploy_data_dir_test.sh"

# Four watcher runs against stubs, three of which build and poll a loopback
# health stub that answers at once. The bound is there to fail a hang.
TIMEOUT_SECONDS = 300

VARIABLE = "IDEALISTA_DATA_DIR"

# Every host script that used to spell the directory itself.
CALLERS = (
    "tools/autopilot/deploy_watcher.sh",
    "tools/autopilot/merge_bot.sh",
    "tools/autopilot/autopilot.sh",
    "tools/autopilot/run_issue.sh",
    "tools/backfill_status.sh",
    "tools/backfill_supervisor.sh",
    "tools/backup_attachments.sh",
)

# `${REPO_DIR}/data`, `"$REPO_ROOT"/data`, `$project_dir/data` and friends:
# a repository-root variable followed by the directory's old name.
HAND_BUILT_PATH = re.compile(
    r"\$\{?(?:REPO_DIR|REPO_ROOT|REPO|repo_root|repo_dir|project_dir)\}?\"?/data\b"
)


def _interpreters() -> list[str]:
    """/bin/bash, plus any different bash this machine also has.

    The LaunchAgent execs /bin/bash (3.2.57 on the owner's Macs); the CI
    runner's /bin/bash is a bash 5. The reader is all parameter expansion and
    `case` patterns, which is exactly where the two disagree.
    """
    interpreters = ["/bin/bash"]
    other = shutil.which("bash")
    if other and os.path.realpath(other) != os.path.realpath("/bin/bash"):
        interpreters.append(other)
    return interpreters


def _environment(**extra: str) -> dict[str, str]:
    """This process's environment without the variable under test: a shell
    that exported it would otherwise decide every scenario."""
    env = {key: value for key, value in os.environ.items() if key != VARIABLE}
    env.update(extra)
    return env


@pytest.mark.parametrize("interpreter", _interpreters())
def test_the_one_reader_of_the_state_directory(interpreter):
    result = subprocess.run(
        ["/bin/bash", str(LIB_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
        env=_environment(DATA_DIR_TEST_BASH=interpreter),
    )
    assert result.returncode == 0, (
        "tools/autopilot/lib/data_dir.sh no longer answers where the "
        "deployment's state lives the way Compose reads it\n"
        f"run by: {interpreter}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ALL " in result.stdout and " PASSED" in result.stdout


def test_the_watcher_keeps_its_files_where_the_state_is():
    for tool in ("git", "curl", "nc", "python3"):
        assert shutil.which(tool), f"the harness needs {tool} on PATH"
    result = subprocess.run(
        ["/bin/bash", str(WATCHER_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
        env=_environment(),
    )
    assert result.returncode == 0, (
        "deploy_watcher.sh and the state directory came apart: either the "
        "default is no longer <repo>/data, or a configured directory did not "
        "take every one of the watcher's files with it\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout.count("OK: ") == 4, result.stdout


def _resolve(repo: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(LIB), str(repo)],
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(**extra),
    )


def test_this_checkout_resolves_to_its_own_data_directory(tmp_path):
    """The default, asked the way every caller asks it. A stand-in root, not
    this checkout: the main checkout's `.env` is allowed to say otherwise."""
    result = _resolve(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path}/data\n"


def test_compose_mounts_the_variable_and_defaults_to_the_old_literal():
    """`${IDEALISTA_DATA_DIR:-./data}` renders `./data` when nothing is set,
    which is the literal this line used to be - so a merge moves nothing.

    Long form, for `create_host_path: false`: the short form creates a missing
    source as an empty directory and starts the app on it, so a typo in the
    variable would be a deployment with no mail cursor. Measured on Compose
    v5.7 against a throwaway project: with the flag, a missing source is
    "bind source path does not exist" and nothing is created.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    code = [
        line.strip()
        for line in compose.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    at = code.index("target: /app/data")
    assert code[at - 2 : at + 3] == [
        "- type: bind",
        "source: ${IDEALISTA_DATA_DIR:-./data}",
        "target: /app/data",
        "bind:",
        "create_host_path: false",
    ], code[at - 2 : at + 3]
    # The short form is gone: it is the one that creates a missing source.
    assert not any(line.endswith(":/app/data") for line in code)

    # Nothing else in either Compose file mounts the state directory or names
    # the checkout's data/ by hand.
    for name in ("docker-compose.yml", "docker-compose.dev.yml"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        code = [
            line
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert sum("/app/data" in line for line in code) == (
            1 if name == "docker-compose.yml" else 0
        ), name
        hand_written = [
            line
            for line in code
            if re.search(r"(^|[\s\"'])\./data\b", line)
            and "${IDEALISTA_DATA_DIR:-./data}" not in line
        ]
        assert hand_written == [], name


def test_the_container_side_is_not_fed_the_host_path():
    """`.env` is the app service's env_file, and config.py reads `DATA_DIR`.
    The host variable must stay a different name, or a host path becomes the
    directory the app writes inside the container."""
    assert VARIABLE != "DATA_DIR"
    config = (REPO_ROOT / "config.py").read_text(encoding="utf-8")
    assert VARIABLE not in config
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    environment_lines = [
        line.strip()
        for line in compose.splitlines()
        if line.strip().startswith("- ") and "=" in line and "DATA_DIR" in line
    ]
    assert environment_lines == [], environment_lines


@pytest.mark.parametrize("script", CALLERS)
def test_every_host_tool_asks_the_one_reader(script):
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "lib/data_dir.sh" in code, (
        f"{script} no longer asks tools/autopilot/lib/data_dir.sh where the "
        "state directory is"
    )


def test_no_script_builds_the_path_by_hand():
    """A ninth copy would be a tool that keeps looking in `<repo>/data` after
    the mount has moved: a backup of an empty directory, a watcher that reads
    no in-flight markers."""
    offenders: list[str] = []
    roots = [REPO_ROOT / "tools", REPO_ROOT / ".githooks"]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.endswith("_test.sh"):
                continue
            if path.suffix not in {".sh", ""}:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, start=1):
                if line.lstrip().startswith("#"):
                    continue
                if HAND_BUILT_PATH.search(line):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
                    )
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize(
    "line",
    [
        'LOG_FILE="${AUTOPILOT_LOG:-${REPO_DIR}/data/autopilot.log}"',
        'for marker in "${REPO_ROOT}"/data/.inflight/*.json; do',
        'attachments_dir="${ATTACHMENTS_DIR:-$project_dir/data/attachments}"',
        'LOCK_ROOT="$REPO_ROOT/data"',
    ],
)
def test_the_pattern_recognises_the_spellings_that_were_removed(line):
    """The scan above is only as good as its pattern; these are the lines it
    exists to keep out, verbatim from before the change."""
    assert HAND_BUILT_PATH.search(line)


@pytest.mark.parametrize(
    "line",
    [
        'LOG_FILE="${AUTOPILOT_LOG:-${STATE_DIR}/autopilot.log}"',
        'RUN_LOG="${RUN_LOG:-data/${MODULE_TAIL}_supervised.log}"',
        'WORKTREE_ROOT="${AUTOPILOT_WORKTREE_ROOT:-${REPO_DIR}/.claude/worktrees}"',
        'cp "$REPO_ROOT/database.yml" .',
    ],
)
def test_the_pattern_leaves_other_paths_alone(line):
    assert not HAND_BUILT_PATH.search(line)
