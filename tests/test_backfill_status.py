"""A liveness check is not a claim about the next minute (#338).

On 2026-08-16 a session asked the documented question before starting a paid
backfill, got the correct answer, and started a duplicate anyway:

    09:01:02  the deploy killed a running utils.backfill_pool
    09:01:58  tools/backfill_supervisor.sh ticked
    09:01:59  ...and restarted it in the rebuilt container
    09:03:51  a second session, having seen an empty process list, started its
              own run of the same module

Both runs then measured the same three properties, two measurements were
overwritten by refusals, and three rows were billed to Google twice (#339).

Nothing was wrong with the check. `docker top` is authoritative about the
instant it runs, and in those 57 seconds the container really did hold no job.
What it cannot express is that a kill makes the process list read empty
*precisely when* a supervisor is about to refill it — and every deploy
manufactures one of those windows.

CLAUDE.md already said a marker is not a liveness check. This is the converse,
and `tools/backfill_status.sh` is the answer to it: the three sources that each
know a different part — what runs now, what is expected shortly, and what
started and never cleaned up — read together, in three states, with unknown
blocking exactly like busy.

This wrapper runs its shell test the way `test_backfill_supervisor.py` does for
the supervisor. It needs no binary the suite does not already require, so it
never skips.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_TEST = REPO_ROOT / "tools" / "backfill_status_test.sh"

# Nine scenarios against stubs, no polling and no sleeps: measured at well
# under a second. The bound exists to fail a hang, not to pace a slow machine.
TIMEOUT_SECONDS = 120


def test_status_answers_what_docker_top_cannot():
    result = subprocess.run(
        ["bash", str(STATUS_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        "backfill_status read an empty container under a live supervisor as "
        "safe, or an unreadable probe as idle, or a marker as a lock\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
