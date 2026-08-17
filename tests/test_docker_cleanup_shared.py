"""The deploy housekeeping contract: tools/autopilot/lib/docker_cleanup.sh.

Both deployers sweep what a build leaves behind, and they read one copy of the
rule for the #292 reason. What makes this worth pinning is not the sweeping -
it is everything the sweep must refuse to touch, none of which is visible in
the command it does run:

  * `docker image prune -a` would delete the rollback image, because -a removes
    every image no *container* uses and a rollback tag is exactly that.
  * an unscoped prune would collect other projects off a shared daemon.
  * a stopped idealista-db carries the project label too, so the one-off label
    is the only thing standing between housekeeping and the stack's own
    containers.
  * a job the deploy just killed is an exited one-off seconds old, and its
    container log is the only record of how far it got.

Every stub call is asserted: an earlier generation of deployer tests answered
anything and stayed green while pointing the hook at the wrong container (see
the header of tests/test_post_merge_hook.py), so this stub exits 91 on a call
it was not told to expect.
"""

import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "tools" / "autopilot" / "lib" / "docker_cleanup.sh"
WATCHER = REPO_ROOT / "tools" / "autopilot" / "deploy_watcher.sh"
HOOK = REPO_ROOT / ".githooks" / "post-merge"

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$1" in
    inspect)
        id="$2"
        case "$*" in
            *com.docker.compose.oneoff*)
                printf '%s\\n' "${STUB_META:-}" | while IFS= read -r row; do
                    case "$row" in
                        "$id "*) printf '%s\\n' "${row#* }" ;;
                    esac
                done
                exit 0
                ;;
            *com.docker.compose.project*)
                [ -n "${STUB_PROJECT:-}" ] || exit 1
                printf '%s\\n' "$STUB_PROJECT"
                exit 0
                ;;
        esac
        printf 'docker stub: unexpected inspect: %s\\n' "$*" >&2
        exit 91
        ;;
    ps)
        case "$*" in
            *"--filter status=exited"*) ;;
            *)
                printf 'docker stub: ps without status=exited: %s\\n' "$*" >&2
                exit 91
                ;;
        esac
        printf '%s\\n' "${STUB_PS:-}"
        exit 0
        ;;
    rm) exit "${STUB_RM_RC:-0}" ;;
    image)
        case "$*" in
            *" prune "*)
                printf 'Total reclaimed space: %s\\n' "${STUB_IMAGE_FREED:-1.5GB}"
                exit "${STUB_IMAGE_RC:-0}"
                ;;
        esac
        printf 'docker stub: unexpected image command: %s\\n' "$*" >&2
        exit 91
        ;;
    buildx)
        case "$*" in
            *" prune "*)
                if [ "${STUB_BUILDX_RC:-0}" = "0" ]; then
                    printf 'Total:\\t%s\\n' "${STUB_BUILDX_FREED:-3GB}"
                    exit 0
                fi
                printf 'unknown flag: --max-used-space\\n' >&2
                exit 125
                ;;
        esac
        printf 'docker stub: unexpected buildx command: %s\\n' "$*" >&2
        exit 91
        ;;
esac
printf 'docker stub: unexpected command: %s\\n' "$*" >&2
exit 91
"""


def _stamp(hours_ago):
    """An RFC3339 FinishedAt the way docker writes it."""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(time.time() - hours_ago * 3600)
    )


@pytest.fixture
def run_cleanup(tmp_path):
    """Source the lib and call deploy_cleanup() against a stub docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    # The shebang has to sit at byte 0 - a blank first line makes execve return
    # ENOEXEC and bash re-executes the stub with itself (#284).
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")

    def _run(container="idealista-app", **env):
        full_env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "DOCKER_LOG": str(log),
            "STUB_PROJECT": "idealistarank",
        }
        full_env.update({k: str(v) for k, v in env.items()})
        proc = subprocess.run(
            [
                "bash",
                "-c",
                f'set -euo pipefail; . "{LIB}"; deploy_cleanup "{container}"',
            ],
            capture_output=True,
            text=True,
            env=full_env,
        )
        assert "docker stub:" not in proc.stderr, proc.stderr
        return proc, log.read_text()

    return _run


def test_an_old_exited_one_off_is_removed(run_cleanup):
    proc, log = run_cleanup(
        STUB_PS="abc123 sea-backfill-restart",
        STUB_META=f"abc123 True {_stamp(48)}",
    )
    assert proc.returncode == 0, proc.stderr
    assert "rm abc123" in log, log
    assert "removed exited one-off container sea-backfill-restart" in proc.stdout


def test_a_job_the_deploy_just_killed_is_kept(run_cleanup):
    """Its container log is the only record of how far it got."""
    proc, log = run_cleanup(
        STUB_PS="abc123 idealista-pool-resume",
        STUB_META=f"abc123 True {_stamp(1)}",
    )
    assert "rm abc123" not in log, log
    assert "kept" in proc.stdout


def test_a_container_without_the_one_off_label_is_never_removed(run_cleanup):
    """A stopped idealista-db carries the project label and must survive it."""
    proc, log = run_cleanup(
        STUB_PS="db0001 idealista-db",
        STUB_META=f"db0001  {_stamp(240)}",
    )
    assert "rm db0001" not in log, log


def test_an_unparsable_end_time_keeps_the_container(run_cleanup):
    """A probe that could not answer must not read as 'old enough to delete'."""
    proc, log = run_cleanup(
        STUB_PS="zzz999 never-ran",
        STUB_META="zzz999 True 0001-01-01T00:00:00Z",
    )
    assert "rm zzz999" not in log, log
    assert "kept" in proc.stdout


def test_the_image_prune_is_scoped_and_never_uses_dash_a(run_cleanup):
    proc, log = run_cleanup()
    prunes = [line for line in log.splitlines() if line.startswith("image prune")]
    assert len(prunes) == 1, log
    assert "--filter label=com.docker.compose.project=idealistarank" in prunes[0]
    assert " -a" not in prunes[0] and "--all" not in prunes[0], prunes[0]
    assert "reclaimed 1.5GB" in proc.stdout


def test_the_project_is_read_from_the_container_never_guessed(run_cleanup):
    """No label, no cleanup - guessing the project is how a sweep leaves its lane."""
    proc, log = run_cleanup(STUB_PROJECT="")
    assert "carries no compose project label" in proc.stdout
    assert "prune" not in log, log
    assert proc.returncode == 0


def test_the_build_cache_is_capped_not_emptied(run_cleanup):
    proc, log = run_cleanup()
    prunes = [line for line in log.splitlines() if line.startswith("buildx prune")]
    assert len(prunes) == 1, log
    assert "--max-used-space 5GB" in prunes[0], prunes[0]
    assert "capped at 5GB, freed 3GB" in proc.stdout


def test_a_rejected_cap_does_not_fall_back_to_emptying_the_cache(run_cleanup):
    """`docker buildx prune` with no cap keeps nothing - a silent escalation."""
    proc, log = run_cleanup(STUB_BUILDX_RC=1)
    prunes = [line for line in log.splitlines() if line.startswith("buildx prune")]
    assert len(prunes) == 1, log
    assert "--max-used-space" in prunes[0]
    assert "capping the build cache at 5GB failed" in proc.stdout


def test_an_age_that_is_not_a_number_falls_back_instead_of_aborting(run_cleanup):
    """ "24h" here is an arithmetic syntax error, and the watcher runs set -e."""
    proc, log = run_cleanup(
        DEPLOY_CLEANUP_ONEOFF_MIN_AGE_H="24h",
        STUB_PS="abc123 sea-backfill-restart",
        STUB_META=f"abc123 True {_stamp(48)}",
    )
    assert proc.returncode == 0, proc.stderr
    assert "is not a whole number of hours - using 24" in proc.stdout
    # And the sweep still happened under the fallback.
    assert "rm abc123" in log, log


def test_the_switch_stops_every_call(run_cleanup):
    proc, log = run_cleanup(DEPLOY_CLEANUP=0)
    assert log.strip() == "", log
    assert "skipped (DEPLOY_CLEANUP=0)" in proc.stdout


def test_nothing_here_may_fail_a_deploy(run_cleanup):
    """A deploy that is already serving does not fail over an image prune."""
    proc, _ = run_cleanup(STUB_IMAGE_RC=1, STUB_RM_RC=1, STUB_BUILDX_RC=1)
    assert proc.returncode == 0, proc.stderr


def _rollback_body():
    """The watcher's rollback() function, brace-matched from its definition."""
    text = WATCHER.read_text()
    start = text.index("\nrollback() {")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("rollback() is not brace-balanced")


def test_the_rollback_path_never_sweeps():
    """There the previous image is what is being restored, not collected."""
    assert "deploy_cleanup" not in _rollback_body()


def test_the_watcher_sweeps_only_after_the_deploy_is_recorded():
    text = WATCHER.read_text()
    # The invocation, not the `command -v` that vets the contract.
    assert text.count('deploy_cleanup "') == 1, (
        "one call site, or the order below is moot"
    )
    assert text.index('deploy_cleanup "') > text.index("record_deployed "), (
        "housekeeping must not run before the deploy is proven and recorded"
    )


@pytest.mark.parametrize("consumer", [WATCHER, HOOK], ids=["watcher", "post-merge"])
def test_both_deployers_read_the_shared_lib_and_grow_no_prune_of_their_own(consumer):
    text = consumer.read_text()
    assert "lib/docker_cleanup.sh" in text, (
        f"{consumer.name} does not load the contract"
    )
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("system prune", "--remove-orphans"):
        assert forbidden not in body, f"{consumer.name} grew a {forbidden}"
    # Pruning belongs to the lib. A second copy in a consumer is the shape of
    # defect #292 exists to stop: one rule, two homes, shipped half-changed.
    assert not re.search(r"docker\s+(image|builder|buildx|container)\s+prune", body), (
        f"{consumer.name} prunes on its own instead of calling deploy_cleanup"
    )
