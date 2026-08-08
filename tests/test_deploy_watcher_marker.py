"""The deployment marker must never name a build that is not serving.

The watcher lives in bash, but this particular claim is the one that decides
whether continuous deployment keeps working at all: once the marker equals HEAD,
every later tick concludes there is nothing to do. On 2026-08-08 a rollback
rebuild failed while the untouched previous container kept answering healthz,
the marker was written anyway, and the watcher went quiet for good - with the
merge bot and the issue runner still reporting success upstream of it.

This wrapper runs the shell test and surfaces its output on failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKER_TEST = REPO_ROOT / "tools" / "autopilot" / "deploy_marker_test.sh"

# One watcher run against stubs: no real build, no real registry.
TIMEOUT_SECONDS = 120


@pytest.mark.skipif(not MARKER_TEST.exists(), reason="deploy marker test not present")
def test_failed_rollback_rebuild_leaves_no_marker():
    if shutil.which("git") is None:
        pytest.skip("the test builds a throwaway git repository")
    if shutil.which("curl") is None:
        pytest.skip("the watcher polls healthz with curl")

    result = subprocess.run(
        ["bash", str(MARKER_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        "the watcher recorded a deployment that never happened\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
