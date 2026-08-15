"""A deploy of the watcher must be governed by the watcher being deployed.

Observed live on 2026-08-14 16:33:30 (#293): the tick that rolled out #285 ran
the *pre*-#285 `deploy_watcher.sh`. `git merge --ff-only` replaces a file by
renaming a new one over it, so the shell's open descriptor keeps pointing at
the old inode and the tick reads the previous script to its end - reliably, not
intermittently. The deploy that shipped the in-flight survey and the page check
therefore ran with neither, and it killed a pool backfill at 32 ledger rows
silently.

Neither existing watcher test can reach this: both run the real script against
a throwaway repository that does not contain a copy of it, so there is nothing
for a fast-forward to replace. The shell test wrapped here puts the watcher
*inside* the repository it deploys, which is the arrangement on the mini.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_UPDATE_TEST = REPO_ROOT / "tools" / "autopilot" / "deploy_self_update_test.sh"

# Nine watcher runs against stubs, each preceded by building a small repository.
TIMEOUT_SECONDS = 300


@pytest.mark.skipif(
    not SELF_UPDATE_TEST.exists(), reason="deploy self-update test not present"
)
def test_a_watcher_change_is_deployed_by_the_watcher_it_brings():
    if shutil.which("git") is None:
        pytest.skip("the test builds throwaway git repositories")
    if shutil.which("curl") is None:
        pytest.skip("the watcher polls healthz and /properties with curl")
    if shutil.which("nc") is None:
        pytest.skip("the test picks a free port with nc")
    if shutil.which("perl") is None:
        pytest.skip("the test marks the new watcher with perl")

    result = subprocess.run(
        ["bash", str(SELF_UPDATE_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        "the watcher deployed its own change while running the version it replaced\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
