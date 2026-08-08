"""The autopilot lock is the only thing preventing two concurrent deploys.

It lives in bash (tools/autopilot/lib/lock.sh) because the scripts that use it
do, but it is load-bearing enough to belong in CI rather than in a file someone
remembers to run. This wrapper executes the shell test suite and surfaces its
output on failure.

The shell suite covers the case that actually broke an earlier implementation:
a holder killed with SIGKILL, then several ticks contending at once.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_TEST = REPO_ROOT / "tools" / "autopilot" / "lib" / "lock_race_test.sh"

# 10 rounds x 8 processes, twice over, plus the orphan setup.
TIMEOUT_SECONDS = 180


@pytest.mark.skipif(not LOCK_TEST.exists(), reason="autopilot lock test not present")
def test_autopilot_lock_admits_exactly_one_holder():
    if shutil.which("python3") is None:
        pytest.skip("lock.sh needs python3 for flock(2)")

    result = subprocess.run(
        ["bash", str(LOCK_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        "tools/autopilot/lib/lock_race_test.sh failed - concurrent autopilot "
        f"runs are possible:\n{result.stdout}\n{result.stderr}"
    )
