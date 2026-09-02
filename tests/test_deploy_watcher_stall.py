"""Production quietly stopped receiving deploys, and nothing said so (#532).

On 2026-09-01 the mini's checkout sat on branch `codex/issue-473` with five
uncommitted files from 07:43 to 16:03. `tools/autopilot/deploy_watcher.sh`
refused every five-minute tick - correctly, and it must keep refusing: the
refusal is what keeps it off another session's work - while two merged commits
never reached production. `/api/healthz` was green because the OLD image was
healthy, the page check passed because the OLD page rendered, and every
liveness signal this project has answers "is the app serving" and none "is the
app current". The gap was found by accident, in the log, eight hours later.

So a tick that ends without deploying while `origin/main` is ahead of
`data/.deployed_sha` is counted, and from a threshold every such tick logs one
grep-able `STALLED:` line and writes `data/.deploy_stalled` for anyone who reads
files rather than logs. The alarm leads to a person: nothing on that path
deploys, stashes, switches branches or resets a tree, and the shell test
wrapped here asserts what did NOT happen as carefully as what did.

The scenarios that need the watcher *inside* the repository it deploys - the
parse-gate refusal and the handover's continuity - live in
`deploy_self_update_test.sh`; the rest are here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STALL_TEST = REPO_ROOT / "tools" / "autopilot" / "deploy_stall_test.sh"

# Nine scenarios, twenty-two watcher runs against stubs, two of which build and
# poll a loopback health stub that answers at once. The bound exists to fail a
# hang, not to pace a slow machine.
TIMEOUT_SECONDS = 300


def _interpreters() -> list[str]:
    """/bin/bash, plus any *different* bash this machine also has.

    The LaunchAgent execs /bin/bash (3.2.57 on the owner's Macs), so that is
    the case production runs and the one that must always be covered; the CI
    runner's /bin/bash is a bash 5, so a scenario built on syntax the two
    disagree about would pass on one machine and fail on the other. Where a
    second bash is installed the suite runs under it too.
    """
    interpreters = ["/bin/bash"]
    other = shutil.which("bash")
    if other and os.path.realpath(other) != os.path.realpath("/bin/bash"):
        interpreters.append(other)
    return interpreters


@pytest.mark.skipif(not STALL_TEST.exists(), reason="deploy stall test not present")
@pytest.mark.parametrize("interpreter", _interpreters())
def test_consecutive_refusals_while_main_is_ahead_raise_the_alarm(interpreter):
    if shutil.which("git") is None:
        pytest.skip("the test builds a throwaway git repository")
    if shutil.which("curl") is None:
        pytest.skip("the watcher polls healthz and /properties with curl")
    if shutil.which("nc") is None:
        pytest.skip("the test picks a free port with nc")

    result = subprocess.run(
        ["/bin/bash", str(STALL_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
        env={**os.environ, "WATCHER_BASH": interpreter},
    )

    assert result.returncode == 0, (
        "the watcher let production fall behind main in silence, or the alarm "
        "did something other than alarm\n"
        f"watcher run by: {interpreter}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
