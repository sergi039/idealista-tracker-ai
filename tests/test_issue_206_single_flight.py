"""Single-flight for the universal Property AI-analysis endpoint (issue #206).

The owner's contract (issue #206 comment, 2026-08-10): a second Enrich press
for the same (property_id, provider) while one is already running must join
the in-flight run and get back the SAME job id -- never pay for the work
twice. Once that run is terminal, the *next* press must be a genuine, fresh
recompute: no cache stands in for it, ever. Different providers never join
each other, and a dead row (expired lease, abandoned by a killed process)
must never capture a press either.

`analyze_universal_property_structured` (routes/api_routes.py) already
passes `dedupe_key=f"property_ai_analysis:{prop.id}:{provider}"` to
`services.background_jobs.enqueue_job` -- built for issue #176/#190 (a
redeploy must not lose an in-flight analysis) well before this issue existed.
That mechanism already gives exactly the properties this issue asks for:
`_find_live_job_id` only ever hands back a row whose status is `queued`/
`running` *and* whose lease has not expired (services/background_jobs.py),
so a terminal row (success/error/interrupted) never blocks a fresh enqueue,
and a dead row is reaped rather than handed back. This module is the
route-level proof of that, closing the one gap the existing coverage left:
`tests/test_issue_176_persist_jobs.py` and
`tests/test_postgres_migrations.py` pin the primitive itself (calling
`enqueue_job` directly, with a hand-built `dedupe_key` string), but nothing
proved the route actually *wires* it -- a refactor could drop the
`dedupe_key=` argument from the route without any existing test noticing.

Every test here drives the real HTTP route through the real async
`enqueue_job` path (`app.config["TESTING"] = False`, no `?sync=1`) -- the
same path a real gunicorn worker thread takes -- mocking only the outbound
provider call (`PropertyAIService.analyze_property_structured`), per this
repo's own rule to mock external APIs, never the thing under test.

The in-flight test needs a job that is genuinely still running when the
second press arrives. Two literally-concurrent threads issuing SQL against
this suite's shared in-memory SQLite connection is exactly the corruption
hazard `tests/test_issue_176_persist_jobs.py`'s module docstring documents
("IndexError: tuple index out of range" in
`sqlalchemy.cyextension.resultproxy`) -- so, following that module's own
precedent, `inline_executor` makes `enqueue_job`'s worker run inline, and
only one background Python thread ever exists (running the whole first
request, blocked on a `threading.Event` inside the mocked provider call).
Every point where the main thread touches the database is synchronized
against that thread being genuinely idle (blocked on an `Event`, issuing no
SQL) first -- never truly overlapping I/O on the shared connection.
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import create_app, db
from models import BackgroundJob, Property
from services import background_jobs
from services.background_jobs import get_job
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    flask_app = create_app()
    # TESTING=False is the point of this module: `_should_run_sync()`
    # (routes/api_routes.py) returns True unconditionally under TESTING,
    # which routes every request through the synchronous `run_job_sync`
    # branch instead of the async `enqueue_job` branch a real browser
    # request actually takes. These tests exist to pin *that* branch's
    # dedupe_key wiring.
    flask_app.config["TESTING"] = False
    flask_app.config["WTF_CSRF_ENABLED"] = False
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


class _ImmediateExecutor:
    """Runs a submitted callable synchronously, in the caller's own thread --
    see tests/test_issue_176_persist_jobs.py's identical fixture for why:
    avoids a genuine artifact of the shared in-memory SQLite connection this
    suite uses, not a stand-in for the code under test. `enqueue_job` still
    calls `_EXECUTOR.submit(_run_job, ...)` exactly as in production; this
    just resolves it inline instead of scheduling it onto a second real
    thread.
    """

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def inline_executor(monkeypatch):
    monkeypatch.setattr(background_jobs, "_EXECUTOR", _ImmediateExecutor())


@pytest.fixture(autouse=True)
def _reset_rate_limits(app):
    """`/api/property/<id>/analyze/structured` carries `3 per 5 minutes`
    (unauthenticated API, issue #136). `app.py`'s `limiter` is a process-wide
    singleton created once at import time, so hits from another test module
    against the same route would otherwise leak into these tests, each of
    which makes several calls against it.
    """
    from app import limiter

    with app.app_context():
        limiter.reset()
    yield


def _make_property(app, key: str) -> int:
    with app.app_context():
        prop = Property(
            source_email_id=f"single_flight_{key}",
            title="Test Property",
            municipality="Alicante",
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


def _success_result(marker: str) -> dict:
    return {
        "status": "success",
        "structured_analysis": {"marker": marker},
        "model": "test-model",
    }


_ANALYZE = "services.property_ai_service.PropertyAIService.analyze_property_structured"


class TestSingleFlightJoinsInFlightRun:
    """Owner's contract: 'Single-flight applies ONLY while a run is in
    flight. A request for the same listing+provider arriving while one is
    already running joins it instead of starting a second one.'
    """

    def test_two_presses_produce_one_job_and_one_provider_call(
        self, app, inline_executor
    ):
        property_id = _make_property(app, "in_flight")
        key = f"property_ai_analysis:{property_id}:claude"

        started = threading.Event()
        release = threading.Event()
        calls = []
        result_holder = {}

        def _slow_analyze(prop, provider=None):
            calls.append(provider)
            started.set()
            release.wait(timeout=10)
            return _success_result("joined")

        def _press_one():
            thread_client = app.test_client()
            result_holder["resp1"] = thread_client.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "claude"},
            )

        with patch(_ANALYZE, side_effect=_slow_analyze):
            thread = threading.Thread(target=_press_one)
            thread.start()

            assert started.wait(timeout=5), (
                "the first press never reached the (real, unmocked) "
                "background-job path -- the async enqueue never actually ran"
            )

            # `thread` is now blocked on `release`, doing no database work at
            # all -- safe for this thread to touch the shared SQLite
            # connection now (see the module docstring).
            with app.app_context():
                row = BackgroundJob.query.filter_by(dedupe_key=key).one()
                assert row.status == "running"
                job_id_1 = row.id

            client2 = app.test_client()
            resp2 = client2.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "claude"},
            )

            release.set()
            thread.join(timeout=10)
            assert not thread.is_alive(), "the first press never finished"

        assert resp2.status_code == 202, resp2.get_data(as_text=True)
        job_id_2 = resp2.get_json()["job_id"]

        resp1 = result_holder["resp1"]
        assert resp1.status_code == 202, resp1.get_data(as_text=True)
        assert resp1.get_json()["job_id"] == job_id_1

        assert job_id_2 == job_id_1, (
            "a press arriving while the same (property, provider) run is "
            "still in flight must join it and get back the SAME job id, so "
            "the client's existing polling works unchanged"
        )
        assert calls == ["claude"], (
            "the provider must be invoked exactly once for two presses "
            "joined into a single run -- a second invocation means the "
            "second press paid for its own, separate execution"
        )

        with app.app_context():
            job = get_job(job_id_1)
        assert job["status"] == "success"
        assert job["result"]["analysis"]["marker"] == "joined"


class TestSingleFlightAllowsFreshRunAfterFinish:
    """Owner's contract: 'A finished analysis NEVER stands in for a new
    press. Once the run is done, pressing Enrich means recompute, honestly
    and from scratch.'
    """

    def test_second_press_after_completion_is_a_real_new_run(
        self, app, client, inline_executor
    ):
        property_id = _make_property(app, "after_finish")
        calls = []

        def _analyze(prop, provider=None):
            calls.append(provider)
            return _success_result(f"pass_{len(calls)}")

        with patch(_ANALYZE, side_effect=_analyze):
            resp1 = client.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "claude"},
            )
            assert resp1.status_code == 202, resp1.get_data(as_text=True)
            job_id_1 = resp1.get_json()["job_id"]

            with app.app_context():
                job1 = get_job(job_id_1)
            assert job1["status"] == "success", (
                "inline_executor runs the job synchronously -- it must "
                "already be terminal by the time the response returns"
            )

            resp2 = client.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "claude"},
            )
            assert resp2.status_code == 202, resp2.get_data(as_text=True)
            job_id_2 = resp2.get_json()["job_id"]

        assert job_id_2 != job_id_1, (
            "a press after the previous run finished must start a NEW job -- "
            "reusing the finished one would be a cache substituting for a "
            "press, exactly what the owner ruled out"
        )
        assert calls == ["claude", "claude"], (
            "the provider must be called again for the second press -- a "
            "finished analysis must never stand in for a new press"
        )
        with app.app_context():
            job2 = get_job(job_id_2)
        assert job2["status"] == "success"
        assert job2["result"]["analysis"]["marker"] == "pass_2"


class TestSingleFlightSkipsADeadJob:
    """Owner's contract: '"In flight" must mean genuinely alive. A job whose
    lease expired ... must NOT capture a new press -- otherwise a dead run
    permanently blocks re-analysis of that listing.'
    """

    def test_expired_lease_does_not_block_a_new_press(
        self, app, client, inline_executor
    ):
        property_id = _make_property(app, "dead_lease")
        key = f"property_ai_analysis:{property_id}:claude"
        stale_id = "d" * 32
        with app.app_context():
            db.session.add(
                BackgroundJob(
                    id=stale_id,
                    job_type="property_ai_analysis",
                    status="running",
                    dedupe_key=key,
                    meta={"property_id": property_id, "provider": "claude"},
                    started_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            )
            db.session.commit()

        calls = []

        def _analyze(prop, provider=None):
            calls.append(provider)
            return _success_result("revived")

        with patch(_ANALYZE, side_effect=_analyze):
            resp = client.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "claude"},
            )

        assert resp.status_code == 202, resp.get_data(as_text=True)
        new_id = resp.get_json()["job_id"]
        assert new_id != stale_id, (
            "a dead row (expired lease) must not be handed back as the "
            "'in-flight' job -- a genuinely new job must be created"
        )
        assert calls == ["claude"], "the provider must run for the new job"

        with app.app_context():
            stale = db.session.get(BackgroundJob, stale_id)
            fresh = get_job(new_id)
        assert stale.status == "interrupted", (
            "the dead row must be reaped, not left claiming to be running forever"
        )
        assert fresh["status"] == "success"


class TestSingleFlightKeepsProvidersIndependent:
    """Owner's contract: 'claude and codex are different providers, so a
    single Enrich click legitimately starts two runs. Only same-provider
    requests join.'

    Sequential presses (claude finishes, *then* openai starts) would pass
    even if the route's dedupe_key dropped the provider entirely -- a
    terminal row never blocks a fresh enqueue regardless of what the key
    contains, so that shape proves nothing about provider isolation
    specifically. This holds claude's press genuinely in flight (the real
    `_EXECUTOR` worker thread, blocked mid-call, same technique as
    `TestSingleFlightJoinsInFlightRun`) and fires openai *while it is still
    running* -- the one moment a shared key could actually cause a join.
    """

    def test_claude_and_openai_do_not_join(self, app, inline_executor):
        property_id = _make_property(app, "providers")
        claude_key = f"property_ai_analysis:{property_id}:claude"

        started = threading.Event()
        release = threading.Event()
        calls = []
        result_holder = {}

        def _analyze(prop, provider=None):
            calls.append(provider)
            if provider == "claude":
                started.set()
                release.wait(timeout=10)
            return _success_result(provider)

        def _press_claude():
            thread_client = app.test_client()
            result_holder["resp_claude"] = thread_client.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "claude"},
            )

        with patch(_ANALYZE, side_effect=_analyze):
            thread = threading.Thread(target=_press_claude)
            thread.start()

            assert started.wait(timeout=5), (
                "the claude press never reached the (real, unmocked) "
                "background-job path"
            )

            with app.app_context():
                claude_row = BackgroundJob.query.filter_by(dedupe_key=claude_key).one()
                assert claude_row.status == "running"

            client2 = app.test_client()
            resp_openai = client2.post(
                f"/api/property/{property_id}/analyze/structured",
                json={"provider": "openai"},
            )

            release.set()
            thread.join(timeout=10)
            assert not thread.is_alive(), "the claude press never finished"

        assert resp_openai.status_code == 202, resp_openai.get_data(as_text=True)
        job_openai = resp_openai.get_json()["job_id"]

        resp_claude = result_holder["resp_claude"]
        assert resp_claude.status_code == 202, resp_claude.get_data(as_text=True)
        job_claude = resp_claude.get_json()["job_id"]

        assert job_claude != job_openai, (
            "different providers for the same listing must never join each "
            "other, even while one is genuinely still running -- one Enrich "
            "click legitimately starts two runs"
        )
        assert sorted(calls) == ["claude", "openai"], (
            "both providers must actually run, not just one of them"
        )

        with app.app_context():
            claude_job = get_job(job_claude)
            openai_job = get_job(job_openai)
        assert claude_job["status"] == "success"
        assert openai_job["status"] == "success"
