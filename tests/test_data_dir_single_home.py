"""The host path of the deployment's state gets one home (issue #639).

The app container persists one directory, mounted at `/app/data`. On the host
it is spelled `./data` in docker-compose.yml and `${REPO_DIR}/data` in seven
scripts - eight copies of "inside the git working copy", which is the fact
that let a `pytest` run in the checkout overwrite the production mail cursor.

The way out starts with one reader, `tools/autopilot/lib/data_dir.sh`, that the
seven scripts ask instead of spelling the path. In this commit it answers
`<repo>/data` and nothing else, because that is what docker-compose.yml binds;
it parses neither that file nor `.env`. `IDEALISTA_DATA_DIR`, the name the
directory will later be configured under, is refused wherever it is named:
Compose does not read it yet, so following it would point the host tools at a
directory the app is not using.

Pinned here:

* the reader's contract, by running its shell harness (pytest is what CI
  runs; a harness nobody runs proves nothing);
* against Compose itself (`docker compose config`): this commit's
  docker-compose.yml mounts `<checkout>/data` whether or not the variable is
  set, and over a range of compose files the reader never names a directory
  other than the one Compose would mount;
* that the host variable can never be mistaken for the container's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "tools" / "autopilot" / "lib" / "data_dir.sh"
LIB_TEST = REPO_ROOT / "tools" / "autopilot" / "lib" / "data_dir_test.sh"

# The harness is a few dozen runs of a short shell script. The bound is there
# to fail a hang.
TIMEOUT_SECONDS = 300

VARIABLE = "IDEALISTA_DATA_DIR"


def _interpreters() -> list[str]:
    """/bin/bash, plus any different bash this machine also has.

    The LaunchAgent execs /bin/bash (3.2.57 on the owner's Macs); the CI
    runner's /bin/bash is a bash 5. The reader is parameter expansion and
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
        "deployment's state lives, or follows a variable Compose does not read\n"
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


def test_a_checkout_resolves_to_its_own_data_directory(tmp_path):
    """Asked the way every caller will ask it. A stand-in root, not this
    checkout: the main checkout's `.env` is not this test's to read."""
    result = _resolve(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path}/data\n"


# --- against Compose itself ---------------------------------------------------


def _docker_compose_or_skip() -> None:
    probe = (
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=30
        )
        if shutil.which("docker")
        else None
    )
    if probe is not None and probe.returncode == 0:
        return
    # CI is where this agreement is enforced; a skip there would be a green
    # run that compared nothing.
    assert not os.environ.get("CI"), "CI must have the docker compose plugin"
    pytest.skip("docker compose is not installed")


def _stand_in_checkout(root: Path, compose: str, env_line: str | None = None) -> Path:
    """A directory Compose and the reader can both be pointed at: the given
    docker-compose.yml and a `.env` that holds nothing of this machine's."""
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / "data").mkdir()
    (checkout / "docker-compose.yml").write_text(compose, encoding="utf-8")
    lines = ["DB_PASSWORD=not-a-secret"]
    if env_line is not None:
        lines.append(env_line)
    (checkout / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checkout


def _what_compose_mounts(checkout: Path, **extra: str) -> str:
    """The host directory Compose binds at /app/data for the app service, read
    from `docker compose config`. Offline: `config` renders the file, starts
    nothing and pulls nothing. Called the way the deploy watcher calls
    Compose: from the checkout, with `-f docker-compose.yml`."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=checkout,
        env=_environment(**extra),
    )
    assert result.returncode == 0, result.stderr
    volumes = json.loads(result.stdout)["services"]["app"]["volumes"]
    sources = [v["source"] for v in volumes if v.get("target") == "/app/data"]
    assert len(sources) == 1, volumes
    return sources[0]


def _same_directory(left: str | Path, right: str | Path) -> bool:
    return os.path.realpath(left) == os.path.realpath(right)


def _how(state: Path, how: str) -> tuple[str | None, dict[str, str]]:
    """The `.env` line and the environment for one way of setting the variable."""
    env_line = f"{VARIABLE}={state}" if how in ("env file", "both") else None
    extra = {VARIABLE: str(state)} if how in ("environment", "both") else {}
    return env_line, extra


def test_compose_mounts_the_checkouts_data_directory_and_the_reader_names_it(
    tmp_path,
):
    """The real docker-compose.yml, nothing set: Compose and the reader name
    the same directory, `<checkout>/data`."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    checkout = _stand_in_checkout(tmp_path, compose)

    mounted = _what_compose_mounts(checkout)
    answer = _resolve(checkout)
    assert answer.returncode == 0, answer.stderr
    assert _same_directory(mounted, checkout / "data"), mounted
    assert _same_directory(answer.stdout.strip(), mounted)


@pytest.mark.parametrize("how", ["env file", "environment", "both"])
def test_the_variable_moves_nothing_for_compose_and_the_reader_refuses_it(
    tmp_path, how
):
    """The real docker-compose.yml with the variable set to a directory that
    exists. Compose still mounts `<checkout>/data` - that is the proof that
    the state is there whatever the variable says - and the reader gives no
    directory at all rather than the variable's."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    env_line, extra = _how(state, how)
    checkout = _stand_in_checkout(tmp_path, compose, env_line)

    assert _same_directory(_what_compose_mounts(checkout, **extra), checkout / "data")
    refused = _resolve(checkout, **extra)
    assert refused.returncode == 1, refused.stdout
    assert refused.stdout == ""
    assert "does not read the variable" in refused.stderr
    assert str(state) not in refused.stderr


# Compose files that say different things about the variable. The first is the
# reviewer's input from #665: the real bind is the literal, the variable is
# only in a comment after it. A reader that looked for the variable's name in
# the file took this for a mount.
_SHAPES = {
    "literal bind, variable in an inline comment": """\
services:
  app:
    image: busybox
    volumes:
      - ./data:/app/data # ${IDEALISTA_DATA_DIR:-./data}
""",
    "quoted literal bind, variable in a comment": """\
services:
  app:
    image: busybox
    volumes:
      - "./data:/app/data"  # was ${IDEALISTA_DATA_DIR}
""",
    "long-form literal bind, variable in a comment": """\
services:
  app:
    image: busybox
    volumes:
      - type: bind
        source: ./data  # ${IDEALISTA_DATA_DIR:-./data}
        target: /app/data
""",
    "variable mounted at another target of the app": """\
services:
  app:
    image: busybox
    volumes:
      - ./data:/app/data
      - ${IDEALISTA_DATA_DIR:-./data}/logs:/app/logs
""",
    "variable mounted by another service": """\
services:
  app:
    image: busybox
    volumes:
      - ./data:/app/data
  worker:
    image: busybox
    volumes:
      - ${IDEALISTA_DATA_DIR:-./data}:/app/data
""",
    "literal bind through an anchor, variable in a label": """\
x-state: &state
  - ./data:/app/data
services:
  app:
    image: busybox
    labels:
      note: "state used to come from ${IDEALISTA_DATA_DIR:-./data}"
    volumes: *state
""",
    "variable mounted, short form": """\
services:
  app:
    image: busybox
    volumes:
      - ${IDEALISTA_DATA_DIR:-./data}:/app/data
""",
    "variable mounted, long form": """\
services:
  app:
    image: busybox
    volumes:
      - type: bind
        source: ${IDEALISTA_DATA_DIR:-./data}
        target: /app/data
""",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("how", ["nothing set", "env file", "environment", "both"])
def test_the_reader_never_names_a_directory_compose_would_not_mount(
    tmp_path, shape, how
):
    """The question a reviewer put to this reader: on which input does it
    agree to a directory the app does not mount? For every compose file above
    and every way of setting the variable, either the reader gives no
    directory, or it gives the one `docker compose config` binds at /app/data.
    """
    _docker_compose_or_skip()
    state = tmp_path / "state"
    state.mkdir()
    env_line, extra = _how(state, how)
    checkout = _stand_in_checkout(tmp_path, _SHAPES[shape], env_line)

    mounted = _what_compose_mounts(checkout, **extra)
    answer = _resolve(checkout, **extra)
    if how == "nothing set":
        # Every shape defaults to ./data, and the reader has to say so: a
        # reader that refused everything would pass the line below for free.
        assert answer.returncode == 0, answer.stderr
        assert _same_directory(mounted, checkout / "data"), mounted
    else:
        # In this commit a variable that is set is never followed, whatever
        # the compose file would do with it.
        assert answer.returncode == 1, answer.stdout
    if answer.returncode == 0:
        assert _same_directory(answer.stdout.strip(), mounted), (
            f"{shape} / {how}: Compose would mount {mounted}, the host tools "
            f"would use {answer.stdout.strip()}"
        )
    else:
        assert answer.stdout == ""


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
