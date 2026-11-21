"""Compose will not create the app's container on a state directory that does
not exist (issue #639).

docker-compose.yml binds the state directory in the long form, for
`create_host_path: false`: the short form creates a missing source as an empty
directory and starts the app on it - no mail cursor, and ingestion re-reads the
mailbox from the beginning.

`tests/test_data_dir_single_home.py` pins that the file SAYS so. This asks
Docker itself what the words do, because the move of the state directory leans
on it: between the moment `.env` names the destination and the moment the
directory is renamed there, a `docker compose up` by anybody must fail rather
than start the app on a directory nobody has filled.

Nothing is started and nothing is pulled: the image is built `FROM scratch`,
and `up --no-start` stops at the step that reads the mount.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.test_reference_data_location import _docker_build_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent

# The mount as docker-compose.yml writes it.
MOUNT = (
    "      - type: bind\n"
    "        source: ${IDEALISTA_DATA_DIR:-./data}\n"
    "        target: /app/data\n"
    "        bind:\n"
    "          create_host_path: false\n"
)

TIMEOUT_SECONDS = 180


def _compose(checkout: Path, project: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COMPOSE_") and key != "IDEALISTA_DATA_DIR"
    }
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", "docker-compose.yml", *args],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


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


def test_the_mount_under_test_is_the_one_the_deployment_uses():
    real = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in real.splitlines() if not line.strip().startswith("#")
    )
    assert MOUNT in code + "\n"


def test_compose_creates_no_container_on_a_state_directory_that_does_not_exist(
    tmp_path,
):
    _docker_build_or_skip()
    project = f"idealista-mount-probe-{os.getpid()}"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "marker").write_text("x\n", encoding="utf-8")
    (checkout / "Dockerfile").write_text(
        'FROM scratch\nCOPY marker /marker\nCMD ["/marker"]\n', encoding="utf-8"
    )
    (checkout / "docker-compose.yml").write_text(
        "services:\n  app:\n    build: .\n    volumes:\n" + MOUNT, encoding="utf-8"
    )
    state = tmp_path / "state-not-created-yet"
    (checkout / ".env").write_text(f"IDEALISTA_DATA_DIR={state}\n", encoding="utf-8")
    try:
        refused = _compose(checkout, project, "up", "--no-start", "--build", "app")
        assert refused.returncode != 0, (
            "Compose created the container although the directory `.env` names "
            f"does not exist\nstdout:\n{refused.stdout}\nstderr:\n{refused.stderr}"
        )
        assert not state.exists(), "Compose created the missing state directory"
        assert _containers(project) == []

        # The same project with the directory in place: the refusal above was
        # for the directory and for nothing else.
        state.mkdir()
        created = _compose(checkout, project, "up", "--no-start", "--build", "app")
        assert created.returncode == 0, created.stderr
        assert len(_containers(project)) == 1
        assert list(state.iterdir()) == []
    finally:
        _compose(checkout, project, "down", "--rmi", "local", "--timeout", "1")
