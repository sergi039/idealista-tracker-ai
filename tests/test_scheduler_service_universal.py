import threading
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import text

from app import create_app, db
from tests import setup_test_environment

# A job that hangs would otherwise stall the suite instead of failing it.
JOB_THREAD_TIMEOUT_SECONDS = 10


@pytest.fixture(autouse=True)
def _setup_env():
    setup_test_environment()


@pytest.fixture
def app(_setup_env):
    """An app that is NOT pushed as the ambient context.

    Yielding from inside `with app.app_context()` would hand every test a
    context for free - which is exactly how #14 hid: the job looked fine under
    test and died on the APScheduler thread, where no context exists.
    """
    app = create_app(testing=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.drop_all()


def run_off_the_main_thread(job):
    """Run a job the way APScheduler does: a bare worker thread, no context.

    App contexts are thread-local, so a context pushed anywhere else cannot
    leak in and mask a missing one here.
    """
    raised = []

    def target():
        try:
            job()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            raised.append(exc)

    thread = threading.Thread(target=target, name="test-scheduler-job")
    thread.start()
    thread.join(timeout=JOB_THREAD_TIMEOUT_SECONDS)

    assert not thread.is_alive(), (
        f"job thread still running after {JOB_THREAD_TIMEOUT_SECONDS}s"
    )
    if raised:
        raise raised[0]


@pytest.fixture
def scheduler_app(app, monkeypatch):
    """Bind the app the way init_scheduler() does, without starting a scheduler.

    Jobs read the module-level `flask_app`; tests that call them directly have
    to provide it or they are testing a configuration that cannot occur.
    """
    import services.scheduler_service as scheduler_service

    monkeypatch.setattr(scheduler_service, "flask_app", app)
    return app


def test_run_scheduled_ingestion_uses_property_target_by_default(
    monkeypatch, scheduler_app
):
    from config import Config
    from services.scheduler_service import run_scheduled_ingestion

    monkeypatch.setattr(Config, "INGESTION_TARGET", "properties", raising=False)

    mock_instance = Mock()
    mock_instance.run_ingestion.return_value = 7

    with patch(
        "services.property_imap_service.PropertyIMAPService", return_value=mock_instance
    ) as mock_ctor:
        run_scheduled_ingestion()
        mock_ctor.assert_called_once()
        mock_instance.run_ingestion.assert_called_once()


def test_run_scheduled_ingestion_uses_legacy_lands_when_configured(
    monkeypatch, scheduler_app
):
    from config import Config
    from services.scheduler_service import run_scheduled_ingestion

    monkeypatch.setattr(Config, "INGESTION_TARGET", "lands", raising=False)

    mock_instance = Mock()
    mock_instance.run_ingestion.return_value = 3

    with patch(
        "services.imap_service.IMAPService", return_value=mock_instance
    ) as mock_ctor:
        run_scheduled_ingestion()
        mock_ctor.assert_called_once()
        mock_instance.run_ingestion.assert_called_once()


def test_run_listing_status_check_is_skipped_for_properties(monkeypatch, scheduler_app):
    from config import Config
    from services.scheduler_service import run_listing_status_check

    monkeypatch.setattr(Config, "INGESTION_TARGET", "properties", raising=False)

    with patch("services.listing_status_service.ListingStatusService") as mock_service:
        run_listing_status_check()
        mock_service.assert_not_called()


def test_run_listing_status_check_runs_for_lands(monkeypatch, scheduler_app):
    from config import Config
    from services.scheduler_service import run_listing_status_check

    monkeypatch.setattr(Config, "INGESTION_TARGET", "lands", raising=False)

    mock_instance = Mock()
    mock_instance.check_favorites_status.return_value = {
        "checked": 0,
        "removed": 0,
        "sold": 0,
    }

    with patch(
        "services.listing_status_service.ListingStatusService",
        return_value=mock_instance,
    ) as mock_ctor:
        run_listing_status_check()
        mock_ctor.assert_called_once()
        mock_instance.check_favorites_status.assert_called_once()


class TestJobsGetAnAppContext:
    """#14 - jobs ran on APScheduler worker threads with no Flask app context.

    Every `db.session` call inside them raised "Working outside of application
    context", the job body's own `except Exception` logged it, and APScheduler
    still reported "executed successfully". Ingestion was dead and silent from
    February to August 2026.

    The pre-fix suite mocked the services wholesale, so the database call that
    actually blew up never ran. These fakes touch the session the way the real
    services do, which is the only way the missing context shows up.
    """

    @staticmethod
    def _session_touching_service(calls):
        """A service whose ingestion needs a live session, like the real one."""

        class Service:
            def run_ingestion(self):
                db.session.execute(text("SELECT 1"))
                calls.append("ingested")
                return 1

            def check_favorites_status(self, limit=None):
                db.session.execute(text("SELECT 1"))
                calls.append("checked")
                return {"checked": 0, "removed": 0, "sold": 0}

        return Service

    def test_ingestion_reaches_the_database(self, monkeypatch, scheduler_app):
        from config import Config
        from services.scheduler_service import run_scheduled_ingestion

        monkeypatch.setattr(Config, "INGESTION_TARGET", "properties", raising=False)
        calls = []

        with patch(
            "services.property_imap_service.PropertyIMAPService",
            self._session_touching_service(calls),
        ):
            run_off_the_main_thread(run_scheduled_ingestion)

        assert calls == ["ingested"], (
            "ingestion job did not complete - the db.session call inside it was "
            "swallowed, which is #14 all over again"
        )

    def test_listing_status_check_reaches_the_database(
        self, monkeypatch, scheduler_app
    ):
        from config import Config
        from services.scheduler_service import run_listing_status_check

        monkeypatch.setattr(Config, "INGESTION_TARGET", "lands", raising=False)
        calls = []

        with patch(
            "services.listing_status_service.ListingStatusService",
            self._session_touching_service(calls),
        ):
            run_off_the_main_thread(run_listing_status_check)

        assert calls == ["checked"], (
            "listing status job did not complete - the db.session call inside it "
            "was swallowed, which is #14 all over again"
        )

    def test_init_scheduler_binds_the_app_before_jobs_can_fire(self, app, monkeypatch):
        """The binding is what makes the context available at job time."""
        import services.scheduler_service as scheduler_service
        from config import Config

        monkeypatch.setattr(scheduler_service, "scheduler", None)
        monkeypatch.setattr(scheduler_service, "flask_app", None)
        monkeypatch.setattr(Config, "AUTO_START_SCHEDULER", True, raising=False)
        app.config["TESTING"] = False

        started = scheduler_service.init_scheduler(app)
        try:
            assert started is not None, "scheduler failed to start"
            assert scheduler_service.flask_app is app
        finally:
            started.shutdown(wait=False)
            scheduler_service.scheduler = None
            if scheduler_service.scheduler_lock_file:
                scheduler_service.scheduler_lock_file.close()
                scheduler_service.scheduler_lock_file = None

    def test_missing_app_is_a_loud_error_not_a_silent_skip(self, monkeypatch):
        """A scheduler whose jobs can never reach the DB must say so."""
        import services.scheduler_service as scheduler_service

        monkeypatch.setattr(scheduler_service, "flask_app", None)

        with pytest.raises(RuntimeError, match="init_scheduler"):
            with scheduler_service.job_app_context():
                pass
