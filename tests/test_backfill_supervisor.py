"""A long backfill has to survive the deploys that keep killing it.

Every `docker compose up -d --build` recreates the app container and kills
whatever runs inside it. On 2026-08-14 a Phase-2 pool backfill was
interrupted four times in one afternoon by other sessions' merges. Each
interruption cost one property — the backfills commit per row and their
scope drops finished rows — but each one also needed a human to notice and
restart, and the throwaway script used on the mini that day got three things
wrong before it worked:

  - its budget counted loop ticks, so it gave up after an hour of perfectly
    healthy running;
  - a `docker exec` that failed during a rebuild looked like "no job
    running", which would have started a second copy of a live job;
  - a reused `--snapshot` path makes the restarted backfill exit instead of
    run, because the tools refuse to overwrite a rollback point.

An independent Tier-2 audit then broke the first version of those fixes, and
two of its findings were reproduced by probe before being fixed:

  - `docker exec` failing while it still printed a partial listing slipped
    past the empty-output check, and the supervisor started the paid backfill
    twice while the real one was alive. Nonzero status is "could not tell",
    whatever came out of it;
  - a `Done` left in the append-only run log by an earlier run made the next
    supervision of that module exit 0 at its first tick, having called docker
    zero times, and log it as success. Only bytes this run appended count.

`tools/backfill_supervisor.sh` is that script, made honest. This wrapper runs
its shell test and surfaces the output on failure, the way
`test_deploy_watcher_inflight.py` does for the watcher.

The supervisor deliberately does not hold deploys: deferring one for a
resumable job is the worse trade (#283 asks the *watcher* to name what it
killed, and leaves restarting to something like this).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_TEST = REPO_ROOT / "tools" / "backfill_supervisor_test.sh"

# Sixteen scenarios, most polling a stub for a few one-second ticks.
TIMEOUT_SECONDS = 300


@pytest.mark.skipif(
    not SUPERVISOR_TEST.exists(), reason="supervisor shell test not present"
)
def test_supervisor_restarts_only_what_it_should():
    if shutil.which("timeout") is None:
        pytest.skip("the shell test bounds each supervisor run with timeout(1)")

    result = subprocess.run(
        ["bash", str(SUPERVISOR_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        "the supervisor restarted a live job, treated an unreadable container "
        "as idle, reused a snapshot path, or gave up while healthy\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
