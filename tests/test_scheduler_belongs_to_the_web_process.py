"""Only the web entry point starts the scheduler (issue #333).

`create_app()` used to start it whenever `AUTO_START_SCHEDULER` was set, and
that flag is set on the compose *service*, so every container built from that
service inherited it -- including the throwaway one from `docker compose run
--rm app python -m utils.<backfill>`.

The reason nobody noticed is that `init_scheduler`'s guard is an `flock` in the
container's own `/tmp`: `docker exec` into the running app shares that
filesystem and correctly skips, a separate container does not and takes the
lock. Measured on the mini 2026-08-15: a `compose run` backfill logged
`Acquired scheduler lock (PID: 1)` and ran the lease reconciliation 45 times in
18 minutes, having also scheduled IMAP ingestion and the throttled idealista
status scrape. Those two did not fire only because the window missed their
clock times.

So these tests drive `create_app()` and the `main` module with the flag ON --
the exact condition production runs under. A test that left the flag at its
default would pass against the defect.

The flag must be set in the **environment**, not on `Config`: `create_app`
re-reads it from `os.environ` at app.py:155, overriding what
`app.config.from_object(Config)` put there a moment earlier. The first version
of this file patched the class attribute, watched the config come back False,
and passed -- green for a reason that had nothing to do with the fix.
"""

import importlib
import sys

import pytest

from app import create_app, should_start_scheduler
from tests import setup_test_environment


@pytest.fixture(autouse=True)
def _env():
    setup_test_environment()


@pytest.fixture
def scheduler_calls(monkeypatch):
    """Record every init_scheduler call without starting anything."""
    calls = []
    import services.scheduler_service as scheduler_service

    monkeypatch.setattr(
        scheduler_service, "init_scheduler", lambda app: calls.append(app)
    )
    return calls


class TestTheFactoryDoesNotStartIt:
    def test_create_app_leaves_the_scheduler_alone_even_with_the_flag_on(
        self, monkeypatch, scheduler_calls
    ):
        """The production condition: the flag is true, this is not the web app."""
        monkeypatch.setenv("AUTO_START_SCHEDULER", "true")

        app = create_app()

        # The flag really did reach the config, so this test is about the
        # factory's behaviour and not about a flag that never arrived.
        assert app.config["AUTO_START_SCHEDULER"] is True
        assert scheduler_calls == [], (
            "create_app() started the scheduler; a backfill run through "
            "`docker compose run` would own it"
        )


class TestTheWebEntryPointStartsIt:
    def test_importing_main_starts_the_scheduler(self, monkeypatch, scheduler_calls):
        monkeypatch.setenv("AUTO_START_SCHEDULER", "true")
        monkeypatch.delitem(sys.modules, "main", raising=False)

        importlib.import_module("main")

        assert len(scheduler_calls) == 1

    def test_importing_main_respects_the_off_switch(self, monkeypatch, scheduler_calls):
        monkeypatch.setenv("AUTO_START_SCHEDULER", "false")
        monkeypatch.delitem(sys.modules, "main", raising=False)

        importlib.import_module("main")

        assert scheduler_calls == []


class TestTheDecisionItself:
    def _app(self, flag, testing):
        return type(
            "A", (), {"config": {"AUTO_START_SCHEDULER": flag, "TESTING": testing}}
        )()

    def test_the_flag_on_and_not_testing_means_yes(self):
        assert should_start_scheduler(self._app(True, False)) is True

    def test_the_flag_off_means_no(self):
        assert should_start_scheduler(self._app(False, False)) is False

    def test_a_test_run_never_owns_the_scheduler(self):
        assert should_start_scheduler(self._app(True, True)) is False

    def test_a_missing_flag_means_no(self):
        """Absence is not permission."""
        app = type("A", (), {"config": {}})()
        assert should_start_scheduler(app) is False
