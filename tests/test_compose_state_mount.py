"""Compose will not create the app's container on a state directory that does
not exist (issue #639).

docker-compose.yml binds the state directory in the long form, for
`create_host_path: false`: the short form creates a missing source as an empty
directory and starts the app on it - no mail cursor, and ingestion re-reads the
mailbox from the beginning.

The move of the state directory leans on it: between the moment `.env` names
the destination and the moment the directory is renamed there, a
`docker compose up` by anybody must fail rather than start the app on a
directory nobody has filled.

Both questions go to Compose itself, never to the file's text:

- what every service mounts at /app/data, as Compose reads the repository's
  own docker-compose.yml (`config --no-interpolate`, which writes every mount
  in the long form, so a short-form mount cannot pass for a long one);
- what Docker does with the app's mounts, exactly as that reading gives them,
  when the directory `IDEALISTA_DATA_DIR` names is missing.

`create_host_path` is read in two halves on purpose. A short-form mount is
written out as `create_host_path: true`, and so is a long one that asks for it:
a `true` is never left out, and the reading refuses it. A `false` is the
long form's default and is left out by Compose versions that omit an option at
its default, so a missing value is read as the default, never as a failure; the
probe answers it, on the same Compose, by behaviour. The file's own text is not
parsed for the option: the repository has no YAML parser, and a literal in the
file says nothing about what this Compose does with it.

The probe does not run the repository's file as it stands: it names the
production container, network and volumes and depends on the database, so on
the mini it would reach for the live stack. It runs the app service's own
mount list instead - a change to any of the app's mounts reaches Docker.

Nothing is started and nothing is pulled: the image is built `FROM scratch`,
and `up --no-start` stops at the step that reads the mount.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.test_reference_data_location import _docker_build_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent

STATE_TARGET = "/app/data"
STATE_SOURCE = "${IDEALISTA_DATA_DIR:-./data}"
STATE_VARIABLE = "IDEALISTA_DATA_DIR"
APP_SERVICE = "app"
PROBE_PREFIX = "idealista-mount-probe-"

TIMEOUT_SECONDS = 180

# What Docker says when a bind's source is missing and Compose was told not to
# create it. The refusal must be this one: a build or daemon failure also exits
# non-zero, and would otherwise pass for it.
MISSING_SOURCE = "bind source path does not exist"

# A directory name a `.env` line would cut at the comment: the probe's path
# reaches Compose through the environment, never through a file Compose parses.
STATE_DIR_NAME = "state #not created yet"


def _compose(
    project_dir: Path, project: str, *args: str, state: Path | None = None
) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COMPOSE_") and key != STATE_VARIABLE
    }
    if state is not None:
        env[STATE_VARIABLE] = str(state)
    return subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


def _services_as_compose_reads_them(tmp_path: Path) -> dict:
    """The repository's docker-compose.yml as Compose reads it: a byte copy in
    a directory with a stand-in `.env` (the file names one), so nothing of the
    checkout's own `.env` is read, and nothing is interpolated."""
    project_dir = tmp_path / "read"
    project_dir.mkdir()
    shutil.copyfile(
        REPO_ROOT / "docker-compose.yml", project_dir / "docker-compose.yml"
    )
    (project_dir / ".env").write_text("", encoding="utf-8")
    read = _compose(
        project_dir,
        _probe_project_name(),
        "-f",
        "docker-compose.yml",
        "config",
        "--no-interpolate",
        "--no-path-resolution",
        "--format",
        "json",
    )
    assert read.returncode == 0, read.stderr
    return json.loads(read.stdout)["services"]


def _state_mounts(service: dict) -> list[dict]:
    return [m for m in service.get("volumes") or [] if m.get("target") == STATE_TARGET]


def _state_mount_problems(services: dict) -> list[str]:
    """What is wrong with the services' state mounts, as Compose read them.

    A missing `create_host_path` is not a problem here: Compose may omit a
    false one. A `true` is always written, and is one."""
    mounting = {name: _state_mounts(svc) for name, svc in services.items()}
    mounting = {name: mounts for name, mounts in mounting.items() if mounts}
    problems = []
    if APP_SERVICE not in mounting:
        problems.append(f"no {STATE_TARGET} mount on {APP_SERVICE}")
    for name, mounts in sorted(mounting.items()):
        for mount in mounts:
            if mount.get("type") != "bind" or mount.get("source") != STATE_SOURCE:
                problems.append(f"{name} mounts {STATE_TARGET} from elsewhere: {mount}")
            create = (mount.get("bind") or {}).get("create_host_path", False)
            if create is not False:
                problems.append(
                    f"{name} mounts {STATE_TARGET} in a form that creates a "
                    f"missing source: {mount}"
                )
    return problems


def _probe_project_name() -> str:
    # Not the PID: two workers in separate PID namespaces can share a daemon,
    # and one's `down` would remove the other's container.
    return f"{PROBE_PREFIX}{uuid.uuid4().hex}"


@contextmanager
def _probe_project(checkout: Path):
    """A project name nobody else holds, and nothing of it left afterwards:
    neither its container nor its image. A cleanup that fails, or leaves
    either, fails the test; when the test has already failed, the cleanup's
    failure is added to that failure, never put in its place."""
    project = _probe_project_name()
    try:
        yield project
    except BaseException as failure:
        try:
            problems = _cleanup(checkout, project)
        except Exception as cleanup:
            problems = [repr(cleanup)]
        for problem in problems:
            failure.add_note(f"the probe's cleanup failed too: {problem}")
        raise
    problems = _cleanup(checkout, project)
    assert problems == [], f"the probe's cleanup failed: {problems}"


def _cleanup(checkout: Path, project: str) -> list[str]:
    """`down` removes the image only through the container it finds, so a
    probe that failed before creating one would leave its image behind: the
    images Compose labelled with the project are removed by that label."""
    problems = []
    down = _compose(checkout, project, "down", "--rmi", "local", "--timeout", "1")
    if down.returncode != 0:
        problems.append(f"down: {down.stderr}")
    for image in _images(project):
        removed = _docker("image", "rm", "--force", image)
        if removed.returncode != 0:
            problems.append(f"image rm {image}: {removed.stderr}")
    left = _containers(project) + _images(project)
    if left:
        problems.append(f"left behind: {left}")
    return problems


def test_every_service_that_mounts_the_state_refuses_a_missing_directory(tmp_path):
    _docker_build_or_skip()
    assert _state_mount_problems(_services_as_compose_reads_them(tmp_path)) == []


def test_compose_creates_no_container_on_a_state_directory_that_does_not_exist(
    tmp_path,
):
    _docker_build_or_skip()
    app_mounts = _services_as_compose_reads_them(tmp_path)[APP_SERVICE]["volumes"]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marker").write_text("x\n", encoding="utf-8")
    (checkout / "Dockerfile").write_text(
        'FROM scratch\nCOPY marker /marker\nCMD ["/marker"]\n', encoding="utf-8"
    )
    # JSON is YAML: the app's mounts go to Docker exactly as Compose read them.
    probe = {"services": {APP_SERVICE: {"build": ".", "volumes": app_mounts}}}
    (checkout / "docker-compose.yml").write_text(json.dumps(probe), encoding="utf-8")
    state = tmp_path / STATE_DIR_NAME
    with _probe_project(checkout) as project:
        # The build is its own step, so a build that fails is a failure here
        # and never reads as the refusal below.
        built = _compose(checkout, project, "build", APP_SERVICE)
        assert built.returncode == 0, built.stderr

        refused = _compose(
            checkout,
            project,
            "up",
            "--no-start",
            "--no-build",
            APP_SERVICE,
            state=state,
        )
        assert refused.returncode != 0, (
            f"Compose created the container although the directory "
            f"{STATE_VARIABLE} names does not exist\n"
            f"stdout:\n{refused.stdout}\nstderr:\n{refused.stderr}"
        )
        said = refused.stdout + refused.stderr
        assert MISSING_SOURCE in said and str(state) in said, (
            f"Compose refused, but not for the missing state directory:\n{said}"
        )
        assert not state.exists(), "Compose created the missing state directory"
        assert _containers(project) == []

        # The same project with the directory in place: the refusal above was
        # for the directory and for nothing else.
        state.mkdir()
        created = _compose(
            checkout,
            project,
            "up",
            "--no-start",
            "--no-build",
            APP_SERVICE,
            state=state,
        )
        assert created.returncode == 0, created.stderr
        assert len(_containers(project)) == 1
        assert list(state.iterdir()) == []


def _bind(**bind) -> dict:
    return {
        "type": "bind",
        "source": STATE_SOURCE,
        "target": STATE_TARGET,
        "bind": bind,
    }


@pytest.mark.parametrize(
    "mount",
    [
        _bind(create_host_path=False),
        _bind(),  # a Compose that omits a false option
        {"type": "bind", "source": STATE_SOURCE, "target": STATE_TARGET},
    ],
    ids=["written", "bind-without-the-option", "no-bind-block"],
)
def test_a_false_that_compose_leaves_out_is_not_read_as_a_problem(mount):
    assert _state_mount_problems({APP_SERVICE: {"volumes": [mount]}}) == []


@pytest.mark.parametrize(
    "services",
    [
        {APP_SERVICE: {"volumes": [_bind(create_host_path=True)]}},
        {
            APP_SERVICE: {"volumes": [_bind()]},
            "worker": {"volumes": [_bind(create_host_path=True)]},
        },
        {"worker": {"volumes": [_bind()]}},
        {APP_SERVICE: {"volumes": [{**_bind(), "type": "volume"}]}},
    ],
    ids=["app-creates", "worker-creates", "no-app-mount", "not-a-bind"],
)
def test_a_mount_that_creates_the_directory_or_is_not_the_state_is_a_problem(
    services,
):
    assert _state_mount_problems(services) != []


def test_two_probes_never_share_a_project_name():
    first, second = _probe_project_name(), _probe_project_name()
    assert first != second
    assert first.startswith(PROBE_PREFIX) and second.startswith(PROBE_PREFIX)


def _refusing_compose(*_args, **_kwargs) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", "Cannot connect to the daemon")


def _nothing_left(monkeypatch) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_containers", lambda _project: [])
    monkeypatch.setattr(module, "_images", lambda _project: [])


def test_a_down_that_fails_fails_the_test(monkeypatch, tmp_path):
    _nothing_left(monkeypatch)
    monkeypatch.setattr(sys.modules[__name__], "_compose", _refusing_compose)
    with pytest.raises(AssertionError, match="cleanup failed"):
        with _probe_project(tmp_path):
            pass


def test_an_image_the_cleanup_cannot_remove_fails_the_test(monkeypatch, tmp_path):
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_containers", lambda _project: [])
    monkeypatch.setattr(module, "_images", lambda _project: ["0123abcd"])
    monkeypatch.setattr(
        module,
        "_compose",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        module, "_docker", lambda *_a: subprocess.CompletedProcess([], 0, "", "")
    )
    with pytest.raises(AssertionError, match="left behind"):
        with _probe_project(tmp_path):
            pass


def test_a_cleanup_that_fails_does_not_hide_the_failure_before_it(
    monkeypatch, tmp_path
):
    _nothing_left(monkeypatch)
    monkeypatch.setattr(sys.modules[__name__], "_compose", _refusing_compose)
    with pytest.raises(LookupError, match="the probe's own failure") as raised:
        with _probe_project(tmp_path):
            raise LookupError("the probe's own failure")
    assert any("cleanup failed too" in note for note in raised.value.__notes__)


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _labelled(project: str) -> str:
    return f"label=com.docker.compose.project={project}"


def _containers(project: str) -> list[str]:
    listed = _docker(
        "ps", "-a", "--filter", _labelled(project), "--format", "{{.Names}}"
    )
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split()


def _images(project: str) -> list[str]:
    listed = _docker("images", "--filter", _labelled(project), "--format", "{{.ID}}")
    assert listed.returncode == 0, listed.stderr
    return sorted(set(listed.stdout.split()))
