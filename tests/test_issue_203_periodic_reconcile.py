"""Periodic reconciliation of abandoned background jobs (issue #203).

Follow-up to #176 (PR #190, merged as 2c84db1). `reconcile_orphaned_jobs()`
(services/background_jobs.py) used to run only once per `create_app()`, plus
a point sweep of one `dedupe_key` when `enqueue_job` meets it again. Nothing
swept periodically, so a job abandoned by a killed process (the deploy
watcher recreating the container) had `/api/jobs/<id>` answer
`200 {"status": "running"}` for up to `LEASE_TTL_SECONDS`, and could stay
that way indefinitely afterward -- not until the next restart or a
re-enqueue of the exact same `dedupe_key`, either of which might never come.

`services/scheduler_service.py` now registers `run_background_jobs_
reconciliation` on the app's own APScheduler (`init_scheduler`), on an
`IntervalTrigger` of `RECONCILE_INTERVAL_S` (services/background_jobs.py)
seconds -- no second scheduler, no extra thread of its own.

These tests simulate a scheduler tick by calling that job function directly,
the same way test_issue_176_persist_jobs.py's heartbeat tests simulate a
heartbeat tick via `_renew_owned_leases()` instead of waiting
`HEARTBEAT_INTERVAL_S` seconds for real -- waiting out `RECONCILE_INTERVAL_S`
seconds of wall-clock time here would make the suite slow without proving
anything a direct call does not already prove. They pin both acceptance
criteria from the issue:

1. An abandoned job (expired lease, no heartbeat owner) reaches
   `interrupted` through the periodic sweep alone, without another
   `create_app()` or a re-enqueue of the same `dedupe_key`.
2. A live, heartbeat-renewing job is never marked `interrupted` by that
   sweep -- the main risk of this change: getting it wrong would undo
   #176's own fix.

Plus the wiring itself (the sweep really is periodic, registered on the
app's existing scheduler, not a function nobody calls) and the #14 lesson
applied to this job like every other one here: it must reach the database
from a bare APScheduler worker thread with no ambient context, and a
failure inside it must propagate rather than being swallowed.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app, db
from models import BackgroundJob
from services.background_jobs import (
    INTERRUPTED_MESSAGE,
    LEASE_TTL_SECONDS,
    RECONCILE_INTERVAL_S,
    _register_owned_job,
    _renew_owned_leases,
    _unregister_owned_job,
    get_job,
)
from tests import setup_test_environment

THREAD_TIMEOUT_SECONDS = 10


@pytest.fixture
def app():
    setup_test_environment()
    flask_app = create_app(testing=True)
    with flask_app.app_context():
        db.create_all()
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def scheduler_app(app, monkeypatch):
    """Bind the app the way `init_scheduler()` does, without starting a real
    scheduler: `run_background_jobs_reconciliation()` reads the module-level
    `flask_app` inside `job_app_context()`."""
    import services.scheduler_service as scheduler_service

    monkeypatch.setattr(scheduler_service, "flask_app", app)
    return app


def _insert_row(app, **values):
    with app.app_context():
        db.session.add(BackgroundJob(**values))
        db.session.commit()


def _future_lease(minutes: int = 30) -> datetime:
    """A lease that has not expired -- a job a live worker is still holding."""
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _expired_lease(minutes: int = 5) -> datetime:
    """A lease that expired a while ago -- a job nothing is renewing anymore."""
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _run_off_the_scheduler_thread(job):
    """Run exactly as APScheduler does: a bare worker thread, no ambient
    context. #14 -- a job that only works under an ambient *test* context can
    still be silently broken on the thread APScheduler actually uses."""
    raised = []

    def target():
        try:
            job()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            raised.append(exc)

    thread = threading.Thread(target=target, name="test-reconcile-scheduler-tick")
    thread.start()
    thread.join(timeout=THREAD_TIMEOUT_SECONDS)
    assert not thread.is_alive(), (
        f"reconciliation job thread still running after {THREAD_TIMEOUT_SECONDS}s"
    )
    if raised:
        raise raised[0]


# --- Acceptance criterion 1: an abandoned job self-heals -------------------


def test_an_abandoned_job_reaches_interrupted_via_the_periodic_sweep_alone(
    scheduler_app,
):
    """The issue's scenario: the process that owned this row is gone (no
    heartbeat renewal was ever registered for it, exactly like a killed
    container), and its lease has expired. A single scheduler tick -- not a
    new create_app(), not a re-enqueue of any dedupe_key -- must be enough
    to reach `interrupted`."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "a" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key="prop:355:claude",
        lease_expires_at=_expired_lease(minutes=1),
    )

    _run_off_the_scheduler_thread(run_background_jobs_reconciliation)

    with scheduler_app.app_context():
        job = get_job(job_id)
    assert job["status"] == "interrupted"
    assert job["error"] == INTERRUPTED_MESSAGE
    assert job["finished_at"] is not None


def test_a_queued_abandoned_job_also_self_heals(scheduler_app):
    """`queued`, not just `running` -- the same predicate
    `reconcile_orphaned_jobs` itself uses (ACTIVE_STATUSES), exercised
    through the scheduled job rather than called directly."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "b" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="land_check_status",
        status="queued",
        lease_expires_at=_expired_lease(minutes=1),
    )

    run_background_jobs_reconciliation()

    with scheduler_app.app_context():
        assert get_job(job_id)["status"] == "interrupted"


def test_repeated_ticks_do_not_re_touch_an_already_interrupted_row(scheduler_app):
    """A second tick (the next interval, or a second gunicorn worker also
    scheduling it) is a no-op -- the same idempotency `reconcile_orphaned_
    jobs` already guarantees, pinned again at the level the scheduler
    actually calls."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "c" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        lease_expires_at=_expired_lease(minutes=1),
    )

    run_background_jobs_reconciliation()
    with scheduler_app.app_context():
        first = get_job(job_id)

    run_background_jobs_reconciliation()
    with scheduler_app.app_context():
        second = get_job(job_id)

    assert first["status"] == second["status"] == "interrupted"
    assert first["finished_at"] == second["finished_at"], (
        "a second tick must not rewrite a row already reconciled"
    )


# --- Acceptance criterion 2: a live, heartbeating job is never touched -----


def test_a_live_heartbeating_job_survives_the_periodic_sweep(scheduler_app):
    """The central risk of this change: getting this wrong reintroduces
    exactly what #176 fixed -- a genuinely running job flipped to
    `interrupted` out from under its own worker. `lease_expires_at` is
    pushed into the future the same way the real heartbeat daemon thread
    does (`_renew_owned_leases`, after `_register_owned_job` at claim time),
    not by hand-setting a future timestamp -- so this exercises the same
    ownership bookkeeping production relies on, not just the SQL predicate
    in isolation."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "d" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        # Already past what a naive TTL check would allow -- only the
        # heartbeat-renewed lease is what keeps this row alive below.
        lease_expires_at=_expired_lease(minutes=1),
    )

    _register_owned_job(job_id)
    try:
        with scheduler_app.app_context():
            renewed = _renew_owned_leases(scheduler_app)
        assert renewed == 1, (
            "test setup: the heartbeat tick must have renewed the lease"
        )

        _run_off_the_scheduler_thread(run_background_jobs_reconciliation)

        with scheduler_app.app_context():
            job = get_job(job_id)
        assert job["status"] == "running", (
            "a live, heartbeat-renewed job must never be marked interrupted "
            "by the periodic sweep"
        )
        assert job["error"] is None
        assert job["finished_at"] is None
    finally:
        _unregister_owned_job(job_id)


def test_a_live_job_survives_several_sweep_ticks_interleaved_with_heartbeats(
    scheduler_app,
):
    """A long-running job outlives more than one sweep interval in
    production. Three rounds of (heartbeat tick, sweep tick) must leave the
    row exactly as untouched on the third round as on the first."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "e" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        lease_expires_at=_expired_lease(minutes=1),
    )

    _register_owned_job(job_id)
    try:
        for _ in range(3):
            with scheduler_app.app_context():
                _renew_owned_leases(scheduler_app)
            run_background_jobs_reconciliation()

            with scheduler_app.app_context():
                job = get_job(job_id)
            assert job["status"] == "running"
    finally:
        _unregister_owned_job(job_id)


def test_a_job_with_a_still_valid_lease_survives_without_any_heartbeat_call(
    scheduler_app,
):
    """A job just claimed, whose initial lease has simply not expired yet,
    must survive a sweep even with no heartbeat tick at all -- the sweep's
    predicate is the lease, not "did the heartbeat run recently"."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "v" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        lease_expires_at=_future_lease(),
    )

    run_background_jobs_reconciliation()

    with scheduler_app.app_context():
        assert get_job(job_id)["status"] == "running"


def test_a_terminal_job_is_left_alone_by_the_periodic_sweep(scheduler_app):
    job_id = "f" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="success",
        result={"success": True},
    )

    from services.scheduler_service import run_background_jobs_reconciliation

    run_background_jobs_reconciliation()

    with scheduler_app.app_context():
        job = get_job(job_id)
    assert job["status"] == "success"
    assert job["result"] == {"success": True}


# --- Wiring: the sweep really is periodic, not just a callable -------------


def test_init_scheduler_registers_the_reconciliation_job_on_an_interval(
    app, monkeypatch, tmp_path
):
    """The acceptance criterion is "without needing another application
    start" -- true only if `init_scheduler()` actually puts this job on a
    *recurring* trigger, not a one-shot call or a fixed-clock-time cron
    job like the other two jobs it registers."""
    import services.scheduler_service as scheduler_service
    from apscheduler.triggers.interval import IntervalTrigger

    monkeypatch.setattr(scheduler_service, "scheduler", None)
    monkeypatch.setattr(scheduler_service, "scheduler_lock_file", None)
    monkeypatch.setattr(scheduler_service, "flask_app", None)
    # Isolate the scheduler's advisory lock file from the real host temp
    # dir -- other suites/sessions may hold that real, fixed-name lock file
    # concurrently (docs/DEV_RULES.md's parallel-session note).
    monkeypatch.setattr(scheduler_service.tempfile, "gettempdir", lambda: str(tmp_path))
    app.config["TESTING"] = False
    app.config["AUTO_START_SCHEDULER"] = True

    started = scheduler_service.init_scheduler(app)
    try:
        assert started is not None, "scheduler failed to start"
        job = started.get_job("background_jobs_reconciliation")
        assert job is not None, "the periodic reconcile job was not registered"
        assert isinstance(job.trigger, IntervalTrigger), (
            "must be a recurring interval trigger, not a fixed-time cron job"
        )
        assert job.trigger.interval == timedelta(seconds=RECONCILE_INTERVAL_S)
    finally:
        started.shutdown(wait=False)
        scheduler_service.scheduler = None
        if scheduler_service.scheduler_lock_file:
            scheduler_service.scheduler_lock_file.close()
            scheduler_service.scheduler_lock_file = None


def test_reconcile_interval_is_noticeably_smaller_than_the_lease_ttl():
    """Sanity pin from the issue: the interval must be well under
    `LEASE_TTL_SECONDS` (900 s), or the periodic sweep would barely improve
    on "wait for the lease to expire and hope something else calls
    reconcile_orphaned_jobs()"."""
    assert RECONCILE_INTERVAL_S < LEASE_TTL_SECONDS, (
        "the reconcile interval must be smaller than the lease TTL it sweeps"
    )
    assert RECONCILE_INTERVAL_S <= LEASE_TTL_SECONDS / 2, (
        "the interval should be noticeably smaller, not just technically less"
    )


# --- #14: this job must reach the database from a bare worker thread too,
# and a failure inside it must not be swallowed -----------------------------


def test_reconciliation_reaches_the_database_off_the_scheduler_thread(scheduler_app):
    """Same lesson as #14: a job that only works under an ambient test
    context is broken in production, where APScheduler runs it on its own
    thread with none."""
    from services.scheduler_service import run_background_jobs_reconciliation

    job_id = "g" * 32
    _insert_row(
        scheduler_app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        lease_expires_at=_expired_lease(minutes=1),
    )

    _run_off_the_scheduler_thread(run_background_jobs_reconciliation)

    with scheduler_app.app_context():
        assert get_job(job_id)["status"] == "interrupted"


def test_reconciliation_job_failure_propagates_to_scheduler(scheduler_app, monkeypatch):
    """#14 again: a failure inside the sweep must not be swallowed and
    reported to APScheduler as a successful run."""
    import services.background_jobs as background_jobs
    from services.scheduler_service import run_background_jobs_reconciliation

    class ReconcileFailure(RuntimeError):
        pass

    def _boom():
        raise ReconcileFailure("reconcile exploded")

    monkeypatch.setattr(background_jobs, "reconcile_orphaned_jobs", _boom)

    with pytest.raises(ReconcileFailure):
        _run_off_the_scheduler_thread(run_background_jobs_reconciliation)
