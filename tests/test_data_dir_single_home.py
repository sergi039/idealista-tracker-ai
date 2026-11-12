"""The host path of the deployment's state gets one home (issue #639).

The app container persists one directory, mounted at `/app/data`. On the host
it is spelled `./data` in docker-compose.yml and `${REPO_DIR}/data` in seven
scripts - eight copies of "inside the git working copy", which is the fact
that let a `pytest` run in the checkout overwrite the production mail cursor.

It is one variable now, `IDEALISTA_DATA_DIR` in the checkout's `.env`.
docker-compose.yml mounts `${IDEALISTA_DATA_DIR:-./data}`, and every host tool
asks `tools/autopilot/lib/data_dir.sh`, which parses neither file: named
nowhere it answers `<repo>/data`, named anywhere it asks `docker compose config`.

Pinned here:

* the reader's contract and the watcher's use of it, by running the two shell
  harnesses (pytest is what CI runs; a harness nobody runs proves nothing);
* against Compose itself: the directory the reader names is the one
  `docker compose config` binds at /app/data - or the reader names none;
* that the host variable can never be mistaken for the container's;
* that a linked git worktree is refused as a root, with real git;
* that every host script asks the reader, and none has grown another copy of
  the path.
"""

from __future__ import annotations

import json
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
MOVE_TEST = REPO_ROOT / "tools" / "move_data_dir_test.sh"

# The harness is a few dozen runs of a short shell script. The bound is there
# to fail a hang.
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
    runner's /bin/bash is a bash 5. The reader is parameter expansion and
    `case` patterns, which is exactly where the two disagree.
    """
    interpreters = ["/bin/bash"]
    other = shutil.which("bash")
    if other and os.path.realpath(other) != os.path.realpath("/bin/bash"):
        interpreters.append(other)
    return interpreters


# What the reader refuses to find set in its environment (data_dir.sh).
_READER_REFUSES = (VARIABLE, "COMPOSE_ENV_FILES", "COMPOSE_DISABLE_ENV_FILE")


def _environment(**extra: str) -> dict[str, str]:
    """This process's environment without the variable under test: a shell
    that exported it would otherwise decide every scenario."""
    env = {
        key: value for key, value in os.environ.items() if key not in _READER_REFUSES
    }
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
    assert result.stdout.count("OK: ") == 7, result.stdout


def test_moving_the_state_directory_never_leaves_the_app_down():
    """tools/move_data_dir.sh against a stateful stub docker: the move, the
    rollback, every preflight refusal, and a failure at each step after the
    app is stopped - including a signal - ending on the original directory."""
    for tool in ("git", "python3", "shasum"):
        assert shutil.which(tool), f"the harness needs {tool} on PATH"
    result = subprocess.run(
        ["/bin/bash", str(MOVE_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
        env=_environment(),
    )
    assert result.returncode == 0, (
        "tools/move_data_dir.sh can leave the deployment stopped, split or "
        "changed after a failed move\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ALL 8 GROUPS PASSED" in result.stdout, result.stdout


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


def _git(*args: str, cwd: Path) -> None:
    """Real git, deaf to this machine: no inherited GIT_* (a hook exports
    GIT_DIR), no global or system configuration, so no hooks of the owner's."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=60,
        env=env,
    )


def test_a_linked_git_worktree_is_refused_and_its_checkout_is_answered(tmp_path):
    """The stack is started from one checkout. A linked worktree of the same
    repository has a `data/` no container mounts, so a tool run from it would
    look for the lock, the markers and the attachments where nothing writes
    them. Made with real `git worktree add`, because what the reader goes by
    is what git leaves behind: a `.git` file there, a directory in the
    checkout."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-q", cwd=checkout)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=checkout)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "--detach", str(linked), cwd=checkout)
    (linked / "data").mkdir()

    answered = _resolve(checkout)
    assert answered.returncode == 0, answered.stderr
    assert answered.stdout == f"{checkout}/data\n"

    refused = _resolve(linked)
    assert refused.returncode == 1, refused.stdout
    assert refused.stdout == ""
    assert "linked git worktree" in refused.stderr


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


def test_compose_mounts_the_variable_and_defaults_to_the_old_literal():
    """`${IDEALISTA_DATA_DIR:-./data}` renders `./data` when nothing is set,
    which is the literal this line used to be - so a merge moves nothing.

    Long form, for `create_host_path: false`: the short form creates a missing
    source as an empty directory and starts the app on it. Measured on Compose
    v5.7: with the flag that is "bind source path does not exist".
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
    # The dev overlay adds no mount of its own.
    dev = (REPO_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert "/app/data" not in "\n".join(
        line for line in dev.splitlines() if not line.strip().startswith("#")
    )


_WAYS = [
    "nothing set",
    "env file",
    "environment",
    "both",
    "environment over env file",
    "empty in environment",
    "empty in environment over env file",
]


# An export is private to one process: Compose run from it would follow it,
# the watcher under launchd would not. Refused - an EMPTY one included.
_EXPORTED = set(_WAYS) - {"nothing set", "env file"}


def _refuses_the_export(answer: subprocess.CompletedProcess) -> None:
    assert answer.returncode == 1 and answer.stdout == "", answer.stdout
    assert "is set in this process's environment" in answer.stderr, answer.stderr


def _how(state: Path, other: Path, how: str) -> tuple[str | None, dict[str, str]]:
    """The `.env` line and the environment for one way of setting the
    variable. In the last one the two disagree, and Compose takes the
    environment's."""
    if how == "environment over env file":
        return f"{VARIABLE}={other}", {VARIABLE: str(state)}
    if how.startswith("empty in environment"):
        return (f"{VARIABLE}={other}" if "over" in how else None), {VARIABLE: ""}
    env_line = f"{VARIABLE}={state}" if how in ("env file", "both") else None
    extra = {VARIABLE: str(state)} if how in ("environment", "both") else {}
    return env_line, extra


@pytest.mark.parametrize("how", _WAYS)
def test_compose_and_the_reader_name_the_same_directory(tmp_path, how):
    """The real docker-compose.yml: one input, Compose's answer, and the
    reader's - asked the way every host tool asks it."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    state = tmp_path / "state"
    other = tmp_path / "other"
    state.mkdir()
    other.mkdir()
    env_line, extra = _how(state, other, how)
    checkout = _stand_in_checkout(tmp_path, compose, env_line)

    mounted = _what_compose_mounts(checkout, **extra)
    answer = _resolve(checkout, **extra)
    if how in _EXPORTED:
        # Compose run from this process would mount `state` - or, for an
        # empty export, the checkout's `data` whatever `.env` names.
        here = checkout / "data" if how.startswith("empty") else state
        assert _same_directory(mounted, here), mounted
        _refuses_the_export(answer)
        return
    assert answer.returncode == 0, answer.stderr
    expected = checkout / "data" if how == "nothing set" else state
    assert _same_directory(mounted, expected), mounted
    assert _same_directory(answer.stdout.strip(), mounted), (
        f"{how}: Compose would mount {mounted}, the host tools would use "
        f"{answer.stdout.strip()}"
    )


def test_a_directory_that_does_not_exist_is_refused_and_not_created(tmp_path):
    """Compose would mount it (and, with `create_host_path: false`, fail to
    start). The reader gives no directory, and creates none."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    missing = tmp_path / "typo"
    checkout = _stand_in_checkout(tmp_path, compose, f"{VARIABLE}={missing}")

    assert _what_compose_mounts(checkout) == str(missing)
    refused = _resolve(checkout)
    assert refused.returncode == 1 and refused.stdout == "", refused.stdout
    assert "not an existing directory" in refused.stderr
    assert not missing.exists()


# Compose files that say different things about the variable. The first is the
# reviewer's input from #665: the real bind is the literal, the variable is
# only in a comment after it. A reader that looked for the variable's name in
# the file took this for a mount; this one asks Compose, which does not.
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


# Lines of `.env` that a reader of text and Compose can take differently.
_ENV_SPELLINGS = {
    "plain": "IDEALISTA_DATA_DIR={state}",
    "export": "export IDEALISTA_DATA_DIR={state}",
    "spaces around =": "IDEALISTA_DATA_DIR = {state}",
    "colon": "IDEALISTA_DATA_DIR: {state}",
    "double quotes": 'IDEALISTA_DATA_DIR="{state}"',
    "single quotes": "IDEALISTA_DATA_DIR='{state}'",
    "inline comment": "IDEALISTA_DATA_DIR={state} # the state",
    "commented out": "# IDEALISTA_DATA_DIR={state}",
    "longer name": "IDEALISTA_DATA_DIR_OLD={state}",
    "twice": "IDEALISTA_DATA_DIR={other}\nIDEALISTA_DATA_DIR={state}",
    "inside another value": 'NOTE="first line\nIDEALISTA_DATA_DIR={state}\n"',
    "empty": "IDEALISTA_DATA_DIR=",
    # The review's input to #694. The second line starts with `#`, and to
    # Compose it closes NOTE's value and then sets the variable.
    "after the quote that closes another value": (
        "NOTE='first line\n#' IDEALISTA_DATA_DIR=./state"
    ),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("how", _WAYS)
def test_the_reader_never_names_a_directory_compose_would_not_mount(
    tmp_path, shape, how
):
    """The question a reviewer put to this reader: on which input does it
    agree to a directory the app does not mount? For every compose file above
    and every way of setting the variable, the reader gives the directory
    `docker compose config` binds at /app/data."""
    _docker_compose_or_skip()
    state = tmp_path / "state"
    other = tmp_path / "other"
    state.mkdir()
    other.mkdir()
    env_line, extra = _how(state, other, how)
    checkout = _stand_in_checkout(tmp_path, _SHAPES[shape], env_line)

    answer = _resolve(checkout, **extra)
    if how in _EXPORTED:
        _refuses_the_export(answer)
        return
    mounted = _what_compose_mounts(checkout, **extra)
    if env_line and not _same_directory(mounted, state):
        # `.env` names one directory and this file mounts another: Compose's
        # answer is not the text of `.env`, so the reader gives none.
        assert answer.returncode == 1 and answer.stdout == "", answer.stdout
        return
    # Otherwise there is nothing to refuse: refusing everything must not pass.
    assert answer.returncode == 0, answer.stderr
    assert _same_directory(answer.stdout.strip(), mounted), (shape, how, mounted)


# One more variable in the environment while `.env` names `state`. The first
# is the review's input to #678.
_ENVIRONMENTS = [
    f"{VARIABLE}=",
    f"{VARIABLE}={{other}}",
    "COMPOSE_ENV_FILES=",
    "COMPOSE_ENV_FILES=other.env",
    "COMPOSE_DISABLE_ENV_FILE=",
    "COMPOSE_DISABLE_ENV_FILE=1",
    "COMPOSE_FILE=",
    "COMPOSE_FILE=literal.yml",
    "COMPOSE_PROJECT_NAME=",
    "COMPOSE_PROJECT_NAME=another",
    "COMPOSE_PROFILES=",
    "COMPOSE_PROFILES=another",
    "COMPOSE_PATH_SEPARATOR=;",
    "COMPOSE_PROJECT_DIRECTORY={other}",
    "DOCKER_CONTEXT=nope",
    "DOCKER_HOST=tcp://127.0.0.1:1",
    "PWD={other}",
]


@pytest.mark.parametrize("setting", _ENVIRONMENTS)
def test_the_environment_never_moves_the_reader_off_what_the_watcher_mounts(
    tmp_path, setting
):
    """Whatever is set, the reader names what Compose mounts for a process
    WITHOUT it (the watcher), or refuses - and what it does not refuse,
    Compose itself must not care about."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    state = tmp_path / "state"
    other = tmp_path / "other"
    state.mkdir()
    other.mkdir()
    checkout = _stand_in_checkout(tmp_path, compose, f"{VARIABLE}={state}")
    (checkout / "other.env").write_text(f"{VARIABLE}={other}\n")
    (checkout / "literal.yml").write_text(
        compose.replace("${IDEALISTA_DATA_DIR:-./data}", "./data"), encoding="utf-8"
    )
    # Beside the file the watcher passes with `-f`: Compose does not pick it up.
    (checkout / "docker-compose.override.yml").write_text(
        f"services:\n  app:\n    volumes:\n      - {other}:/app/data\n"
    )
    name, _, value = setting.partition("=")
    extra = {name: value.format(other=other)}

    answer = _resolve(checkout, **extra)
    if name in _READER_REFUSES:
        _refuses_the_export(answer)
        assert name in answer.stderr
        return
    assert _same_directory(_what_compose_mounts(checkout, **extra), state), setting
    assert answer.returncode == 0, answer.stderr
    assert _same_directory(answer.stdout.strip(), state), answer.stdout


# Values Compose derives from the process that asks, and what that process
# has to differ in. `~` is the second input of the review of #678.
_DERIVED = {"~/state": "HOME", "${PROBE_639}/state": "PROBE_639", "./state": ""}


@pytest.mark.parametrize("value", sorted(_DERIVED))
def test_what_compose_derives_from_the_asking_process_is_not_an_answer(tmp_path, value):
    """`.env` says `~/state`, two processes have two HOMEs, both directories
    exist: Compose mounts one for each. The reader takes Compose's answer only
    when it is the text of `.env`, so it gives neither."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    checkout = _stand_in_checkout(tmp_path, compose, f"{VARIABLE}={value}")
    # The compose plugin is looked up under HOME; keep it findable.
    docker = os.environ.get("DOCKER_CONFIG") or os.path.expanduser("~/.docker")
    mounted = []
    for who in ("one", "two"):
        root = tmp_path / who if _DERIVED[value] else checkout
        (root / "state").mkdir(parents=True, exist_ok=True)
        extra = {"DOCKER_CONFIG": docker}
        if _DERIVED[value]:
            extra[_DERIVED[value]] = str(root)
        mounted.append(_what_compose_mounts(checkout, **extra))
        assert _same_directory(mounted[-1], root / "state"), mounted
        answer = _resolve(checkout, **extra)
        assert answer.returncode == 1 and answer.stdout == "", answer.stdout
        assert "does not say exactly that" in answer.stderr, answer.stderr
    assert bool(_DERIVED[value]) != _same_directory(*mounted), mounted


@pytest.mark.parametrize("spelling", sorted(_ENV_SPELLINGS))
def test_only_the_plain_line_is_an_answer(tmp_path, spelling):
    """Compose reads several of these as the variable. The reader takes
    Compose's answer for one: the line that says it in so many characters.
    Every other spelling is refused - never read a second way, and never
    answered with the checkout's own `data`. A commented-out line too: what
    is a comment and what is the tail of a quoted value is Compose's to say."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    state = tmp_path / "state"
    other = tmp_path / "other"
    state.mkdir()
    other.mkdir()
    line = _ENV_SPELLINGS[spelling].format(state=state, other=other)
    checkout = _stand_in_checkout(tmp_path, compose, line)

    answer = _resolve(checkout)
    if spelling == "plain":
        assert answer.returncode == 0, answer.stderr
        assert _same_directory(_what_compose_mounts(checkout), state)
        assert _same_directory(answer.stdout.strip(), state), answer.stdout
        return
    if spelling.startswith("after the quote"):
        # What makes it dangerous: Compose does mount the directory, so a
        # reader that skipped the line as a comment would name another one.
        (checkout / "state").mkdir()
        assert _same_directory(_what_compose_mounts(checkout), checkout / "state")
        answer = _resolve(checkout)
    assert answer.returncode == 1 and answer.stdout == "", (spelling, answer.stdout)


def test_a_value_that_renders_with_a_line_break_is_refused(tmp_path):
    """Review of #694: `IDEALISTA_DATA_DIR="${STATE_ROOT}\\nMARKER="` with a
    `MARKER=` line after it. Compose renders a directory whose path holds a
    line break; as a `grep -F -x` pattern that is two lines, and the second is
    in `.env` for every STATE_ROOT. Both directories exist; neither is given."""
    _docker_compose_or_skip()
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    checkout = _stand_in_checkout(
        tmp_path, compose, VARIABLE + '="${STATE_ROOT}\\nMARKER="\nMARKER='
    )
    for who in ("one", "two"):
        state_root = tmp_path / who
        rendered = f"{state_root}\nMARKER="
        os.makedirs(rendered)
        assert _what_compose_mounts(checkout, STATE_ROOT=str(state_root)) == rendered
        answer = _resolve(checkout, STATE_ROOT=str(state_root))
        assert answer.returncode == 1 and answer.stdout == "", answer.stdout


def test_a_copy_of_the_example_env_file_is_answered(tmp_path):
    """`cp .env.example .env` is how a checkout gets its `.env`. Any mention
    of the variable there - a commented-out example too - is a mention the
    reader takes to Compose and then refuses, so the example names it nowhere."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert VARIABLE not in example
    (tmp_path / ".env").write_text(example, encoding="utf-8")
    answer = _resolve(tmp_path)
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout == f"{tmp_path}/data\n"


def test_another_compose_file_is_read_against_the_checkout(tmp_path):
    """`data_dir.sh --compose-source <repo> <file>` is how the deploy gate asks
    what a commit that is not checked out would mount. The file is a copy that
    lies outside the checkout; `.env` and `./data` must still be the
    checkout's, or the answer is about some other deployment."""
    _docker_compose_or_skip()
    state = tmp_path / "state"
    state.mkdir()
    checkout = _stand_in_checkout(
        tmp_path, _SHAPES["variable mounted, long form"], f"{VARIABLE}={state}"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for shape, expected in (
        ("variable mounted, long form", state),
        ("literal bind, variable in an inline comment", checkout / "data"),
    ):
        copy = elsewhere / "target.yml"
        copy.write_text(_SHAPES[shape], encoding="utf-8")
        asked = subprocess.run(
            ["/bin/bash", str(LIB), "--compose-source", str(checkout), str(copy)],
            capture_output=True,
            text=True,
            timeout=60,
            env=_environment(),
        )
        assert asked.returncode == 0, asked.stderr
        assert _same_directory(asked.stdout.strip(), expected), (shape, asked.stdout)


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
