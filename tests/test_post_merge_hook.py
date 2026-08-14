"""Contract for .githooks/post-merge (auto-rebuild after a merge).

The hook exists because an image is a `COPY . .` snapshot that nothing
re-takes: on 2026-08-14 a container served a broken template for 15 minutes
across the fix, its commit and its merge. What has to hold is narrow and
easy to break by accident, so each rule is pinned here:

  - it rebuilds when main lands and the merge touched the build context,
  - it leaves a branch checkout, a stopped container and an in-progress
    deploy alone,
  - it refuses to build a template that does not parse, rather than
    snapshotting it,
  - and git really does invoke it on a pull.

Docker is stubbed - the assertions are about what the hook decides to run,
not about building an image.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".githooks" / "post-merge"
LOCK_LIB = REPO_ROOT / "tools" / "autopilot" / "lib" / "lock.sh"

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [ "$1" = "ps" ]; then
    if [ -n "${DOCKER_PS_OUTPUT:-}" ]; then
        printf '%s\\n' "$DOCKER_PS_OUTPUT"
    fi
    exit 0
fi
exit "${DOCKER_RC:-0}"
"""

CURL_STUB = """#!/bin/sh
printf '%s' "${CURL_BODY:-}"
exit "${CURL_RC:-0}"
"""

GOOD_TEMPLATE = "{% block body %}{% if x %}ok{% endif %}{% endblock %}\n"
# The 2026-08-14 defect itself: one endif more than there are ifs.
BROKEN_TEMPLATE = "{% block body %}{% if x %}ok{% endif %}{% endif %}{% endblock %}\n"


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway clone shaped like this one, with the hook installed."""
    work = tmp_path / "repo"
    (work / "templates").mkdir(parents=True)
    (work / "tools" / "autopilot" / "lib").mkdir(parents=True)
    (work / ".githooks").mkdir()

    shutil.copy(HOOK, work / ".githooks" / "post-merge")
    (work / ".githooks" / "post-merge").chmod(0o755)
    shutil.copy(LOCK_LIB, work / "tools" / "autopilot" / "lib" / "lock.sh")
    (work / "templates" / "page.html").write_text(GOOD_TEMPLATE)
    (work / "docker-compose.yml").write_text("services: {}\n")

    _git(work, "init", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    # A second commit so ORIG_HEAD can name a real range.
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "second")
    _git(work, "update-ref", "ORIG_HEAD", "HEAD~1")
    return work


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(DOCKER_STUB)
    (binaries / "docker").chmod(0o755)
    (binaries / "curl").write_text(CURL_STUB)
    (binaries / "curl").chmod(0o755)
    return binaries


def run_hook(repo: Path, stub_bin: Path, tmp_path: Path, **overrides: str):
    docker_log = tmp_path / "docker.log"
    docker_log.touch()
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "DOCKER_PS_OUTPUT": "idealista-app",
            "CURL_BODY": '{"ok":true}',
            "AUTO_REBUILD_PYTHON": sys.executable,
            "AUTOPILOT_LOCK_DIR": str(tmp_path / "deploy.lock"),
            "AUTO_REBUILD_HEALTH_TIMEOUT": "5",
        }
    )
    for key in ("SKIP_AUTO_REBUILD", "AUTO_REBUILD_BRANCH", "COMPOSE_CONTAINER_PREFIX"):
        env.pop(key, None)
    env.update(overrides)

    proc = subprocess.run(
        [str(repo / ".githooks" / "post-merge")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, docker_log.read_text()


def built(docker_log: str) -> bool:
    return any("up -d --build" in line for line in docker_log.splitlines())


def test_rebuilds_when_main_moves(repo, stub_bin, tmp_path):
    proc, log = run_hook(repo, stub_bin, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert built(log), f"expected a rebuild, docker saw:\n{log}"
    assert "health OK" in proc.stdout


def test_leaves_a_branch_checkout_alone(repo, stub_bin, tmp_path):
    _git(repo, "checkout", "-b", "claude/some-work")

    proc, log = run_hook(repo, stub_bin, tmp_path)

    assert not built(log)
    assert "not 'main'" in proc.stdout


def test_leaves_a_stopped_container_stopped(repo, stub_bin, tmp_path):
    proc, log = run_hook(repo, stub_bin, tmp_path, DOCKER_PS_OUTPUT="")

    assert not built(log)
    assert "is not running" in proc.stdout


def test_skips_when_the_merge_missed_the_build_context(repo, stub_bin, tmp_path):
    _git(repo, "update-ref", "ORIG_HEAD", "HEAD")
    (repo / "README.md").write_text("docs\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs and tests only")

    proc, log = run_hook(repo, stub_bin, tmp_path)

    assert not built(log)
    assert "build context excludes" in proc.stdout


def test_rebuilds_when_the_merge_touched_a_template(repo, stub_bin, tmp_path):
    _git(repo, "update-ref", "ORIG_HEAD", "HEAD")
    (repo / "templates" / "page.html").write_text(GOOD_TEMPLATE + "<p>new</p>\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "template change")

    proc, log = run_hook(repo, stub_bin, tmp_path)

    assert built(log), proc.stdout


def test_refuses_to_snapshot_a_template_that_does_not_parse(repo, stub_bin, tmp_path):
    (repo / "templates" / "page.html").write_text(BROKEN_TEMPLATE)

    proc, log = run_hook(repo, stub_bin, tmp_path)

    assert not built(log), "a broken template must not reach the image"
    assert "REFUSING TO BUILD" in proc.stdout
    assert "page.html" in proc.stdout


def test_names_uncommitted_files_before_snapshotting_them(repo, stub_bin, tmp_path):
    (repo / "templates" / "other.html").write_text(GOOD_TEMPLATE)

    proc, log = run_hook(repo, stub_bin, tmp_path)

    assert built(log)
    assert "working tree is dirty" in proc.stdout
    assert "templates/other.html" in proc.stdout


def test_yields_to_a_deploy_holding_the_autopilot_lock(repo, stub_bin, tmp_path):
    lock_path = tmp_path / "deploy.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time\n"
            "f=open(sys.argv[1],'w')\n"
            "fcntl.flock(f, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        proc, log = run_hook(repo, stub_bin, tmp_path)
    finally:
        holder.kill()
        holder.wait()

    assert not built(log)
    assert "deploy is already in progress" in proc.stdout


def test_reports_an_unhealthy_rebuild_instead_of_claiming_success(
    repo, stub_bin, tmp_path
):
    proc, log = run_hook(repo, stub_bin, tmp_path, CURL_BODY='{"ok":false}')

    assert built(log)
    assert "DID NOT REPORT ok:true" in proc.stdout
    assert "health OK" not in proc.stdout


def test_reports_a_failed_build_instead_of_claiming_success(repo, stub_bin, tmp_path):
    proc, log = run_hook(repo, stub_bin, tmp_path, DOCKER_RC="1")

    assert "REBUILD FAILED" in proc.stdout
    assert proc.returncode == 0, "a failed rebuild must not fail the git command"


def test_bypass_env_var(repo, stub_bin, tmp_path):
    proc, log = run_hook(repo, stub_bin, tmp_path, SKIP_AUTO_REBUILD="1")

    assert not built(log)
    assert "SKIP_AUTO_REBUILD=1" in proc.stdout


def test_git_pull_actually_invokes_the_hook(repo, stub_bin, tmp_path):
    """The wiring, not the logic: core.hooksPath + a real fast-forward pull."""
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(repo), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "core.hooksPath", ".githooks")
    _git(clone, "checkout", "main")

    (repo / "templates" / "page.html").write_text(GOOD_TEMPLATE + "<p>merged</p>\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "upstream change")

    docker_log = tmp_path / "pull-docker.log"
    docker_log.touch()
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "DOCKER_PS_OUTPUT": "idealista-app",
            "CURL_BODY": '{"ok":true}',
            "AUTO_REBUILD_PYTHON": sys.executable,
            "AUTOPILOT_LOCK_DIR": str(tmp_path / "pull-deploy.lock"),
            "AUTO_REBUILD_HEALTH_TIMEOUT": "5",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    pull = subprocess.run(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
    )

    assert pull.returncode == 0, pull.stderr
    assert built(docker_log.read_text()), (
        f"a fast-forward pull did not reach the hook:\n{pull.stdout}\n{pull.stderr}"
    )
