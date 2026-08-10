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
thread. That runs the exact same `enqueue_job`/`_run`/`_transition` code --
nothing about the code under test is replaced -- it only avoids a genuine
artifact of the *test* database: Flask-SQLAlchemy gives an in-memory SQLite
engine a single shared `StaticPool` connection (app.py's own comment on
`_is_in_memory_sqlite` says as much), and two Python threads issuing
statements over that one pysqlite connection at overlapping instants corrupts
SQLAlchemy's C row-buffer (`IndexError: tuple index out of range` in
`sqlalchemy.cyextension.resultproxy`), independent of anything this module
does. Real PostgreSQL gives every thread its own connection, which is exactly
what `test_postgres_migrations.py`'s concurrent-insert test runs against.
"""

import time

import pytest

from app import create_app, db
from models import BackgroundJob
from services import background_jobs
from services.background_jobs import (
    INTERRUPTED_MESSAGE,
    enqueue_job,
    get_job,
    reconcile_orphaned_jobs,
)
from tests import setup_test_environment


class _ImmediateExecutor:
    """Runs a submitted callable synchronously, in the caller's thread.

    Stands in for the real `ThreadPoolExecutor` only for the SQLite artifact
    described in the module docstring -- `enqueue_job` still calls
    `_EXECUTOR.submit(_run)` exactly as it does in production, this just
    resolves it inline instead of scheduling it onto a second thread.
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


# --- Reconciliation at startup -------------------------------------------


def test_reload_reads_state_a_different_process_wrote(app):
    """The scenario in the issue: process A dies mid-job, process B reads it.

    Nothing here shares Python state between "process A" and "process B" --
    each block opens its own app context and reads the row back from the
    database, the way a freshly started process actually would once the
    in-memory registry it used to keep is gone.
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
    assert "redeploy" in body["job"]["error"].lower()
    assert "run it again" in body["job"]["error"].lower()


def test_api_still_answers_404_for_an_id_that_was_never_enqueued(client):
    resp = client.get("/api/jobs/" + "f" * 32)
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


# --- Idempotent enqueue: acceptance criterion 4 ---------------------------


def test_enqueue_reuses_the_in_flight_job_instead_of_racing_it(app):
    """One job is already active for (property, provider); a resubmit must not
    start a second one racing it for the same PropertyAiAnalysisVariant row."""
    key = "property_ai_analysis:355:claude"
    existing_id = "1" * 32
    _insert_row(
        app,
        id=existing_id,
        job_type="property_ai_analysis",
        status="running",
        dedupe_key=key,
        meta={"property_id": 355, "provider": "claude"},
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
