"""Background jobs survive the process that queued them (issue #176).

`services/background_jobs.py` used to keep every job in a dict private to one
process. `tools/autopilot/deploy_watcher.sh` recreates the app container on
every new `main` -- as often as every 300 s -- so a job in flight at that
moment was silently thrown away: `/api/jobs/<id>` answered 404, and an AI
analysis that had already spent its provider quota produced no stored result.

`migrations/016_create_background_jobs_table.sql` is PostgreSQL-only and is
covered separately in `tests/test_postgres_migrations.py` (schema shape, the
CHECK constraint, and the partial unique index under a real concurrent
insert -- two threads, two connections, an actual race). These tests run
against the same in-memory SQLite database every other module in this suite
uses -- `db.create_all()` builds the table from the `BackgroundJob` model --
and pin the *behaviour*: reconciliation at startup, the honest
`/api/jobs/<id>` response, and enqueue-time idempotency.

Tests that need to observe a job's worker actually run patch
`services.background_jobs._EXECUTOR` to execute inline instead of on a real
thread. That runs the exact same `enqueue_job`/`_run_job`/`_write_status` code
-- nothing about the code under test is replaced -- it only avoids a genuine
artifact of the *test* database: Flask-SQLAlchemy gives an in-memory SQLite
engine a single shared `StaticPool` connection (app.py's own comment on
`_is_in_memory_sqlite` says as much), and two Python threads issuing
statements over that one pysqlite connection at overlapping instants corrupts
SQLAlchemy's C row-buffer (`IndexError: tuple index out of range` in
`sqlalchemy.cyextension.resultproxy`), independent of anything this module
does. Real PostgreSQL gives every thread its own connection.

## The lease model (round 2 of #190's review)

The "Blocker 1" tests below were added after a first independent review
found that a terminal status write whose commit failed left a row stuck
`running` forever. The fix at the time compared a stale row's `started_at`
against *this process's* clock -- and a second review round rejected that:
a process clock that has drifted (or a one-shot utility script's own
`create_app()` interrupting a live web-process job unconditionally) could
misjudge a genuinely live job as dead. The "Lease model" section further
down pins the replacement: every staleness decision happens in SQL, against
the *database's* `now()`, via `lease_expires_at` -- never against anything
Python computed. `_expired_lease()`/`_future_lease()` below build plain
Python datetimes for test fixtures to insert directly (ordinary bound
parameters); production code only ever writes this column via a SQL
expression (`_lease_expiry_expr`), which is exactly what the clock-skew test
near the bottom of this file is pinning.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app, db
from models import BackgroundJob
from services import background_jobs
from services.background_jobs import (
    INTERRUPTED_MESSAGE,
    LEASE_TTL_SECONDS,
    enqueue_job,
    get_job,
    reconcile_orphaned_jobs,
)
from tests import setup_test_environment


class _ImmediateExecutor:
    """Runs a submitted callable synchronously, in the caller's thread.

    Stands in for the real `ThreadPoolExecutor` only for the SQLite artifact
    described in the module docstring -- `enqueue_job` still calls
    `_EXECUTOR.submit(_run_job, ...)` exactly as it does in production, this
    just resolves it inline instead of scheduling it onto a second thread.
    """

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def app():
    setup_test_environment()
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def inline_executor(monkeypatch):
    """Make enqueue_job's worker run synchronously for this test only."""
    monkeypatch.setattr(background_jobs, "_EXECUTOR", _ImmediateExecutor())


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


# --- Reconciliation at startup -------------------------------------------


def test_reload_reads_state_a_different_process_wrote(app):
    """The scenario in the issue: process A dies mid-job, process B reads it.

    Nothing here shares Python state between "process A" and "process B" --
    each block opens its own app context and reads the row back from the
    database, the way a freshly started process actually would once the
    in-memory registry it used to keep is gone. No lease is set (as if the
    row predates this column, or its writer never got as far as renewing
    it), which is the honest worst case: absent a lease, the row cannot be
    proven live, so it is reaped.
    """
    job_id = "a" * 32
    _insert_row(
        app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        meta={"property_id": 355, "provider": "claude"},
    )

    # "Process B" starts: reconcile before anything else touches the table.
    with app.app_context():
        assert reconcile_orphaned_jobs() == 1

        job = get_job(job_id)
        assert job is not None, "the row a redeploy interrupted must still resolve"
        assert job["status"] == "interrupted"
        assert job["error"] == INTERRUPTED_MESSAGE
        assert job["finished_at"] is not None
        # Still the same run, just honestly labeled -- not lost.
        assert job["meta"] == {"property_id": 355, "provider": "claude"}


def test_a_queued_job_also_reconciles_to_interrupted(app):
    """`queued`, not just `running`, means "no process is working on this"."""
    job_id = "b" * 32
    _insert_row(app, id=job_id, job_type="land_check_status", status="queued")

    with app.app_context():
        assert reconcile_orphaned_jobs() == 1
        assert get_job(job_id)["status"] == "interrupted"


def test_terminal_jobs_are_left_alone_by_reconciliation(app):
    job_id = "c" * 32
    _insert_row(
        app,
        id=job_id,
        job_type="property_ai_analysis",
        status="success",
        result={"success": True},
    )

    with app.app_context():
        assert reconcile_orphaned_jobs() == 0
        job = get_job(job_id)
        assert job["status"] == "success"
        assert job["result"] == {"success": True}


def test_a_live_leased_job_is_left_alone_by_reconciliation(app):
    """The core of the lease model: an active row with a lease still in the
    future must never be touched, regardless of how old its started_at is."""
    job_id = "L" * 32
    _insert_row(
        app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=5),
        lease_expires_at=_future_lease(),
    )

    with app.app_context():
        assert reconcile_orphaned_jobs() == 0
        assert get_job(job_id)["status"] == "running"


def test_reconciliation_is_idempotent(app):
    """A second call -- a repeated boot, or a second gunicorn worker -- is a no-op."""
    job_id = "d" * 32
    _insert_row(app, id=job_id, job_type="land_check_status", status="running")

    with app.app_context():
        assert reconcile_orphaned_jobs() == 1
        assert reconcile_orphaned_jobs() == 0


def test_reconcile_is_a_noop_before_the_table_exists():
    """create_app() calls this unconditionally (app.py); most fixtures in this
    suite build the schema with db.create_all() only after create_app()
    returns, so the table does not exist yet at that point."""
    setup_test_environment()
    flask_app = create_app()
    with flask_app.app_context():
        assert reconcile_orphaned_jobs() == 0


def test_a_second_create_app_does_not_touch_a_live_leased_job(tmp_path, monkeypatch):
    """#190 review round 2, finding 3: a one-shot utility script
    (utils/backfill_sea_view.py and friends) builds its own `create_app()`
    while the web process is still alive and holding a valid lease on its
    own job. That used to interrupt it unconditionally; now it must not,
    because the row it would touch has a lease still in the future.

    Uses a file-backed SQLite database (not the usual `:memory:`) so two
    independent `create_app()` calls -- two separate engines -- genuinely
    share one database, the way two OS processes sharing one PostgreSQL
    server do in production.
    """
    setup_test_environment()
    db_path = tmp_path / "shared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    web_app = create_app()
    with web_app.app_context():
        db.create_all()
        live_id = "w" * 32
        expired_id = "x" * 32
        db.session.add_all(
            [
                BackgroundJob(
                    id=live_id,
                    job_type="property_ai_analysis",
                    status="running",
                    lease_expires_at=_future_lease(),
                ),
                BackgroundJob(
                    id=expired_id,
                    job_type="land_check_status",
                    status="running",
                    lease_expires_at=_expired_lease(),
                ),
            ]
        )
        db.session.commit()

    # A second, independent app instance -- as a utility script builds for
    # itself -- pointed at the same database file. create_app() itself
    # calls reconcile_orphaned_jobs() (app.py), so this alone is the sweep
    # under test; no explicit extra call is needed to trigger it.
    utility_app = create_app()
    with utility_app.app_context():
        assert get_job(live_id)["status"] == "running", (
            "a second create_app() must never touch a job whose lease is still valid"
        )
        assert get_job(expired_id)["status"] == "interrupted", (
            "a second create_app() must still reap a row whose lease has expired"
        )

        # A repeated, explicit call is idempotent -- nothing new to reap.
        assert reconcile_orphaned_jobs() == 0

    with web_app.app_context():
        db.drop_all()


# --- /api/jobs/<id>: 200 with an honest status, never 404 for a real job ---


def test_api_answers_200_for_an_interrupted_job_not_404(app, client):
    job_id = "e" * 32
    _insert_row(app, id=job_id, job_type="property_ai_analysis", status="running")
    with app.app_context():
        reconcile_orphaned_jobs()

    resp = client.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["job"]["status"] == "interrupted"
    # static/js/main.js pollJob surfaces job.error verbatim -- this is what
    # replaces the bare "Job not found" the issue complains about.
    assert "run it again" in body["job"]["error"].lower()


def test_api_still_answers_404_for_an_id_that_was_never_enqueued(client):
    resp = client.get("/api/jobs/" + "f" * 32)
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


# --- Idempotent enqueue: acceptance criterion 4 ---------------------------


def test_enqueue_reuses_the_in_flight_job_instead_of_racing_it(app):
    """One job is already active for (property, provider), with a lease
    still comfortably in the future; a resubmit must not start a second one
    racing it for the same PropertyAiAnalysisVariant row."""
    key = "property_ai_analysis:355:claude"
    existing_id = "1" * 32
    _insert_row(
        app,
        id=existing_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key=key,
        meta={"property_id": 355, "provider": "claude"},
        lease_expires_at=_future_lease(),
    )

    calls = []

    def _fn():
        calls.append(1)
        return {"success": True}

    with app.app_context():
        returned_id = enqueue_job(
            _fn,
            job_type="property_ai_analysis",
            meta={"property_id": 355, "provider": "claude"},
            app=app,
            dedupe_key=key,
        )

    assert returned_id == existing_id, "must reuse the run already in flight"

    time.sleep(0.2)  # a wrongly-submitted duplicate worker would run by now
    assert calls == [], "a duplicate run must never have been submitted"

    with app.app_context():
        active = BackgroundJob.query.filter(
            BackgroundJob.dedupe_key == key,
            BackgroundJob.status.in_(("queued", "running")),
        ).count()
    assert active == 1


def test_enqueue_allows_a_fresh_run_once_the_earlier_one_is_terminal(
    app, inline_executor
):
    """Retrying an interrupted analysis must still be possible -- the partial
    index only covers active rows, so a terminal one never blocks a retry."""
    key = "property_ai_analysis:355:claude"
    old_id = "2" * 32
    _insert_row(
        app,
        id=old_id,
        job_type="property_ai_analysis",
        status="interrupted",
        dedupe_key=key,
        error=INTERRUPTED_MESSAGE,
    )

    def _fn():
        return {"success": True, "retried": True}

    with app.app_context():
        new_id = enqueue_job(
            _fn,
            job_type="property_ai_analysis",
            meta={"property_id": 355, "provider": "claude"},
            app=app,
            dedupe_key=key,
        )

    assert new_id != old_id

    with app.app_context():
        job = get_job(new_id)
        old_job = get_job(old_id)
        active = BackgroundJob.query.filter(
            BackgroundJob.dedupe_key == key,
            BackgroundJob.status.in_(("queued", "running")),
        ).count()

    assert job["status"] == "success"
    assert job["result"] == {"success": True, "retried": True}
    # The old row was never touched by the retry, and at no point were two
    # rows racing each other -- the new run is now the only terminal-or-active
    # one that matters, and the interrupted row stays exactly what it was.
    assert old_job["status"] == "interrupted"
    assert active == 0


def test_enqueue_without_a_dedupe_key_behaves_as_before(app, inline_executor):
    """Most job types (enrichment, status checks, ...) pass no dedupe_key and
    must be unaffected -- two calls queue two independent jobs, and both run
    to completion."""

    def _fn():
        return {"ok": True}

    with app.app_context():
        first = enqueue_job(_fn, job_type="lands_enrich_all", app=app)
        second = enqueue_job(_fn, job_type="lands_enrich_all", app=app)

    assert first != second
    with app.app_context():
        for job_id in (first, second):
            job = get_job(job_id)
            assert job["status"] == "success"
            assert job["result"] == {"ok": True}


def test_a_failing_job_is_recorded_as_error_not_left_queued(app, inline_executor):
    """A job function that raises must reach a terminal, honest status."""

    def _fn():
        raise ValueError("provider refused the request")

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert job["status"] == "error"
    assert job["error"] == "provider refused the request"
    assert job["started_at"] is not None
    assert job["finished_at"] is not None


# --- Blocker 1 (#190 review round 1): a broken terminal write must not
# strand a job -----------------------------------------------------------


def test_a_running_mark_that_fails_recovers_via_a_fresh_session(
    app, inline_executor, monkeypatch
):
    """Simulates the first commit(s) of the "mark running" transition
    failing.

    `_write_status` retries `_TRANSITION_MAX_ATTEMPTS` times against the
    normal session, then falls back once to a brand-new `Session`. This
    fails every attempt against the normal session and lets only the
    fresh-session fallback through, proving the row reaches `running` (and
    then `fn()` actually runs) rather than staying `queued` forever.

    Targets the "mark running" transition specifically, not the terminal
    "success" write: since #190 review round 4, finding 4, the success
    write goes through `_finalize_success`, which deliberately does not
    retry or fall back to a fresh session (a job function's own staged
    domain writes could not survive either recovery path) -- see
    `test_a_success_write_that_fails_does_not_retry_and_records_an_honest_error`
    below for that write's own, different, contract.
    """
    real_try_write = background_jobs._try_write
    calls = {"running_attempts": 0}

    def _flaky_try_write(session, job_id, expected_status, fields):
        if fields.get("status") != "running":
            # Let the terminal write through untouched.
            return real_try_write(session, job_id, expected_status, fields)
        calls["running_attempts"] += 1
        if calls["running_attempts"] <= background_jobs._TRANSITION_MAX_ATTEMPTS:
            return False
        return real_try_write(session, job_id, expected_status, fields)

    monkeypatch.setattr(background_jobs, "_try_write", _flaky_try_write)

    def _fn():
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    # _TRANSITION_MAX_ATTEMPTS failures against the normal session, then one
    # more call that is the fresh-session fallback succeeding.
    assert calls["running_attempts"] == background_jobs._TRANSITION_MAX_ATTEMPTS + 1
    assert job["status"] == "success", (
        "the row must not stay 'queued' forever when the normal session's "
        "commit is broken -- the fresh-session fallback must recover it, "
        "and fn() must then actually run"
    )
    assert job["result"] == {"success": True}


def test_a_success_write_that_fails_does_not_retry_and_records_an_honest_error(
    app, inline_executor, monkeypatch
):
    """#190 review round 4, finding 4: the combined success write
    (`_finalize_success`) does not retry or fall back to a fresh session
    the way a status-only write does -- `fn()`'s own staged domain writes
    would not survive either recovery path (a fresh session never sees
    them at all; a retry's own rollback discards them, and nothing tracks
    them to redo). A transient failure here is recorded as an honest
    `error` instead, through the ordinary status-only fallback write
    (which *does* retry, since there is nothing but a status column at
    stake by then) -- never silently claimed as `success` over domain data
    that was never actually written.
    """
    real_try_write = background_jobs._try_write

    def _fail_only_the_success_write(session, job_id, expected_status, fields):
        if fields.get("status") == "success":
            return False
        return real_try_write(session, job_id, expected_status, fields)

    monkeypatch.setattr(background_jobs, "_try_write", _fail_only_the_success_write)

    def _fn():
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert job["status"] == "error", (
        "a failed combined success write must not be silently retried into "
        "a false success -- it must honestly record an error instead"
    )
    assert "run it again" in job["error"].lower()
    assert job["result"] is None, "no result must be recorded alongside the error"


def test_a_terminal_write_that_always_fails_leaves_the_row_running_but_reapable(
    app, inline_executor, monkeypatch
):
    """The genuine worst case: even the fresh-session fallback fails.

    `_write_status` can only return False here -- nothing can force a commit
    that truly never succeeds. What matters is that the row does not then
    block its dedupe_key forever: once its lease expires,
    `enqueue_job`'s reap check is what frees it, proven by the next test
    section. This one just pins that `_write_status` gives up honestly
    (returns False, logs, does not raise) rather than crashing the worker or
    claiming success.
    """
    monkeypatch.setattr(background_jobs, "_try_write", lambda *a, **kw: False)

    def _fn():
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    # "mark running" also went through the always-failing patch, so the row
    # never left 'queued' -- exactly the stuck state a real, total DB outage
    # would produce. No exception escaped enqueue_job or the worker.
    assert job["status"] == "queued"


# --- The lease model (#190 review round 2) --------------------------------


def test_an_expired_lease_is_reaped_instead_of_blocking_dedupe_forever(
    app, inline_executor
):
    """The backstop for blocker 1, now expressed through the lease: even if
    a row's writer could never record its own outcome, a later enqueue for
    the same dedupe_key must not hang on it forever. Its dedupe_key is freed
    once the lease itself -- not any process's read of elapsed wall-clock
    time -- has expired.

    Uses `inline_executor`: unlike the tests above it, the reap here
    actually succeeds and enqueue_job reaches `_EXECUTOR.submit` for a real
    replacement job, which must not run on a genuine second thread racing
    the assertions below over the shared SQLite connection (see the module
    docstring).
    """
    key = "property_ai_analysis:355:claude"
    stale_id = "9" * 32
    _insert_row(
        app,
        id=stale_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key=key,
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        lease_expires_at=_expired_lease(),
    )

    def _fn():
        return {"success": True}

    with app.app_context():
        new_id = enqueue_job(
            _fn, job_type="property_ai_analysis", app=app, dedupe_key=key
        )

    assert new_id != stale_id, "an expired-lease row must not be handed back as active"

    with app.app_context():
        stale_job = get_job(stale_id)
        new_job = get_job(new_id)
        # inline_executor runs the replacement synchronously, so by now it
        # has already finished -- "exactly the new job" is checked by row
        # count for this dedupe_key, not by status, which is terminal here.
        rows_for_key = BackgroundJob.query.filter(
            BackgroundJob.dedupe_key == key
        ).count()

    assert stale_job["status"] == "interrupted"
    assert "lease" in stale_job["error"].lower()
    assert new_job["status"] == "success"
    assert rows_for_key == 2, "exactly the stale row plus the new one, never a third"


def test_a_job_with_a_valid_lease_is_not_reaped_even_if_started_long_ago(app):
    """The lease, not started_at's age, is what decides liveness. A job
    that has been running for hours but is still renewing its lease (a
    long-running analysis, or simply a recent renewal) must be handed back,
    not raced."""
    key = "property_ai_analysis:355:claude"
    existing_id = "8" * 32
    _insert_row(
        app,
        id=existing_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key=key,
        started_at=datetime.now(timezone.utc) - timedelta(hours=3),
        lease_expires_at=_future_lease(),
    )

    def _fn():
        return {"success": True}

    with app.app_context():
        returned_id = enqueue_job(
            _fn, job_type="property_ai_analysis", app=app, dedupe_key=key
        )

    assert returned_id == existing_id
    with app.app_context():
        assert get_job(existing_id)["status"] == "running"


def test_process_clock_skew_does_not_affect_the_reap_decision(app, monkeypatch):
    """#190 review round 2, finding 2, proven directly: skew this process's
    own clock two hours into the future and confirm a live-leased job is
    still not reaped. If staleness were judged by comparing against
    anything Python computed, this would immediately misjudge the job as
    abandoned; since the decision is made in SQL against the database's own
    now(), a lying process clock changes nothing.
    """
    real_datetime = background_jobs.datetime

    class _SkewedDatetime(real_datetime):
        """`datetime.datetime`, but `.now()` lies two hours into the future."""

        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(hours=2)

    monkeypatch.setattr(background_jobs, "datetime", _SkewedDatetime)
    # Sanity: the module's own "now" helper really is skewed by this patch.
    assert (background_jobs._now() - real_datetime.now(timezone.utc)) > timedelta(
        hours=1
    )

    key = "property_ai_analysis:355:claude"
    existing_id = "7" * 32
    _insert_row(
        app,
        id=existing_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key=key,
        lease_expires_at=_future_lease(minutes=10),
    )

    def _fn():
        return {"success": True}

    with app.app_context():
        returned_id = enqueue_job(
            _fn, job_type="property_ai_analysis", app=app, dedupe_key=key
        )
        reconciled = reconcile_orphaned_jobs()

    assert returned_id == existing_id, (
        "a skewed process clock must not make enqueue_job reap a live-leased job"
    )
    assert reconciled == 0, (
        "a skewed process clock must not make reconcile_orphaned_jobs reap a "
        "live-leased job either"
    )
    with app.app_context():
        assert get_job(existing_id)["status"] == "running"


def test_the_dedupe_lock_blocks_a_concurrent_caller_for_the_same_key(
    app, inline_executor
):
    """#190 review round 4, finding 1: the check -> reap -> insert sequence
    in `_acquire_job_slot` must actually be serialized per `dedupe_key`,
    not just look race-free by construction. This is what closed the gap a
    round-3 test (`test_a_losing_reap_falls_back_to_the_winners_replacement_
    id`, before this round) used to pin instead: with a real lock in place,
    that test's exact scenario -- a caller retrying *past* a concurrent
    winner's reap-and-insert -- can no longer happen at all, because no
    second caller can even be inside the check/reap/insert section for the
    same key while the first is.

    Proven directly against `_dedupe_serialization`'s SQLite branch (a
    process-local `threading.Lock` per key -- see the module docstring's
    "Serializing the enqueue race" section): only one thread ever touches
    the database at a time here -- the second is fully blocked acquiring
    the lock itself, never issuing any SQL -- so this is safe against the
    shared-`StaticPool`-connection corruption the rest of this file's
    docstring warns real concurrent database access would risk.
    `inline_executor` keeps it that way even across `enqueue_job`'s own
    async dispatch: the worker thread's `fn()` runs to completion inline,
    inside that same one thread, rather than handing off to a second, real
    `_EXECUTOR` thread that would also touch the database. The equivalent
    proof against PostgreSQL's `pg_advisory_xact_lock`, with two real
    threads and two real connections racing `enqueue_job` itself, is
    `tests/test_postgres_migrations.py::
    test_016_the_dedupe_serialization_lock_allows_only_one_execution_per_key`.
    """
    import threading

    from services.background_jobs import _sqlite_dedupe_lock

    key = "property_ai_analysis:355:claude"
    lock = _sqlite_dedupe_lock(key)

    finished = threading.Event()

    def _worker():
        with app.app_context():
            enqueue_job(
                lambda: {"success": True},
                job_type="property_ai_analysis",
                app=app,
                dedupe_key=key,
            )
        finished.set()

    lock.acquire()
    try:
        thread = threading.Thread(target=_worker)
        thread.start()
        finished_while_locked = finished.wait(timeout=0.3)
        assert not finished_while_locked, (
            "a concurrent enqueue_job for the same dedupe_key must block on "
            "the lock, not run straight through it"
        )
    finally:
        lock.release()

    thread.join(timeout=5)
    assert not thread.is_alive(), "the worker must finish once the lock is released"
    assert finished.is_set()

    with app.app_context():
        active_rows = BackgroundJob.query.filter(
            BackgroundJob.dedupe_key == key,
            BackgroundJob.status.in_(("queued", "running", "success")),
        ).all()
    assert len(active_rows) == 1, "exactly one job must exist for the key"


def test_after_both_terminal_writes_fail_a_lease_expiry_reaps_and_redispatches(
    app, inline_executor
):
    """Finding 1's remaining, honest bound: if every attempt to record a
    job's outcome fails (the database was unreachable, say), the row cannot
    be proven dead before its lease expires -- that is the model's stated
    minimum, not a bug. Once "the database recovers" (writes start
    succeeding again) and the lease's TTL has actually elapsed, a resubmit
    must reap the abandoned row and dispatch a real replacement rather than
    handing back the dead job's id or refusing to run at all.

    Uses its own scoped `pytest.MonkeyPatch.context()` rather than the
    shared `monkeypatch` fixture, so reverting the `_try_write` patch here
    cannot also undo the `inline_executor` fixture's own patch of
    `_EXECUTOR` -- both are applied through the same fixture instance if
    requested together, and `monkeypatch.undo()` reverts everything it has
    done, not just one caller's change.
    """

    def _fn():
        return {"success": True}

    key = "property_ai_analysis:355:claude"
    with pytest.MonkeyPatch.context() as failing_writes:
        failing_writes.setattr(background_jobs, "_try_write", lambda *a, **kw: False)
        with app.app_context():
            stuck_id = enqueue_job(
                _fn, job_type="property_ai_analysis", app=app, dedupe_key=key
            )
            stuck_job = get_job(stuck_id)
    assert stuck_job["status"] == "queued", "every write attempt was patched to fail"

    # "The database recovers": writes succeed again from here on (the patch
    # above is already reverted). "The lease's TTL has elapsed": back-date
    # the row's lease directly (this test is not going to sleep for
    # LEASE_TTL_SECONDS) rather than fake it -- an expired lease is exactly
    # what elapsed real time would produce.
    with app.app_context():
        BackgroundJob.query.filter_by(id=stuck_id).update(
            {"lease_expires_at": _expired_lease()}, synchronize_session=False
        )
        db.session.commit()

    def _replacement_fn():
        return {"success": True, "replacement": True}

    with app.app_context():
        new_id = enqueue_job(
            _replacement_fn, job_type="property_ai_analysis", app=app, dedupe_key=key
        )
        new_job = get_job(new_id)
        stuck_job_after = get_job(stuck_id)

    assert new_id != stuck_id
    assert new_job["status"] == "success"
    assert new_job["result"] == {"success": True, "replacement": True}
    assert stuck_job_after["status"] == "interrupted"


def test_lease_ttl_constant_is_reasonable_for_the_longest_known_job_budget():
    """Pins the TTL against the client-visible AI-analysis budget
    (static/js/main.js JOB_POLL_TIMEOUTS.aiAnalysis = 660000 ms) so a change
    to one is a deliberate decision about the other, not a silent drift."""
    assert LEASE_TTL_SECONDS > 660, (
        "the lease must outlive the longest job budget the client itself waits for"
    )


# --- The heartbeat (#190 review round 3, finding 1) -----------------------


def test_heartbeat_renewal_extends_the_lease_of_an_owned_job(app):
    """`_renew_owned_leases` -- what the daemon thread calls every
    HEARTBEAT_INTERVAL_S -- extends the lease of every job id currently
    registered as owned by this process, proven directly rather than by
    waiting for the real thread's timer."""
    from services.background_jobs import (
        _register_owned_job,
        _renew_owned_leases,
        _unregister_owned_job,
    )

    job_id = "h" * 32
    _insert_row(
        app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        lease_expires_at=_expired_lease(minutes=1),  # about to be reaped
    )

    _register_owned_job(job_id)
    try:
        with app.app_context():
            renewed = _renew_owned_leases(app)
        assert renewed == 1

        with app.app_context():
            row = db.session.get(BackgroundJob, job_id)
            # SQLite round-trips a naive value regardless of what was
            # written; compare on equal footing rather than assume tzinfo.
            lease = row.lease_expires_at
            now = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                if lease.tzinfo is None
                else datetime.now(timezone.utc)
            )
            assert lease > now, "the heartbeat must push the lease back into the future"
    finally:
        _unregister_owned_job(job_id)


def test_renewal_is_a_noop_for_a_job_this_process_does_not_own(app):
    """A job id never registered (or already unregistered) is left alone --
    the heartbeat only ever touches ids this process currently owns."""
    from services.background_jobs import _renew_owned_leases

    job_id = "u" * 32
    _insert_row(
        app,
        id=job_id,
        job_type="property_ai_analysis",
        status="running",
        lease_expires_at=_expired_lease(minutes=1),
    )

    with app.app_context():
        renewed = _renew_owned_leases(app)
    assert renewed == 0, "a job id never registered as owned must not be touched"


def test_a_job_queued_past_its_initial_lease_survives_while_its_owner_heartbeats(
    tmp_path, monkeypatch
):
    """#190 review round 3, finding 1: `_EXECUTOR` has a fixed worker count
    and an unbounded queue, so a job can sit queued behind slow work long
    enough to outlive its own initial LEASE_TTL_SECONDS before a worker ever
    starts it. Simulated here by an expired lease on a still-`queued` row
    whose owning process nonetheless keeps renewing it (registered, then
    heartbeat-ticked) -- a second, independent `create_app()` sharing the
    same database must not reap it.
    """
    from services.background_jobs import (
        _register_owned_job,
        _renew_owned_leases,
        _unregister_owned_job,
    )

    setup_test_environment()
    db_path = tmp_path / "shared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    owner_app = create_app()
    with owner_app.app_context():
        db.create_all()
        queued_id = "q" * 32
        db.session.add(
            BackgroundJob(
                id=queued_id,
                job_type="property_ai_analysis",
                status="queued",
                # Already past its own initial TTL -- the queue was long.
                lease_expires_at=_expired_lease(minutes=1),
            )
        )
        db.session.commit()

    # The owning process registers the job (as enqueue_job does at claim
    # time) and its heartbeat ticks once (as the daemon thread would,
    # HEARTBEAT_INTERVAL_S later) -- extending the lease past the moment
    # the other process below checks it.
    _register_owned_job(queued_id)
    try:
        with owner_app.app_context():
            _renew_owned_leases(owner_app)

        other_app = create_app()
        with other_app.app_context():
            assert get_job(queued_id)["status"] == "queued", (
                "a heartbeat-renewed job must survive a different process's "
                "reconcile_orphaned_jobs sweep, even while still only queued"
            )
    finally:
        _unregister_owned_job(queued_id)
        with owner_app.app_context():
            db.drop_all()


def test_a_job_without_a_heartbeat_owner_is_reaped(tmp_path, monkeypatch):
    """The other half of finding 1: a job whose owning process is genuinely
    gone -- never registered here at all -- has nothing renewing its lease,
    and a second, independent `create_app()` correctly reaps it once
    expired."""
    setup_test_environment()
    db_path = tmp_path / "shared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    dead_app = create_app()
    with dead_app.app_context():
        db.create_all()
        job_id = "z" * 32
        db.session.add(
            BackgroundJob(
                id=job_id,
                job_type="property_ai_analysis",
                status="queued",
                lease_expires_at=_expired_lease(minutes=1),
            )
        )
        db.session.commit()

    # No _register_owned_job call for job_id anywhere -- simulating the
    # process that queued it having died before ever renewing anything.
    other_app = create_app()
    with other_app.app_context():
        assert get_job(job_id)["status"] == "interrupted"
        db.drop_all()


# --- Compare-and-swap writes (#190 review round 3, finding 2) -------------


def test_a_reap_mid_execution_prevents_a_late_write_from_resurrecting_it(
    app, inline_executor
):
    """A job is genuinely `running` when something reaps the row out from
    under it mid-execution (an administrative intervention, or a lease
    sweep from another process racing an unusually delayed heartbeat) --
    the worker's own subsequent "mark success" write must find the row no
    longer `running` (a CAS miss), must not resurrect it, and must discard
    its result rather than store it over the reap (#190 review round 3,
    finding 2).
    """

    def _fn():
        # Executes inside _run_job's already-open app context (inline
        # executor, same thread) -- db.session here is exactly the session
        # the subsequent CAS write will use, the same way a concurrent
        # reaper in a different process would race it in production. The
        # running row is looked up by status, not a captured id: with
        # inline_executor, enqueue_job below runs this closure to
        # completion *before it returns*, so no id could be captured first.
        running_row = BackgroundJob.query.filter_by(status="running").one()
        BackgroundJob.query.filter_by(id=running_row.id).update(
            {
                "status": "interrupted",
                "error": "reaped mid-execution for this test",
                "finished_at": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
        db.session.commit()
        return {"success": True, "should_never_be_stored": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)

    with app.app_context():
        job = get_job(job_id)

    assert job["status"] == "interrupted", (
        "a late success write must never resurrect a row a reaper already terminalized"
    )
    assert job["error"] == "reaped mid-execution for this test"
    assert job["result"] is None, "the discarded result must never be stored"


def test_a_reap_before_mark_running_prevents_the_job_from_starting(app):
    """The earlier CAS: if a row is reaped between enqueue and the worker's
    own "mark running" write, that write must lose the CAS (status is no
    longer `queued`), and the (potentially paid) job function must never
    run at all.

    Inserts the row directly (rather than via enqueue_job) and calls
    _run_job directly, so the "already reaped before the worker starts"
    state exists *before* _run_job's own "mark running" write runs --
    enqueue_job with inline_executor would run the whole job to completion
    in one call, leaving no such window to reap into.
    """
    from services.background_jobs import _run_job

    ran = {"value": False}

    def _fn():
        ran["value"] = True
        return {"success": True}

    job_id = "r" * 32
    _insert_row(
        app,
        id=job_id,
        job_type="property_ai_analysis",
        status="interrupted",
        error="reaped before start",
    )

    with app.app_context():
        _run_job(app, job_id, _fn)

    assert ran["value"] is False, "a reaped job must never execute its (paid) closure"
    with app.app_context():
        assert get_job(job_id)["status"] == "interrupted"


# --- Domain writes and the terminal CAS share one transaction (#190 review
# round 4, finding 4) --------------------------------------------------------


def test_a_reap_via_a_separate_connection_discards_fns_staged_domain_write(
    tmp_path, monkeypatch
):
    """A reaped job that is still alive inside `fn()` must not let its
    staged domain writes (an AI analysis, a variant row) reach the database
    just because `fn()` itself returned normally -- the terminal CAS still
    has to match for any of it to land (#190 review round 4, finding 4).

    Needs two genuinely independent connections to simulate honestly: a
    reap arriving through a *different* session (a different process, in
    production) is invisible to `fn()`'s own session until that session
    looks again, which only this suite's file-backed-SQLite pattern (round
    3's heartbeat tests use the same one) can provide -- the shared
    in-memory `StaticPool` connection the rest of this file uses would make
    the "different connection" fiction incoherent (both sessions would
    really be the same one transaction).

    `job_app` plays the worker actually executing `fn()`; `reaper_app`
    plays whatever reaps the row out from under it -- an administrative
    intervention, or a lease sweep from a different process -- while
    `fn()`'s own domain write is still only staged, never committed.
    """
    from models import Property

    setup_test_environment()
    db_path = tmp_path / "shared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    job_app = create_app()
    with job_app.app_context():
        db.create_all()
        job_id = "d" * 32
        db.session.add(
            BackgroundJob(
                id=job_id,
                job_type="property_ai_analysis",
                status="queued",
                # A valid, unexpired lease: create_app()'s own reconciliation
                # sweep runs on every call (including reaper_app's, below)
                # and would otherwise mark a lease-less row interrupted
                # immediately -- before fn() ever gets a chance to run its
                # own simulated reap through a different connection.
                lease_expires_at=_future_lease(),
            )
        )
        db.session.add(
            Property(id=1, title="original", price=1, area=1, source_email_id="e1")
        )
        db.session.commit()

    reaper_app = create_app()

    def _fn():
        prop = db.session.get(Property, 1)
        prop.title = "should never be persisted"
        # Staged, not committed -- exactly what the real AI-analysis
        # closures do post-fix (routes/api_routes.py). A genuinely
        # different connection reaps the row right now, independently of
        # this one, and commits that on its own.
        with reaper_app.app_context():
            BackgroundJob.query.filter_by(id=job_id).update(
                {
                    "status": "interrupted",
                    "error": "reaped by a different connection",
                },
                synchronize_session=False,
            )
            db.session.commit()
        return {"success": True, "should_never_be_stored": True}

    from services.background_jobs import _execute_job

    with job_app.app_context():
        _execute_job(job_id, _fn)

    with job_app.app_context():
        job = get_job(job_id)
        prop = db.session.get(Property, 1)

        assert job["status"] == "interrupted"
        assert job["error"] == "reaped by a different connection"
        assert job["result"] is None
        assert prop.title == "original", (
            "fn()'s staged domain write must never reach the database once "
            "the terminal CAS lost the race to an independent reap"
        )
        db.drop_all()


# --- An ambiguous commit failure is not a lost race (#190 review round 4,
# finding 2) ----------------------------------------------------------------


def test_a_write_that_lands_but_loses_its_acknowledgement_still_runs_fn_once(
    app, inline_executor, monkeypatch
):
    """PostgreSQL commits the "mark running" write server-side, but this
    process never finds out about it -- a connection dropped on the way
    back, the exact shape of #190 review round 4, finding 2. The old code
    could not tell that apart from a genuine failure: it returned `False`,
    a retry's own CAS then matched zero rows (the write, after all, had
    already happened), and that looked exactly like a lost race -- the job
    would never run at all, stuck until its lease expired.

    Simulated here by wrapping `_try_write`: the first call performs the
    *real* write (so it genuinely, verifiably lands) and then reports it as
    failed anyway -- exactly what a lost acknowledgement looks like from
    `_write_status`'s side. The retry that follows calls through to the
    real `_try_write` again, unwrapped behaviour this time, so it is the
    actual `_matches_our_own_write` logic under test recognizing that the
    row already shows exactly what this write was trying to record.
    """
    real_try_write = background_jobs._try_write
    state = {"pretended_failure_once": False}

    def _land_then_pretend_to_fail(session, job_id, expected_status, fields):
        outcome = real_try_write(session, job_id, expected_status, fields)
        if (
            fields.get("status") == "running"
            and outcome is True
            and not state["pretended_failure_once"]
        ):
            state["pretended_failure_once"] = True
            return False  # the write landed; report it as if it had not
        return outcome

    monkeypatch.setattr(background_jobs, "_try_write", _land_then_pretend_to_fail)

    calls = {"fn": 0}

    def _fn():
        calls["fn"] += 1
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert state["pretended_failure_once"] is True, (
        "the test did not exercise the ambiguous-write path it was meant to"
    )
    assert calls["fn"] == 1, (
        "fn() must run exactly once: not skipped (treating the ambiguous "
        "write as a lost race) and not run twice (treating it as a clean "
        "retry from scratch)"
    )
    assert job["status"] == "success"
    assert job["result"] == {"success": True}


def test_a_write_that_genuinely_failed_is_not_mistaken_for_success(
    app, inline_executor, monkeypatch
):
    """Negative control: when a write genuinely never reaches the database
    (not just loses its acknowledgement), `_matches_our_own_write` must
    correctly find nothing and let the failure stand -- the disambiguation
    round 4's finding 2 added must not turn every transient failure into a
    false success.
    """
    monkeypatch.setattr(background_jobs, "_try_write", lambda *a, **kw: False)

    def _fn():
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert job["status"] == "queued", (
        "a write that truly never landed must not be reinterpreted as a "
        "success just because it failed"
    )


# --- The insert's own commit can be ambiguous too (#190 review round 5) ---


def test_an_insert_that_lands_but_loses_its_acknowledgement_still_runs_fn_once(
    app, inline_executor, monkeypatch
):
    """Mirrors round 4's ambiguous-write fix, one step earlier: PostgreSQL
    commits the fresh `queued` row's INSERT server-side, but this process
    never finds out -- a connection dropped on the way back. The old code
    only ever caught `IntegrityError` around this commit; anything else (an
    `OperationalError`, the actual shape a dropped connection raises)
    propagated straight out of `_acquire_job_slot` as a 500, *before*
    `enqueue_job` ever registered ownership or dispatched `fn()`. The row
    still landed as an orphaned `queued` insert nobody was running -- a
    retry's own liveness check would find it live and defer to its id
    forever, never calling `fn()`, stuck until its lease expired.

    Simulated by monkeypatching `Session.commit` at the SQLAlchemy class
    level -- nothing narrower is available for a plain `db.session.
    commit()` call the way `_try_write` gave the round-4 fix a function to
    wrap. The first call performs the *real* commit (so the insert
    genuinely, verifiably lands) and then raises anyway, exactly what a
    lost acknowledgement looks like.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SQLASession

    real_commit = SQLASession.commit
    state = {"raised_once": False}

    def _commit_that_lands_then_loses_its_ack(self, *args, **kwargs):
        result = real_commit(self, *args, **kwargs)
        if not state["raised_once"]:
            state["raised_once"] = True
            raise OperationalError(
                "simulated dropped connection after commit", {}, Exception("boom")
            )
        return result

    monkeypatch.setattr(SQLASession, "commit", _commit_that_lands_then_loses_its_ack)

    calls = {"fn": 0}

    def _fn():
        calls["fn"] += 1
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert state["raised_once"] is True, (
        "the test did not exercise the ambiguous insert-commit path it was meant to"
    )
    assert calls["fn"] == 1, (
        "fn() must run exactly once: an insert that actually landed must "
        "still be picked up and dispatched, not left stuck as an "
        "unrun orphaned queued row, and not run a second time either"
    )
    assert job["status"] == "success"
    assert job["result"] == {"success": True}


def test_an_insert_that_genuinely_never_landed_still_raises(app, monkeypatch):
    """Negative control: when the insert's commit genuinely never lands
    (not just loses its acknowledgement), `_matches_our_own_insert` must
    correctly find nothing, the original exception must propagate, and no
    row must be left behind -- the disambiguation must not turn every
    transient insert failure into a false success.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SQLASession

    real_commit = SQLASession.commit
    state = {"raised_once": False}

    def _commit_that_never_lands(self, *args, **kwargs):
        if not state["raised_once"]:
            state["raised_once"] = True
            raise OperationalError("simulated failed commit", {}, Exception("boom"))
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(SQLASession, "commit", _commit_that_never_lands)

    def _fn():
        return {"success": True}

    with app.app_context(), pytest.raises(OperationalError):
        enqueue_job(_fn, job_type="property_ai_analysis", app=app)

    assert state["raised_once"] is True, (
        "the test did not exercise the insert-commit failure path it was meant to"
    )
    with app.app_context():
        rows = BackgroundJob.query.filter_by(job_type="property_ai_analysis").all()
    assert rows == [], (
        "an insert whose commit genuinely never landed must leave no row behind"
    )


def test_run_job_sync_also_recovers_from_an_ambiguous_insert_commit(app, monkeypatch):
    """The disambiguation applies to the synchronous path too
    (`run_job_sync`, used by `?sync=1` and `TESTING`) since it shares
    `_acquire_job_slot` with the async path -- an ambiguous insert-commit
    failure there must not answer 500 for work that, in fact, already
    started (#190 review round 5).
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SQLASession

    from services.background_jobs import run_job_sync

    real_commit = SQLASession.commit
    state = {"raised_once": False}

    def _commit_that_lands_then_loses_its_ack(self, *args, **kwargs):
        result = real_commit(self, *args, **kwargs)
        if not state["raised_once"]:
            state["raised_once"] = True
            raise OperationalError("simulated", {}, Exception("boom"))
        return result

    monkeypatch.setattr(SQLASession, "commit", _commit_that_lands_then_loses_its_ack)

    calls = {"fn": 0}

    def _fn():
        calls["fn"] += 1
        return {"success": True}

    with app.app_context():
        job_id = run_job_sync(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert state["raised_once"] is True
    assert calls["fn"] == 1
    assert job["status"] == "success"
    assert job["result"] == {"success": True}


# --- A failed disambiguation attempt is not the same as a clean "no row"
# (#190 review round 7) --------------------------------------------------


def test_a_disambiguation_attempt_that_fails_once_retries_and_still_dispatches(
    app, inline_executor, monkeypatch
):
    """The disambiguation's own wait/re-read can itself fail -- a
    `lock_timeout`, a `statement_timeout`, a cancelled query, a dropped
    connection -- and that failure is not evidence the original insert
    never happened. `_matches_our_own_insert` must retry a *failed
    attempt* (not silently treat it as a clean negative) before concluding
    anything.

    Two independent ambiguities are stacked here: the insert's own commit
    raises first (as in round 5/6), and then the *first* disambiguation
    attempt raises too (the round-7 scenario) before a second attempt
    succeeds cleanly and finds the row. fn() must still run exactly once.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SQLASession

    real_commit = SQLASession.commit
    commit_raised_once = {"value": False}

    def _commit_that_lands_then_loses_its_ack(self, *args, **kwargs):
        result = real_commit(self, *args, **kwargs)
        if not commit_raised_once["value"]:
            commit_raised_once["value"] = True
            raise OperationalError(
                "simulated dropped connection after commit", {}, Exception("boom")
            )
        return result

    monkeypatch.setattr(SQLASession, "commit", _commit_that_lands_then_loses_its_ack)

    real_attempt = background_jobs._attempt_insert_disambiguation
    disambiguation_calls = {"n": 0}

    def _flaky_disambiguation(job_id, dedupe_key):
        disambiguation_calls["n"] += 1
        if disambiguation_calls["n"] == 1:
            raise OperationalError(
                "simulated lock_timeout during disambiguation", {}, Exception("boom")
            )
        return real_attempt(job_id, dedupe_key)

    monkeypatch.setattr(
        background_jobs, "_attempt_insert_disambiguation", _flaky_disambiguation
    )

    calls = {"fn": 0}

    def _fn():
        calls["fn"] += 1
        return {"success": True}

    with app.app_context():
        job_id = enqueue_job(_fn, job_type="property_ai_analysis", app=app)
        job = get_job(job_id)

    assert disambiguation_calls["n"] == 2, (
        "must retry after the first disambiguation attempt itself failed, "
        "not give up or treat that failure as a clean 'no row'"
    )
    assert calls["fn"] == 1, "the job that actually landed must still be dispatched"
    assert job["status"] == "success"
    assert job["result"] == {"success": True}


def test_a_disambiguation_that_always_fails_raises_an_explicit_unknown_outcome(
    app, monkeypatch
):
    """When every disambiguation attempt itself fails -- not a clean read
    finding no row, but the wait/read machinery failing outright each time
    -- `_acquire_job_slot` must not fall back to re-raising the original
    commit exception as if the insert had genuinely never happened (the
    round-6 shape this closes). It must raise the dedicated
    `EnqueueOutcomeUnknown` instead, and never register ownership for a
    job_id this process could not confirm.

    What happens next -- a later `enqueue_job` reaping the row once its
    lease actually expires and dispatching a clean replacement -- is
    already covered by this file's lease-model tests (e.g.
    `test_an_expired_lease_is_reaped_instead_of_blocking_dedupe_forever`);
    not duplicated here.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SQLASession

    from services.background_jobs import EnqueueOutcomeUnknown

    real_commit = SQLASession.commit
    commit_raised_once = {"value": False}

    def _commit_that_lands_then_loses_its_ack(self, *args, **kwargs):
        result = real_commit(self, *args, **kwargs)
        if not commit_raised_once["value"]:
            commit_raised_once["value"] = True
            raise OperationalError(
                "simulated dropped connection after commit", {}, Exception("boom")
            )
        return result

    monkeypatch.setattr(SQLASession, "commit", _commit_that_lands_then_loses_its_ack)

    def _always_fails(job_id, dedupe_key):
        raise OperationalError(
            "simulated lock_timeout during disambiguation", {}, Exception("boom")
        )

    monkeypatch.setattr(
        background_jobs, "_attempt_insert_disambiguation", _always_fails
    )

    def _fn():
        return {"success": True}

    with app.app_context():
        with pytest.raises(EnqueueOutcomeUnknown) as exc_info:
            enqueue_job(_fn, job_type="property_ai_analysis", app=app)

    assert exc_info.value.dedupe_key is None

    # Ownership must never be registered when the outcome is unknown --
    # nothing in this single-threaded test could have registered any other
    # job either, so an empty set is exactly the claim.
    with background_jobs._owned_job_ids_lock:
        assert not background_jobs._owned_job_ids, (
            "a job whose enqueue outcome is unknown must never be registered as owned"
        )


def test_a_dedupe_race_defers_to_a_replacement_that_already_finished(
    app, inline_executor, monkeypatch
):
    """Reproduces the reviewer's exact sequence (#190 review round 3,
    finding 3 / "F4-bis"): A and B both start racing for the same expired
    J0. A wins the reap, inserts J1, and J1 finishes (inline executor)
    before B's own retry runs. Without deferring to J1 regardless of its
    (by-then terminal) status, B's insert would succeed cleanly once J1's
    terminal status frees the partial unique index -- a second, paid
    execution for the same dedupe_key.

    J0's own `created_at` is set explicitly, safely in the past: it is now
    a `server_default` (models.py, #190 review round 4, finding 2) rather
    than a Python-computed value, and SQLite's `CURRENT_TIMESTAMP` only has
    second-level resolution -- J0 and J1 could otherwise tie under a fast
    test, which would make `_find_superseding_row`'s `created_at >
    baseline` comparison this test is pinning fail to see J1 as newer at
    all. Real PostgreSQL timestamps have microsecond resolution, so this is
    a test-environment-only concern, the same category the module
    docstring already documents for SQLite's shared connection.
    """
    from services.background_jobs import _baseline_snapshot

    key = "property_ai_analysis:355:claude"
    stale_id = "0" * 32
    _insert_row(
        app,
        id=stale_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key=key,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        lease_expires_at=_expired_lease(),
    )

    # "B" starts racing here, while only the stale J0 exists -- captured
    # now, applied to B's own enqueue_job call below via monkeypatch, to
    # reproduce the interleaving where B's attempt began before A's
    # replacement was ever created.
    with app.app_context():
        b_baseline = _baseline_snapshot(db, key)

    calls = {"a": 0, "b": 0}

    def _a_fn():
        calls["a"] += 1
        return {"success": True, "who": "a"}

    def _b_fn():
        calls["b"] += 1
        return {"success": True, "who": "b"}

    # "A": reaps J0, inserts J1, and (inline executor) J1 runs to
    # completion before B's call even begins.
    with app.app_context():
        a_id = enqueue_job(
            _a_fn, job_type="property_ai_analysis", app=app, dedupe_key=key
        )
    with app.app_context():
        assert get_job(a_id)["status"] == "success", "J1 must have already finished"

    # "B" resumes its race using the baseline it captured before A acted.
    monkeypatch.setattr(
        background_jobs, "_baseline_snapshot", lambda *a, **kw: b_baseline
    )
    with app.app_context():
        b_id = enqueue_job(
            _b_fn, job_type="property_ai_analysis", app=app, dedupe_key=key
        )

    assert b_id == a_id, "B must defer to A's already-finished replacement"
    assert calls == {"a": 1, "b": 0}, "B's own paid closure must never run"

    with app.app_context():
        rows_for_key = BackgroundJob.query.filter(
            BackgroundJob.dedupe_key == key
        ).count()
    assert rows_for_key == 2, "exactly J0 (now interrupted) and J1 -- never a J2"


def test_a_dedupe_race_still_allows_a_later_independent_resubmit(app, inline_executor):
    """Negative control: once a race has genuinely settled (no baseline is
    stale relative to a *fresh* call), a later, independent enqueue_job for
    the same dedupe_key must still be allowed to run again -- the
    supersede check must not permanently block retries, only concurrent
    duplicates within one race window."""
    key = "property_ai_analysis:355:claude"

    with app.app_context():
        first_id = enqueue_job(
            lambda: {"success": True},
            job_type="property_ai_analysis",
            app=app,
            dedupe_key=key,
        )
    with app.app_context():
        assert get_job(first_id)["status"] == "success"

    with app.app_context():
        second_id = enqueue_job(
            lambda: {"success": True, "second": True},
            job_type="property_ai_analysis",
            app=app,
            dedupe_key=key,
        )

    assert second_id != first_id, (
        "a fresh, non-racing call must be allowed to run again"
    )
    with app.app_context():
        assert get_job(second_id)["status"] == "success"
        assert get_job(second_id)["result"] == {"success": True, "second": True}
