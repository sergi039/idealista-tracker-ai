"""A deploy must name the work it kills, and "healthy" must mean a page rendered.

Two incidents on 2026-08-14, one contract each:

  - a pool backfill was running inside `idealista-app` when a merge landed;
    `docker compose up -d --build` recreated the container and the run died
    mid-flight with nothing recording it. The watcher logged an ordinary
    successful deploy, because it had no notion of work in flight (#283).
  - a `TemplateSyntaxError` turned every `/properties/<id>` into a redirect for
    15 minutes while `/api/healthz` stayed green - healthz renders no template,
    so it cannot see a broken one.

Which page proves that, and what counts as rendered, is one contract shared
with `.githooks/post-merge` (#292): the scenarios move `DEPLOY_RENDER_PATH` and
require the watcher to follow it, so a copy of the rule kept here would fail.

The watcher lives in bash; this wrapper runs its shell test and surfaces the
output on failure, the same way `test_deploy_watcher_marker.py` does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INFLIGHT_TEST = REPO_ROOT / "tools" / "autopilot" / "deploy_inflight_test.sh"

# Thirty-five scenarios, 39 watcher runs against stubs, two of which wait out a
# short health timeout. Measured 27.9 s wall clock on the development Mac on
# 2026-08-15, so the bound below is roughly eleven times the observed cost and
# exists to fail a hang, not to pace a slow machine. Re-measure before lowering
# it; a timeout that fires on a busy CI runner reads as a broken deploy gate.
TIMEOUT_SECONDS = 300


@pytest.mark.skipif(
    not INFLIGHT_TEST.exists(), reason="deploy in-flight test not present"
)
def test_deploy_reports_inflight_work_and_verifies_a_page():
    if shutil.which("git") is None:
        pytest.skip("the test builds a throwaway git repository")
    if shutil.which("curl") is None:
        pytest.skip("the watcher polls healthz and /properties with curl")
    if shutil.which("nc") is None:
        pytest.skip("the test picks a free port with nc")

    result = subprocess.run(
        ["bash", str(INFLIGHT_TEST)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        "the watcher killed work silently, or called a redirecting page healthy\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
