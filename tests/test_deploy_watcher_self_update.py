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

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_UPDATE_TEST = REPO_ROOT / "tools" / "autopilot" / "deploy_self_update_test.sh"

# Nine watcher runs against stubs, each preceded by building a small repository.
TIMEOUT_SECONDS = 300


def _interpreters() -> list[str]:
    """/bin/bash, plus any *different* bash this machine also has.

    The LaunchAgent execs /bin/bash, so that is the case production runs and
    the one that must always be covered. The second exists because /bin/bash
    is bash 3.2.57 on the owner's Macs and bash 5 on the Linux CI runner: a
    scenario built on syntax the two disagree about passes on one machine and
    fails on the other, which is exactly how this suite first went red in CI
    (run 31868366707). Where a second bash is installed the suite is run under
    it too, so that divergence is found here rather than on the runner. On the
    runner `bash` resolves to /bin/bash and this adds nothing.
    """
    interpreters = ["/bin/bash"]
    other = shutil.which("bash")
    if other and os.path.realpath(other) != os.path.realpath("/bin/bash"):
        interpreters.append(other)
    return interpreters


@pytest.mark.skipif(
    not SELF_UPDATE_TEST.exists(), reason="deploy self-update test not present"
)
@pytest.mark.parametrize("interpreter", _interpreters())
def test_a_watcher_change_is_deployed_by_the_watcher_it_brings(interpreter):
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
        env={**os.environ, "WATCHER_BASH": interpreter},
    )

    assert result.returncode == 0, (
        "the watcher deployed its own change while running the version it replaced\n"
        f"watcher run by: {interpreter}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
