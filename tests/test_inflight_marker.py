"""A long job announces itself, and an interrupted run is reported once (#283).

The marker is the only thing that survives a `docker compose up -d --build`
killing a backfill: the process is gone, the container is new, and the ledger
alone cannot say that a run *stopped* rather than *finished*. So the two
claims pinned here are exactly the two the deploy chain reads:

  - while a job runs, a marker exists saying whether killing it loses work;
  - after a job is killed, the marker it never removed is what tells the next
    run - and the operator - that a run was interrupted.

`resumable` is the load-bearing field. Unknown and false must behave the same
way everywhere, because a deploy reading them cannot tell the difference and
guessing "resumable" is how work gets lost quietly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from utils.inflight import (
    describe,
    inflight,
    read_markers,
    report_interrupted,
    write_marker,
)


def _markers(directory: Path):
    return sorted(p.name for p in directory.glob("*.json"))


def test_marker_exists_while_the_job_runs_and_is_gone_after(tmp_path: Path):
    directory = tmp_path / "inflight"

    with inflight(
        "backfill_pool",
        ledger="data/pool.ledger.jsonl",
        resumable=True,
        argv=["--snapshot", "data/pool.json"],
        directory=str(directory),
    ) as path:
        assert path.exists()
        payload = json.loads(path.read_text())
        assert payload["module"] == "backfill_pool"
        assert payload["pid"] == os.getpid()
        assert payload["resumable"] is True
        assert payload["ledger"] == "data/pool.ledger.jsonl"
        assert payload["argv"] == ["--snapshot", "data/pool.json"]

    assert _markers(directory) == []


def test_marker_is_removed_even_when_the_job_raises(tmp_path: Path):
    directory = tmp_path / "inflight"

    try:
        with inflight("backfill_pool", directory=str(directory)):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert _markers(directory) == [], "a crash must not look like a live job"


def test_an_interrupted_run_is_reported_and_then_cleared(tmp_path, caplog):
    """A SIGKILL skips the removal - that leftover file *is* the report."""
    directory = tmp_path / "inflight"
    directory.mkdir()
    # A marker from a process that no longer exists: pid 0 is never a live
    # process, which is what a killed backfill leaves behind.
    (directory / "backfill_pool.0.json").write_text(
        json.dumps(
            {
                "module": "backfill_pool",
                "pid": 0,
                "argv": ["--snapshot", "data/pool_backfill_20260814b.json"],
                "started_at": "2026-08-14T09:00:00+00:00",
                "resumable": True,
                "ledger": "data/pool_backfill_20260814b.json.ledger.jsonl",
            }
        )
    )

    with caplog.at_level(logging.WARNING):
        interrupted = report_interrupted("backfill_pool", directory=str(directory))

    assert len(interrupted) == 1
    text = caplog.text
    assert "did not finish" in text
    assert "2026-08-14T09:00:00+00:00" in text
    assert "pool_backfill_20260814b.json.ledger.jsonl" in text
    assert _markers(directory) == [], "reporting it twice would be noise, not news"


def test_a_live_run_of_the_same_job_is_not_treated_as_a_corpse(tmp_path, caplog):
    """Two concurrent backfills re-measure the same rows; say so, touch nothing.

    The probe carries the module name in its own argv on purpose: after a
    container recreate the next run starts in a fresh PID namespace, so a
    stale marker's number can belong to an unrelated process. "Still alive"
    has to mean "still this job" - which needs `/proc` to confirm. Where
    `/proc` cannot be read (macOS) the honest answer is "cannot tell", the
    third state, not "still active"; either way the marker is left alone and
    nothing is reported interrupted.
    """
    directory = tmp_path / "inflight"
    directory.mkdir()
    probe = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "backfill_pool"]
    )
    try:
        (directory / f"backfill_pool.{probe.pid}.json").write_text(
            json.dumps({"module": "backfill_pool", "pid": probe.pid, "resumable": True})
        )
        with caplog.at_level(logging.WARNING):
            interrupted = report_interrupted("backfill_pool", directory=str(directory))
    finally:
        probe.kill()
        probe.wait(timeout=10)

    assert interrupted == []
    assert len(_markers(directory)) == 1
    if Path(f"/proc/{probe.pid}/cmdline").exists():
        assert "appears to be active" in caplog.text
    else:
        assert "cannot tell whether" in caplog.text


def test_a_reused_pid_is_not_mistaken_for_a_live_run(tmp_path, caplog):
    """The next container starts PIDs at 1, so the number alone proves nothing."""
    directory = tmp_path / "inflight"
    directory.mkdir()
    # This very process is alive, but it is pytest, not the backfill.
    (directory / f"backfill_pool.{os.getpid()}.json").write_text(
        json.dumps({"module": "backfill_pool", "pid": os.getpid(), "resumable": True})
    )

    with caplog.at_level(logging.WARNING):
        interrupted = report_interrupted("backfill_pool", directory=str(directory))

    if not Path(f"/proc/{os.getpid()}/cmdline").exists():
        pytest.skip("no /proc here: liveness is all this platform can answer")
    assert len(interrupted) == 1, "a reused PID was reported as the job still running"
    assert "did not finish" in caplog.text


def test_other_jobs_markers_are_left_alone(tmp_path: Path):
    directory = tmp_path / "inflight"
    directory.mkdir()
    (directory / "backfill_sea_view.0.json").write_text(
        json.dumps({"module": "backfill_sea_view", "pid": 0, "resumable": False})
    )

    report_interrupted("backfill_pool", directory=str(directory))

    assert _markers(directory) == ["backfill_sea_view.0.json"]


def test_a_corrupt_marker_cannot_stop_anything(tmp_path: Path, caplog):
    """A half-written or hand-edited file must not be able to block a deploy."""
    directory = tmp_path / "inflight"
    directory.mkdir()
    (directory / "backfill_pool.0.json").write_text("{not json")
    (directory / "other.0.json").write_text('"a string, not an object"')

    with caplog.at_level(logging.WARNING):
        assert read_markers(str(directory)) == []
        assert report_interrupted("backfill_pool", directory=str(directory)) == []


def test_resumable_defaults_to_false(tmp_path: Path):
    """Unknown and "not resumable" have to be the same answer."""
    directory = tmp_path / "inflight"
    path = write_marker("recalc_property_travel", argv=[], directory=str(directory))

    assert json.loads(path.read_text())["resumable"] is False


def test_describe_renders_the_command_a_human_would_type(tmp_path: Path):
    assert (
        describe({"module": "backfill_pool", "argv": ["--snapshot", "data/p.json"]})
        == "python -m utils.backfill_pool --snapshot data/p.json"
    )
    # A marker missing its argv still names the module rather than crashing.
    assert describe({"module": "backfill_pool"}) == "python -m utils.backfill_pool"


def test_a_marker_holding_true_as_its_pid_still_reports(tmp_path, caplog):
    """`isinstance(True, int)` is True, and PID 1 is gunicorn in the container."""
    directory = tmp_path / "inflight"
    directory.mkdir()
    (directory / "backfill_pool.1.json").write_text(
        json.dumps({"module": "backfill_pool", "pid": True, "resumable": True})
    )

    with caplog.at_level(logging.WARNING):
        interrupted = report_interrupted("backfill_pool", directory=str(directory))

    assert len(interrupted) == 1, "a bogus PID suppressed the interrupted-run report"


# --- #359: a containerised run is always PID 1 --------------------------


def test_a_marker_recording_my_own_pid_is_never_evidence_of_a_concurrent_run(
    tmp_path, caplog
):
    """THE regression test for #359.

    A long job runs in its own container, which makes it PID 1 - and every
    restart of the same module is PID 1 again. Before this fix, a stale
    marker recording pid 1 (or, more generally, this very process's own pid)
    read as "still active": the reader is alive at that number and its own
    cmdline names the module it is running, by definition. On a platform
    where `/proc` cannot be read at all the old code degraded straight to
    "alive" without even checking cmdline - so on every platform, a
    containerised job could never report its predecessor interrupted, because
    the successor always finds itself. `pid == os.getpid()` now settles it
    before cmdline is even consulted.

    The marker's `run_id` deliberately differs from anything this call would
    generate for itself, and `report_interrupted` is called with no `run_id`
    of its own (the default) - this test is about the pid rule, not the
    run-id skip.
    """
    directory = tmp_path / "inflight"
    directory.mkdir()
    (directory / "backfill_pool.deadbeefdeadbeefdeadbeefdeadbeef.json").write_text(
        json.dumps(
            {
                "module": "backfill_pool",
                "pid": os.getpid(),
                "run_id": "deadbeefdeadbeefdeadbeefdeadbeef",
                "resumable": True,
                "started_at": "2026-08-16T10:15:44.339133+00:00",
            }
        )
    )

    with caplog.at_level(logging.WARNING):
        interrupted = report_interrupted("backfill_pool", directory=str(directory))

    assert len(interrupted) == 1, (
        "a marker recording my own pid read as a live predecessor"
    )
    assert "did not finish" in caplog.text
    assert _markers(directory) == [], "an interrupted run's marker must be cleared"


def test_a_restart_does_not_clobber_its_predecessors_marker(tmp_path):
    """Two `write_marker` calls for the same module - as a restart inside one
    container, pinned at the same pid, would produce - must land in two
    files, not one (#359). Identity is the run id, generated fresh each call,
    never the pid.
    """
    directory = tmp_path / "inflight"

    first = write_marker(
        "recalc_sea_distance", argv=["--only-missing"], directory=str(directory)
    )
    second = write_marker(
        "recalc_sea_distance", argv=["--only-missing"], directory=str(directory)
    )

    assert first != second
    assert first.exists() and second.exists()
    assert len(_markers(directory)) == 2

    first_run_id = json.loads(first.read_text())["run_id"]
    second_run_id = json.loads(second.read_text())["run_id"]
    assert first_run_id != second_run_id
    assert first_run_id in first.name
    assert second_run_id in second.name


def test_an_undecidable_marker_is_left_in_place_and_named_distinctly(
    tmp_path, caplog, monkeypatch
):
    """ "Cannot tell" must never collapse into "alive" or "dead" (#359).

    A live pid that is not me, with `/proc` unreadable, is the exact shape
    macOS and a hardened runtime present for a genuinely foreign process:
    liveness is provable, identity is not. The caller must leave the marker
    untouched, must not count it as interrupted, and must say something a
    reader (and a test) can tell apart from "still active".
    """
    import utils.inflight as inflight_module

    monkeypatch.setattr(inflight_module, "_cmdline", lambda pid: None)
    foreign_pid = os.getppid()  # alive for the life of this test, and not me

    directory = tmp_path / "inflight"
    directory.mkdir()
    (directory / f"backfill_pool.{foreign_pid}.json").write_text(
        json.dumps({"module": "backfill_pool", "pid": foreign_pid, "resumable": True})
    )

    with caplog.at_level(logging.WARNING):
        interrupted = report_interrupted("backfill_pool", directory=str(directory))

    assert interrupted == [], "an undecidable marker must not be reported interrupted"
    assert len(_markers(directory)) == 1, "an undecidable marker must be left in place"
    assert "cannot tell whether" in caplog.text
    assert "appears to be active" not in caplog.text


def test_report_interrupted_skips_the_marker_matching_its_own_run_id(tmp_path, caplog):
    """`inflight()` threads one run id through both calls so a run never reads
    its own marker as a predecessor's. Exercised directly here at the
    `report_interrupted(run_id=...)` seam.
    """
    directory = tmp_path / "inflight"
    directory.mkdir()
    (directory / "backfill_pool.mine.json").write_text(
        json.dumps(
            {
                "module": "backfill_pool",
                "pid": 0,  # dead by any measure, which is exactly the point:
                "run_id": "mine",  # the run-id match must skip it before that
                "resumable": True,
            }
        )
    )

    with caplog.at_level(logging.WARNING):
        interrupted = report_interrupted(
            "backfill_pool", directory=str(directory), run_id="mine"
        )

    assert interrupted == []
    assert _markers(directory) == ["backfill_pool.mine.json"], (
        "my own marker must survive"
    )


def test_inflight_generates_a_run_id_and_never_collides_with_itself(tmp_path):
    """The context manager's own marker must never be reported as its own
    interrupted predecessor - the run-id skip is what `inflight()` relies on.
    """
    directory = tmp_path / "inflight"

    with inflight("backfill_pool", directory=str(directory)) as path:
        payload = json.loads(path.read_text())
        assert payload["run_id"], "a run id must be generated, not left empty"
        assert payload["run_id"] in path.name

    assert _markers(directory) == []
