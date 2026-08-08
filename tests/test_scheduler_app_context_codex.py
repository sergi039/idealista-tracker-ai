"""Adversarial regression tests for scheduler jobs crossing Flask thread bounds.

The production boundary is a bare worker thread calling the function registered
with APScheduler.  None of the fixtures in this module leaves an application
context pushed on the test thread, and every database probe uses the real
Flask-SQLAlchemy extension with an in-memory SQLite engine.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import ModuleType
from typing import Callable

import pytest
from flask import current_app, has_app_context
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from config import Config  # noqa: E402
from services import scheduler_service  # noqa: E402


THREAD_TIMEOUT_SECONDS = 10


@dataclass
class ThreadOutcome:
    initial_has_context: bool | None = None
    after_each_run: list[bool] = field(default_factory=list)
    final_has_context: bool | None = None
    exception: BaseException | None = None


def run_on_scheduler_thread(job: Callable[[], None], *, repetitions: int = 1):
    """Run exactly as APScheduler does: on a new thread with no ambient context."""
    outcome = ThreadOutcome()

    def target():
        outcome.initial_has_context = has_app_context()
        try:
            for _ in range(repetitions):
                job()
                outcome.after_each_run.append(has_app_context())
        except BaseException as exc:  # noqa: BLE001 - the assertion inspects it
            outcome.exception = exc
        finally:
            outcome.final_has_context = has_app_context()

    thread = threading.Thread(target=target, name="apscheduler-test-worker")
    thread.start()
    thread.join(timeout=THREAD_TIMEOUT_SECONDS)
    assert not thread.is_alive(), (
        f"scheduler worker did not finish in {THREAD_TIMEOUT_SECONDS} seconds"
    )
    assert outcome.initial_has_context is False
    return outcome


@pytest.fixture
def app(monkeypatch):
    """Create an app without yielding from inside its application context."""
    setup_test_environment()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AUTO_CREATE_DB", "false")
    monkeypatch.setenv("AUTO_START_SCHEDULER", "false")

    flask_app = create_app(testing=True)
    flask_app.config["SCHEDULER_APP_ID"] = "expected"
    with flask_app.app_context():
        db.create_all()

    yield flask_app

    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def bound_app(app, monkeypatch):
    monkeypatch.setattr(scheduler_service, "flask_app", app)
    return app


def _patch_job_service(
    monkeypatch,
    *,
    kind: str,
    service_class: type,
) -> Callable[[], None]:
    """Replace only the network-owning service while preserving real DB access."""
    if kind == "ingestion":
        import services.property_imap_service as service_module

        monkeypatch.setattr(Config, "INGESTION_TARGET", "properties", raising=False)
        attribute = "PropertyIMAPService"
        job = scheduler_service.run_scheduled_ingestion
    elif kind == "listing":
        import services.listing_status_service as service_module

        monkeypatch.setattr(Config, "INGESTION_TARGET", "lands", raising=False)
        attribute = "ListingStatusService"
        job = scheduler_service.run_listing_status_check
    else:  # pragma: no cover - helper has only the two production job kinds
        raise AssertionError(f"unknown job kind: {kind}")

    assert isinstance(service_module, ModuleType)
    monkeypatch.setattr(service_module, attribute, service_class)
    return job


@pytest.mark.parametrize("kind", ["ingestion", "listing"])
def test_database_work_runs_inside_context_on_worker_thread(
    kind, monkeypatch, bound_app
):
    """Failure mode 1: the job's database work still runs outside app context."""
    completed = []

    class SessionTouchingService:
        def run_ingestion(self):
            db.session.execute(text("SELECT 1"))
            completed.append("ingestion")
            return 1

        def check_favorites_status(self, limit):
            assert limit == 30
            db.session.execute(text("SELECT 1"))
            completed.append("listing")
            return {"checked": 0, "removed": 0, "sold": 0, "details": []}

    job = _patch_job_service(
        monkeypatch, kind=kind, service_class=SessionTouchingService
    )
    outcome = run_on_scheduler_thread(job)

    assert outcome.exception is None
    assert completed == [kind], "the job swallowed the failed db.session access"
    assert outcome.final_has_context is False


def test_context_covers_construction_service_call_and_post_processing(
    monkeypatch, bound_app
):
    """Failure mode 2: only part of a job is protected by the app context."""
    phases = []

    class SessionBackedResults(dict):
        def __getitem__(self, key):
            db.session.execute(text("SELECT 1"))
            phases.append(f"post:{key}")
            return super().__getitem__(key)

        def get(self, key, default=None):
            db.session.execute(text("SELECT 1"))
            phases.append(f"post:get:{key}")
            return super().get(key, default)

    class ContextAtEveryPhaseService:
        def __init__(self):
            db.session.execute(text("SELECT 1"))
            phases.append("constructed")

        def check_favorites_status(self, limit):
            assert limit == 30
            db.session.execute(text("SELECT 1"))
            phases.append("service-call")
            return SessionBackedResults(checked=0, removed=0, sold=0, details=[])

    job = _patch_job_service(
        monkeypatch, kind="listing", service_class=ContextAtEveryPhaseService
    )
    outcome = run_on_scheduler_thread(job)

    assert outcome.exception is None
    assert phases == [
        "constructed",
        "service-call",
        "post:checked",
        "post:removed",
        "post:sold",
        "post:get:details",
    ], "database-backed result processing escaped the job's app context"
    assert outcome.final_has_context is False


def test_repeated_runs_do_not_leak_application_context(monkeypatch, bound_app):
    """Failure mode 3: a pushed context is never popped across repeated runs."""
    calls = []

    class SessionTouchingService:
        def run_ingestion(self):
            db.session.execute(text("SELECT 1"))
            calls.append("ran")
            return 1

    job = _patch_job_service(
        monkeypatch, kind="ingestion", service_class=SessionTouchingService
    )
    outcome = run_on_scheduler_thread(job, repetitions=2)

    assert outcome.exception is None
    assert calls == ["ran", "ran"]
    assert outcome.after_each_run == [False, False]
    assert outcome.final_has_context is False


class CapturingScheduler:
    """Small APScheduler stand-in; jobs themselves still run on a real thread."""

    def __init__(self):
        self.jobs = []
        self.running = False

    def add_job(self, **kwargs):
        self.jobs.append(kwargs)

    def start(self):
        self.running = True

    def shutdown(self, *args, **kwargs):
        self.running = False

    def get_jobs(self):
        return []


def _probe_rows(flask_app):
    with flask_app.app_context():
        table_exists = db.session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'scheduler_binding_probe'"
            )
        ).scalar_one_or_none()
        if table_exists is None:
            return []
        return list(
            db.session.execute(
                text("SELECT app_id FROM scheduler_binding_probe ORDER BY app_id")
            ).scalars()
        )


def test_init_rebinds_jobs_to_the_intended_app_and_database(app, monkeypatch, tmp_path):
    """Failure mode 4: a stale/wrong app selects the wrong config and database."""
    stale_app = create_app(testing=True)
    stale_app.config["SCHEDULER_APP_ID"] = "stale"
    with stale_app.app_context():
        db.create_all()

    monkeypatch.setattr(scheduler_service, "scheduler", None)
    monkeypatch.setattr(scheduler_service, "scheduler_lock_file", None)
    monkeypatch.setattr(scheduler_service, "flask_app", stale_app)
    monkeypatch.setattr(scheduler_service, "BackgroundScheduler", CapturingScheduler)
    monkeypatch.setattr(scheduler_service.atexit, "register", lambda callback: None)
    monkeypatch.setattr(scheduler_service.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(scheduler_service.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(Config, "AUTO_START_SCHEDULER", True, raising=False)
    app.config["TESTING"] = False

    observed_apps = []

    class BindingProbeService:
        def __init__(self):
            app_id = current_app.config["SCHEDULER_APP_ID"]
            observed_apps.append(app_id)
            db.session.execute(
                text("CREATE TABLE scheduler_binding_probe (app_id TEXT NOT NULL)")
            )
            db.session.execute(
                text("INSERT INTO scheduler_binding_probe (app_id) VALUES (:app_id)"),
                {"app_id": app_id},
            )
            db.session.commit()

        def run_ingestion(self):
            return 1

    job = _patch_job_service(
        monkeypatch, kind="ingestion", service_class=BindingProbeService
    )

    started = scheduler_service.init_scheduler(app)
    try:
        assert started is not None
        outcome = run_on_scheduler_thread(job)
        assert outcome.exception is None
        assert observed_apps == ["expected"]
        assert _probe_rows(app) == ["expected"]
        assert _probe_rows(stale_app) == []
    finally:
        if started is not None:
            started.shutdown()
        lock_file = scheduler_service.scheduler_lock_file
        if lock_file is not None and not lock_file.closed:
            lock_file.close()
        scheduler_service.scheduler_lock_file = None
        with stale_app.app_context():
            db.session.remove()
            db.drop_all()


class ScheduledJobFailure(RuntimeError):
    pass


@pytest.mark.parametrize("kind", ["ingestion", "listing"])
def test_job_failure_propagates_to_scheduler(kind, monkeypatch, bound_app):
    """Failure mode 5: an internal failure is swallowed and reported as success."""

    class ExplodingService:
        def run_ingestion(self):
            db.session.execute(text("SELECT 1"))
            raise ScheduledJobFailure("ingestion exploded")

        def check_favorites_status(self, limit):
            assert limit == 30
            db.session.execute(text("SELECT 1"))
            raise ScheduledJobFailure("listing exploded")

    job = _patch_job_service(monkeypatch, kind=kind, service_class=ExplodingService)
    outcome = run_on_scheduler_thread(job)

    assert isinstance(outcome.exception, ScheduledJobFailure), (
        "the job swallowed its failure, so APScheduler would report success"
    )
