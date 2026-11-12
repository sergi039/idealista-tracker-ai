"""The host path of the deployment's state gets one home (issue #639).

The app container persists one directory, mounted at `/app/data`. On the host
it is spelled `./data` in docker-compose.yml and `${REPO_DIR}/data` in seven
scripts - eight copies of "inside the git working copy", which is the fact
that let a `pytest` run in the checkout overwrite the production mail cursor.

It is one variable now, `IDEALISTA_DATA_DIR` in the checkout's `.env`.
docker-compose.yml mounts `${IDEALISTA_DATA_DIR:-./data}`, and every host tool
asks `tools/autopilot/lib/data_dir.sh`. Compose is the reader that decides
where the app's state really is, so the other one keeps no opinion of its own:
it parses neither docker-compose.yml nor `.env`. With the variable named
nowhere it answers `<repo>/data`; with it named anywhere it asks
`docker compose config` what is bound at `/app/data`.

Pinned here:

* the reader's contract and the watcher's use of it, by running the two shell
  harnesses (pytest is what CI runs; a harness nobody runs proves nothing);
* against Compose itself: for the real docker-compose.yml and for a range of
  compose files and `.env` spellings, the directory the reader names is the
  one `docker compose config` binds at /app/data - or the reader names none;
* that Compose's default still renders the directory it always did, and that
  a source that does not exist is an error rather than a new empty directory;
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
    assert result.stdout.count("OK: ") == 2, result.stdout


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
    # The dev overlay adds no mount of its own.
    dev = (REPO_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert "/app/data" not in "\n".join(
        line for line in dev.splitlines() if not line.strip().startswith("#")
    )


_WAYS = ["nothing set", "env file", "environment", "both", "environment over env file"]


# An export is private to one process: Compose run from it would follow the
# export, the watcher under launchd would not. The reader refuses those.
_EXPORTED = {"environment", "both", "environment over env file"}


def _refuses_the_export(answer: subprocess.CompletedProcess) -> None:
    assert answer.returncode == 1 and answer.stdout == "", answer.stdout
    assert "exported" in answer.stderr, answer.stderr


def _how(state: Path, other: Path, how: str) -> tuple[str | None, dict[str, str]]:
    """The `.env` line and the environment for one way of setting the
    variable. In the last one the two disagree, and Compose takes the
    environment's."""
    if how == "environment over env file":
        return f"{VARIABLE}={other}", {VARIABLE: str(state)}
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
        # Compose run from this process would mount `state`; nothing run from
        # another process would know.
        assert _same_directory(mounted, state), mounted
        _refuses_the_export(answer)
        return
    assert answer.returncode == 0, answer.stderr
    expected = checkout / "data" if how == "nothing set" else state
    assert _same_directory(mounted, expected), mounted
    assert _same_directory(answer.stdout.strip(), mounted), (
        f"{how}: Compose would mount {mounted}, the host tools would use "
        f"{answer.stdout.strip()}"
    )


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


def _agrees_or_refuses(checkout: Path, label: str, **extra: str) -> None:
    """The property: the reader names the directory Compose binds at
    /app/data, or it names none. Compose may itself reject the input; then
    the reader must not answer either."""
    rendered = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=checkout,
        env=_environment(**extra),
    )
    answer = _resolve(checkout, **extra)
    if rendered.returncode != 0:
        assert answer.returncode == 1 and answer.stdout == "", (label, answer.stdout)
        return
    mounted = _what_compose_mounts(checkout, **extra)
    if answer.returncode != 0:
        assert answer.stdout == "", label
        # A refusal is only for a directory that cannot be used.
        assert not os.path.isdir(mounted), (label, mounted, answer.stderr)
        return
    assert _same_directory(answer.stdout.strip(), mounted), (
        f"{label}: Compose would mount {mounted}, the host tools would use "
        f"{answer.stdout.strip()}"
    )


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
    # Every source in play exists, so there is nothing to refuse: a reader
    # that refused everything must not pass.
    assert answer.returncode == 0, answer.stderr
    _agrees_or_refuses(checkout, f"{shape} / {how}", **extra)


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
