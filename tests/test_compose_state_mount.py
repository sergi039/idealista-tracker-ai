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
  when the directory `.env` names is missing.

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
from pathlib import Path

from tests.test_reference_data_location import _docker_build_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent

STATE_TARGET = "/app/data"
STATE_SOURCE = "${IDEALISTA_DATA_DIR:-./data}"
APP_SERVICE = "app"

TIMEOUT_SECONDS = 180

# What Docker says when a bind's source is missing and Compose was told not to
# create it. The refusal must be this one: a build or daemon failure also exits
# non-zero, and would otherwise pass for it.
MISSING_SOURCE = "bind source path does not exist"


def _compose(
    project_dir: Path, project: str, *args: str
) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COMPOSE_") and key != "IDEALISTA_DATA_DIR"
    }
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
        "idealista-mount-read",
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


def test_every_service_that_mounts_the_state_refuses_a_missing_directory(tmp_path):
    _docker_build_or_skip()
    services = _services_as_compose_reads_them(tmp_path)
    mounting = {name: _state_mounts(svc) for name, svc in services.items()}
    mounting = {name: mounts for name, mounts in mounting.items() if mounts}
    assert APP_SERVICE in mounting, f"no {STATE_TARGET} mount on {APP_SERVICE}"
    for name, mounts in mounting.items():
        for mount in mounts:
            assert mount.get("type") == "bind", (name, mount)
            assert mount.get("source") == STATE_SOURCE, (name, mount)
            assert (mount.get("bind") or {}).get("create_host_path") is False, (
                f"{name} mounts {STATE_TARGET} in a form that creates a missing "
                f"source: {mount}"
            )


def test_compose_creates_no_container_on_a_state_directory_that_does_not_exist(
    tmp_path,
):
    _docker_build_or_skip()
    app_mounts = _services_as_compose_reads_them(tmp_path)[APP_SERVICE]["volumes"]
    project = f"idealista-mount-probe-{os.getpid()}"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marker").write_text("x\n", encoding="utf-8")
    (checkout / "Dockerfile").write_text(
        'FROM scratch\nCOPY marker /marker\nCMD ["/marker"]\n', encoding="utf-8"
    )
    # JSON is YAML: the app's mounts go to Docker exactly as Compose read them.
    probe = {"services": {APP_SERVICE: {"build": ".", "volumes": app_mounts}}}
    (checkout / "docker-compose.yml").write_text(json.dumps(probe), encoding="utf-8")
    state = tmp_path / "state-not-created-yet"
    (checkout / ".env").write_text(f"IDEALISTA_DATA_DIR={state}\n", encoding="utf-8")
    try:
        # The build is its own step, so a build that fails is a failure here
        # and never reads as the refusal below.
        built = _compose(checkout, project, "build", APP_SERVICE)
        assert built.returncode == 0, built.stderr

        refused = _compose(
            checkout, project, "up", "--no-start", "--no-build", APP_SERVICE
        )
        assert refused.returncode != 0, (
            "Compose created the container although the directory `.env` names "
            f"does not exist\nstdout:\n{refused.stdout}\nstderr:\n{refused.stderr}"
        )
        said = refused.stdout + refused.stderr
        assert MISSING_SOURCE in said and state.name in said, (
            f"Compose refused, but not for the missing state directory:\n{said}"
        )
        assert not state.exists(), "Compose created the missing state directory"
        assert _containers(project) == []

        # The same project with the directory in place: the refusal above was
        # for the directory and for nothing else.
        state.mkdir()
        created = _compose(
            checkout, project, "up", "--no-start", "--no-build", APP_SERVICE
        )
        assert created.returncode == 0, created.stderr
        assert len(_containers(project)) == 1
        assert list(state.iterdir()) == []
    finally:
        _compose(checkout, project, "down", "--rmi", "local", "--timeout", "1")


def _containers(project: str) -> list[str]:
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split()
