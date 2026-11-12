"""The host path of the deployment's state has one home (issue #639).

The app container persists one directory, mounted at `/app/data`. On the host
it is spelled `./data` in docker-compose.yml and `${REPO_DIR}/data` in seven
scripts - eight copies of "inside the git working copy", which is the fact
that let a `pytest` run in the checkout overwrite the production mail cursor.

This is the first step out of that: one variable, `IDEALISTA_DATA_DIR` in the
checkout's `.env`. Compose interpolates the bind mount from it, and
`tools/autopilot/lib/data_dir.sh` is the one reader of it for everything else
on the host. Unset, both say `<repo>/data`, so nothing moves until a
machine's `.env` says so. The scripts are moved over to the reader in the
changes that follow this one.

Pinned here:

* the reader's contract, by running its shell harness (pytest is what CI
  runs; a harness nobody runs proves nothing);
* that Compose's default still renders the directory it always did;
* that the host variable can never be mistaken for the container's.
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

# Four watcher runs against stubs, three of which build and poll a loopback
# health stub that answers at once. The bound is there to fail a hang.
TIMEOUT_SECONDS = 300

VARIABLE = "IDEALISTA_DATA_DIR"


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
    which is the literal this line used to be - so a merge moves nothing."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    binds = [
        line.strip().removeprefix("- ").strip()
        for line in compose.splitlines()
        if line.strip().startswith("- ") and line.rstrip().endswith(":/app/data")
    ]
    assert binds == ["${IDEALISTA_DATA_DIR:-./data}:/app/data"], binds

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
        assert not any(re.search(r"(^|[\s\"'])\./data\b", line) for line in code), name


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
