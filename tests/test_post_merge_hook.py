"""Contract for .githooks/post-merge (auto-rebuild after a merge).

The hook exists because an image is a `COPY . .` snapshot that nothing
re-takes: on 2026-08-14 a container served a broken template for 15 minutes
across the fix, its commit and its merge. What has to hold is narrow and easy
to break by accident, so each rule is pinned here.

The stubs assert their own arguments on purpose. An earlier version answered
any `docker ps` and any `curl`, and a mutation run proved it worthless:
pointing the hook at the wrong container, dropping the name filter, defaulting
to docker-compose.dev.yml and aiming health at a dead port all kept 12 tests
green. A stub that ignores its inputs pins nothing. Against these tests the
same exercise fails - accepting a green healthz without rendering a page,
dropping the rollback checkpoint, and guessing the container name from the
shell instead of asking compose each break at least one test.
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

# Answers only for the stack it was told to expect, and records every call.
DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$1" in
    compose)
        case "$*" in
            *"-f ${EXPECT_COMPOSE_FILE} "*) ;;
            *)
                printf 'docker stub: unexpected compose file: %s\\n' "$*" >&2
                exit 90
                ;;
        esac
        case "$*" in
            *" ps "*)
                printf '%s\\n' "${DOCKER_PS_OUTPUT:-}"
                exit 0
                ;;
            *" port "*)
                printf '%s\\n' "${DOCKER_PORT_OUTPUT:-127.0.0.1:5001}"
                exit 0
                ;;
            *"up -d --build") exit "${DOCKER_BUILD_RC:-0}" ;;
            *"up -d --no-build") exit "${DOCKER_RESTORE_RC:-0}" ;;
        esac
        exit 0
        ;;
    inspect)
        [ -n "${DOCKER_IMAGE_NAME:-}" ] || exit 1
        printf '%s\\n' "$DOCKER_IMAGE_NAME"
        exit 0
        ;;
    tag) exit "${DOCKER_TAG_RC:-0}" ;;
esac
exit 0
"""

# Answers only for the URLs the hook derived from compose.
CURL_STUB = """#!/bin/sh
for a in "$@"; do url="$a"; done
case "$url" in
    "$EXPECT_HEALTH_URL")
        printf '%s' "${CURL_BODY:-}"
        exit "${CURL_RC:-0}"
        ;;
    "$EXPECT_RENDER_URL")
        printf '%s' "${CURL_RENDER_CODE:-200}"
        exit 0
        ;;
esac
printf 'curl stub: unexpected url %s\\n' "$url" >&2
exit 7
"""

GOOD_TEMPLATE = "{% block body %}{% if x %}ok{% endif %}{% endblock %}\n"
# The 2026-08-14 defect itself: one endif more than there are ifs.
BROKEN_TEMPLATE = "{% block body %}{% if x %}ok{% endif %}{% endif %}{% endblock %}\n"

WATCHER_PLIST = "Library/LaunchAgents/com.idealista.deploy-watcher.plist"


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
    (work / "app.py").write_text("x = 1\n")
    (work / "docker-compose.yml").write_text("services: {}\n")

    _git(work, "init", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
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


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A HOME with no deploy-watcher LaunchAgent in it."""
    fake = tmp_path / "home"
    (fake / "Library" / "LaunchAgents").mkdir(parents=True)
    return fake


def hook_env(stub_bin: Path, home: Path, tmp_path: Path, log_name: str) -> dict:
    docker_log = tmp_path / log_name
    docker_log.touch()
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "DOCKER_LOG": str(docker_log),
            "DOCKER_PS_OUTPUT": "idealista-app",
            "DOCKER_PORT_OUTPUT": "127.0.0.1:5001",
            "DOCKER_IMAGE_NAME": "idealistarank-app",
            "EXPECT_COMPOSE_FILE": "docker-compose.yml",
            "EXPECT_HEALTH_URL": "http://127.0.0.1:5001/api/healthz",
            "EXPECT_RENDER_URL": "http://127.0.0.1:5001/properties",
            "CURL_BODY": '{"ok":true}',
            "CURL_RENDER_CODE": "200",
            "AUTO_REBUILD_PYTHON": sys.executable,
            "AUTOPILOT_LOCK_DIR": str(tmp_path / log_name) + ".lock",
            "AUTO_REBUILD_HEALTH_TIMEOUT": "4",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    for key in (
        "SKIP_AUTO_REBUILD",
        "AUTO_REBUILD_BRANCH",
        "AUTO_REBUILD_BASE_URL",
        "AUTO_REBUILD_RENDER_PATH",
        "COMPOSE_CONTAINER_PREFIX",
    ):
        env.pop(key, None)
    return env


def run_hook(repo: Path, stub_bin: Path, home: Path, tmp_path: Path, **overrides: str):
    env = hook_env(stub_bin, home, tmp_path, "docker.log")
    env.update(overrides)
    proc = subprocess.run(
        [str(repo / ".githooks" / "post-merge")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, Path(env["DOCKER_LOG"]).read_text()


def built(docker_log: str) -> bool:
    return any(line.endswith("up -d --build") for line in docker_log.splitlines())


def no_jinja_python(tmp_path: Path, name: str) -> Path:
    """A real interpreter that simply cannot import jinja2.

    Not a stub that fails at everything: tools/autopilot/lib/lock.sh needs a
    working python3 for its flock, so breaking the interpreter outright would
    make the hook skip on the lock and prove nothing about the gate.
    """
    where = tmp_path / name
    where.mkdir()
    shim = where / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ] && [ "$2" = "import jinja2" ]; then exit 1; fi\n'
        f'exec {sys.executable} "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def test_rebuilds_when_main_moves(repo, stub_bin, home, tmp_path):
    proc, log = run_hook(repo, stub_bin, home, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert built(log), f"expected a rebuild, docker saw:\n{log}"
    # A rendered page, not just healthz - healthz renders no template.
    assert "http://127.0.0.1:5001/properties 200" in proc.stdout
    # The stack is named, not guessed: the compose file reaches every call.
    assert "compose -f docker-compose.yml up -d --build" in log
    assert "compose -f docker-compose.yml ps --status running" in log


def test_gates_on_the_stack_compose_reports_not_on_a_guessed_name(
    repo, stub_bin, home, tmp_path
):
    """A worktree .env renames the containers; compose knows, the shell does not."""
    proc, log = run_hook(
        repo,
        stub_bin,
        home,
        tmp_path,
        DOCKER_PS_OUTPUT="wt1-app",
        DOCKER_IMAGE_NAME="wt1rank-app",
        COMPOSE_CONTAINER_PREFIX="idealista",
    )

    assert built(log), proc.stdout
    assert "inspect --format {{.Config.Image}} wt1-app" in log
    assert "tag wt1rank-app wt1rank-app:post-merge-rollback" in log


def test_derives_the_urls_from_the_published_port(repo, stub_bin, home, tmp_path):
    """APP_HOST_PORT lives in the project .env, so ask compose, do not assume."""
    proc, _ = run_hook(
        repo,
        stub_bin,
        home,
        tmp_path,
        DOCKER_PORT_OUTPUT="127.0.0.1:5101",
        EXPECT_HEALTH_URL="http://127.0.0.1:5101/api/healthz",
        EXPECT_RENDER_URL="http://127.0.0.1:5101/properties",
    )

    assert "http://127.0.0.1:5101/properties 200" in proc.stdout, (
        proc.stdout + proc.stderr
    )


def test_rolls_back_when_healthz_is_green_but_the_page_redirects(
    repo, stub_bin, home, tmp_path
):
    """The 2026-08-14 blind spot: healthz renders no template, the route
    swallows TemplateSyntaxError into a 302, and the container looks fine."""
    proc, log = run_hook(repo, stub_bin, home, tmp_path, CURL_RENDER_CODE="302")

    assert built(log)
    assert "answered 302" in proc.stdout
    assert "tag idealistarank-app:post-merge-rollback idealistarank-app" in log
    assert any(line.endswith("up -d --no-build") for line in log.splitlines())


def test_leaves_a_branch_checkout_alone(repo, stub_bin, home, tmp_path):
    _git(repo, "checkout", "-b", "claude/some-work")

    proc, log = run_hook(repo, stub_bin, home, tmp_path)

    assert not built(log)
    assert "not 'main'" in proc.stdout


def test_stands_down_where_the_deploy_watcher_is_installed(
    repo, stub_bin, home, tmp_path
):
    """One deployer per machine: the watcher owns rollback and the marker."""
    (home / WATCHER_PLIST).write_text("<plist/>\n")

    proc, log = run_hook(repo, stub_bin, home, tmp_path)

    assert not built(log)
    assert "deploy_watcher.sh owns this machine" in proc.stdout


def test_skips_a_linked_worktree(repo, stub_bin, home, tmp_path):
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), "main")

    env = hook_env(stub_bin, home, tmp_path, "docker.log")
    proc = subprocess.run(
        [str(repo / ".githooks" / "post-merge")],
        cwd=linked,
        env=env,
        capture_output=True,
        text=True,
    )

    assert not built(Path(env["DOCKER_LOG"]).read_text())
    assert "linked worktree" in proc.stdout


def test_leaves_a_stopped_stack_stopped(repo, stub_bin, home, tmp_path):
    proc, log = run_hook(repo, stub_bin, home, tmp_path, DOCKER_PS_OUTPUT="")

    assert not built(log)
    assert "is not running here" in proc.stdout


def test_refuses_to_snapshot_a_template_that_does_not_parse(
    repo, stub_bin, home, tmp_path
):
    (repo / "templates" / "page.html").write_text(BROKEN_TEMPLATE)

    proc, log = run_hook(repo, stub_bin, home, tmp_path)

    assert not built(log), "a broken template must not reach the image"
    assert "REFUSING TO BUILD" in proc.stdout
    assert "page.html" in proc.stdout


def test_refuses_to_snapshot_python_that_does_not_parse(repo, stub_bin, home, tmp_path):
    """The 2026-08-14 shape, in a file the template gate never looked at."""
    (repo / "services.py").write_text("def broken(:\n    pass\n")

    proc, log = run_hook(repo, stub_bin, home, tmp_path)

    assert not built(log)
    assert "REFUSING TO BUILD" in proc.stdout
    assert "services.py" in proc.stdout


def test_refuses_when_the_check_cannot_run_and_the_tree_is_dirty(
    repo, stub_bin, home, tmp_path
):
    """No jinja2 anywhere is a refusal, not a pass - that was the 11:59 image."""
    (repo / "templates" / "other.html").write_text(GOOD_TEMPLATE)
    shim = no_jinja_python(tmp_path, "nojinja")

    proc, log = run_hook(
        repo,
        stub_bin,
        home,
        tmp_path,
        AUTO_REBUILD_PYTHON=str(shim),
        PATH=f"{shim.parent}{os.pathsep}{stub_bin}{os.pathsep}{os.environ['PATH']}",
    )

    assert not built(log)
    assert "REFUSING TO BUILD" in proc.stdout
    assert "jinja2" in proc.stdout


def test_says_so_when_it_builds_a_clean_tree_it_could_not_check(
    repo, stub_bin, home, tmp_path
):
    shim = no_jinja_python(tmp_path, "nojinja2")

    proc, log = run_hook(
        repo,
        stub_bin,
        home,
        tmp_path,
        AUTO_REBUILD_PYTHON=str(shim),
        PATH=f"{shim.parent}{os.pathsep}{stub_bin}{os.pathsep}{os.environ['PATH']}",
    )

    assert built(log)
    assert "not parsed locally" in proc.stdout, proc.stdout


def test_names_uncommitted_files_before_snapshotting_them(
    repo, stub_bin, home, tmp_path
):
    (repo / "templates" / "other.html").write_text(GOOD_TEMPLATE)

    proc, log = run_hook(repo, stub_bin, home, tmp_path)

    assert built(log)
    assert "working tree is dirty" in proc.stdout
    assert "templates/other.html" in proc.stdout


def test_checkpoints_the_image_before_building(repo, stub_bin, home, tmp_path):
    _, log = run_hook(repo, stub_bin, home, tmp_path)

    lines = log.splitlines()
    tag = next(i for i, line in enumerate(lines) if line.startswith("tag "))
    build = next(i for i, line in enumerate(lines) if line.endswith("up -d --build"))
    assert tag < build, f"the checkpoint must precede the build:\n{log}"
    assert lines[tag] == "tag idealistarank-app idealistarank-app:post-merge-rollback"


def test_rolls_the_image_back_when_the_rebuild_is_unhealthy(
    repo, stub_bin, home, tmp_path
):
    proc, log = run_hook(repo, stub_bin, home, tmp_path, CURL_BODY='{"ok":false}')

    assert built(log)
    assert "DID NOT REPORT ok:true" in proc.stdout
    assert "tag idealistarank-app:post-merge-rollback idealistarank-app" in log
    assert any(line.endswith("up -d --no-build") for line in log.splitlines())
    assert "ROLLBACK IS ALSO UNHEALTHY" in proc.stdout
    assert "health OK" not in proc.stdout


def test_rolls_back_a_failed_build_too(repo, stub_bin, home, tmp_path):
    proc, log = run_hook(repo, stub_bin, home, tmp_path, DOCKER_BUILD_RC="1")

    assert "REBUILD FAILED" in proc.stdout
    assert any(line.endswith("up -d --no-build") for line in log.splitlines())
    assert proc.returncode == 0, "a failed rebuild must not fail the git command"


def test_says_so_when_there_is_no_image_to_roll_back_to(repo, stub_bin, home, tmp_path):
    proc, log = run_hook(
        repo, stub_bin, home, tmp_path, DOCKER_IMAGE_NAME="", CURL_BODY='{"ok":false}'
    )

    assert built(log)
    assert "cannot be rolled back" in proc.stdout


def test_yields_to_a_deploy_holding_the_autopilot_lock(repo, stub_bin, home, tmp_path):
    lock_path = tmp_path / "docker.log.lock"
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

        proc, log = run_hook(repo, stub_bin, home, tmp_path)
    finally:
        holder.kill()
        holder.wait()

    assert not built(log)
    assert "holds the deploy lock" in proc.stdout


def test_bypass_env_var(repo, stub_bin, home, tmp_path):
    proc, log = run_hook(repo, stub_bin, home, tmp_path, SKIP_AUTO_REBUILD="1")

    assert not built(log)
    assert "SKIP_AUTO_REBUILD=1" in proc.stdout


def test_git_pull_actually_invokes_the_hook(repo, stub_bin, home, tmp_path):
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

    env = hook_env(stub_bin, home, tmp_path, "pull-docker.log")
    pull = subprocess.run(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
    )

    assert pull.returncode == 0, pull.stderr
    assert built(Path(env["DOCKER_LOG"]).read_text()), (
        f"a fast-forward pull did not reach the hook:\n{pull.stdout}\n{pull.stderr}"
    )
