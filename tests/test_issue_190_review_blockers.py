"""Regression tests for the independent Tier-2 review of PR #190 (issue #176,
persisting background jobs), which returned BLOCKER with three High findings.
Blocker 1 (a broken terminal status write could strand a job `running`
forever and hand it back to dedupe) is covered in
`tests/test_issue_176_persist_jobs.py`, alongside the rest of that module's
tests. These two are the other findings:

Blocker 2 -- `routes/api_routes.py`'s `analyze_property_structured` (the
land/Claude AI-analysis endpoint) captured the `land` ORM object loaded in
the *request's* session and mutated it from inside the async job's own
closure, which the ThreadPoolExecutor runs on a different thread with its
own Flask-SQLAlchemy scoped session (Flask-SQLAlchemy scopes by app-context
identity, `flask_sqlalchemy.session._app_ctx_id`, and a new OS thread always
gets a new one). Committing through that session did not flush a mutation
made on an object that belonged to a different one -- `land.ai_analysis` was
silently never persisted. The fix captures only `land_id` and reloads the
row inside the worker, the same pattern `analyze_universal_property_structured`
and `generate_openai_structured` already used.

Proving this one is more delicate than it looks: this suite's `app` fixture
keeps one app context open for the *whole test* (`with flask_app.app_context():
... yield flask_app`), and every in-memory SQLite engine here uses a single
shared `StaticPool` connection (app.py's own comment on `_is_in_memory_sqlite`
says as much). If the test never closes the request's own session, that
session's dirty, uncommitted `land` object sits there until the *next* query
on it -- and polling `/api/jobs/<id>` is exactly such a query, which triggers
SQLAlchemy's autoflush and writes `land`'s mutation after all, on a
connection every other reader shares, making the pre-fix bug look fixed. Real
PostgreSQL and a real request lifecycle would tear the request's session down
long before an async job's own poll requests arrive; `_poll_job` reproduces
that by calling `db.session.remove()` before it starts polling. This was
caught and fixed for this PR only after these tests initially passed against
the *reverted*, pre-fix code -- see the git history/PR description for how
that was found.

Blocker 3 -- `PropertyAiAnalysisVariant`'s (property_id, provider) pair was
protected by a plain (non-unique) index, and the writer in
`analyze_universal_property_structured` was query-then-insert: find a row,
update it if found, else insert. An interrupted job's async retry racing a
`?sync=1` request -- which bypasses background_jobs' dedupe_key entirely,
since it never goes through enqueue_job -- could both see "no row" and both
insert, leaving two variants racing for the same pair. Migration 017 adds a
real UNIQUE constraint (after deduplicating any rows that race already
produced), and `routes.api_routes._upsert_property_ai_variant` replaces the
query-then-insert with an update-or-insert that recovers from losing the
race instead of assuming it never happens.
"""

import json
import time

import pytest
from sqlalchemy import text

from app import create_app, db
from models import Land, Property, PropertyAiAnalysisVariant
from tests import setup_test_environment


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


def _poll_job(client, job_id, timeout=10.0):
    """Poll /api/jobs/<id> through the real HTTP surface until terminal.

    Closes the *current* scoped session first -- see the module docstring's
    note on why leaving it open lets a later autoflush paper over blocker 2's
    bug instead of catching it.
    """
    db.session.remove()
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        if resp.status_code == 200:
            job = resp.get_json()["job"]
            if job["status"] not in ("queued", "running"):
                return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal status: {job}")


def _read_land_ai_analysis_raw(land_id):
    """Read `lands.ai_analysis` via a plain SQL SELECT, deliberately not
    through any ORM session.

    The `app` fixture keeps an app context open for the whole test
    (`with flask_app.app_context(): ... yield flask_app``), and Flask reuses
    the current app context for a same-thread request rather than pushing a
    new one -- so `db.session.get(Land, land_id)` from the test body can
    return the *same* session (and the same in-memory `Land` object,
    identity-map hit) that `db.get_or_404` used inside the request handler.
    Reading that way would pass even on the pre-fix code: the object's
    Python attribute really was mutated, just never flushed anywhere, and an
    identity-map hit would hand back that stale-but-mutated object without
    issuing SQL at all. A raw SELECT through a fresh connection has no
    identity map to hit and is the only way to prove the value is actually
    in the database.
    """
    with db.engine.connect() as connection:
        raw = connection.execute(
            text("SELECT ai_analysis FROM lands WHERE id = :id"), {"id": land_id}
        ).scalar_one()
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


# --- Blocker 2 -------------------------------------------------------------


def test_land_claude_worker_persists_ai_analysis_readable_by_a_fresh_session(
    app, client, monkeypatch
):
    """A worker that received only land_id must persist ai_analysis where a
    brand-new session (a different request, a different process) reads it.

    Forces the real async path -- `_should_run_sync` is patched False, so
    this exercises the actual ThreadPoolExecutor + Flask-SQLAlchemy scoped
    session boundary the bug lived in, not an inlined stand-in for it.
    """
    with app.app_context():
        land = Land(source_email_id="blocker2-1", title="Land for worker test")
        db.session.add(land)
        db.session.commit()
        land_id = land.id

    monkeypatch.setattr("routes.api_routes._should_run_sync", lambda *a, **kw: False)

    canned_result = {
        "status": "success",
        "structured_analysis": {"price_analysis": {"verdict": "FAIR"}},
        "model": "claude-test-model",
    }

    class _StubAnthropicService:
        def analyze_property_structured(self, property_data):
            assert property_data["id"] == land_id
            return canned_result

    monkeypatch.setattr(
        "services.anthropic_service.get_anthropic_service",
        lambda: _StubAnthropicService(),
    )

    resp = client.post(f"/api/analyze/property/{land_id}/structured", json={})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = _poll_job(client, job_id)
    assert job["status"] == "success", job
    assert job["result"]["success"] is True
    assert job["result"]["analysis"] == {"price_analysis": {"verdict": "FAIR"}}

    # The proof: a plain SQL SELECT, bypassing every ORM session, reads what
    # is actually in the database -- not whatever a session's identity map
    # still happens to hand back for an object mutated only in Python memory
    # (see _read_land_ai_analysis_raw's docstring for why that distinction
    # matters here).
    assert _read_land_ai_analysis_raw(land_id) == {
        "price_analysis": {"verdict": "FAIR"}
    }, (
        "the worker's mutation of the request-session's Land object was "
        "never flushed by the worker thread's own session commit"
    )


def test_land_claude_worker_merges_into_a_fresh_session_on_enrichment(
    app, client, monkeypatch
):
    """Same bug, the enrichment (existing_analysis merge) branch."""
    with app.app_context():
        land = Land(
            source_email_id="blocker2-2",
            title="Land for enrichment worker test",
            ai_analysis={"price_analysis": {"verdict": "STALE"}},
        )
        db.session.add(land)
        db.session.commit()
        land_id = land.id

    monkeypatch.setattr("routes.api_routes._should_run_sync", lambda *a, **kw: False)

    canned_result = {
        "status": "success",
        "structured_analysis": {"investment_potential": {"rating": "HIGH"}},
        "model": "claude-test-model",
    }
    monkeypatch.setattr(
        "services.anthropic_service.get_anthropic_service",
        lambda: type(
            "Stub",
            (),
            {"analyze_property_structured": lambda self, data: canned_result},
        )(),
    )

    resp = client.post(
        f"/api/analyze/property/{land_id}/structured",
        json={"existing_analysis": {"price_analysis": {"verdict": "STALE"}}},
    )
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    job = _poll_job(client, job_id)
    assert job["status"] == "success", job

    assert _read_land_ai_analysis_raw(land_id) == {
        "price_analysis": {"verdict": "STALE"},
        "investment_potential": {"rating": "HIGH"},
    }


# --- Blocker 3 ---------------------------------------------------------


def test_property_ai_variant_unique_constraint_blocks_a_direct_duplicate(app):
    """The model's UniqueConstraint (matching migration 017) must be real on
    whatever backend actually enforces it -- SQLite here, PostgreSQL in
    tests/test_postgres_migrations.py."""
    with app.app_context():
        prop = Property(source_email_id="blocker3-direct", title="P")
        db.session.add(prop)
        db.session.commit()

        db.session.add(
            PropertyAiAnalysisVariant(
                property_id=prop.id, provider="claude", model="m1", analysis={}
            )
        )
        db.session.commit()

        db.session.add(
            PropertyAiAnalysisVariant(
                property_id=prop.id, provider="claude", model="m2", analysis={}
            )
        )
        with pytest.raises(Exception):  # IntegrityError, wrapped by the dialect
            db.session.commit()
        db.session.rollback()


def test_upsert_property_ai_variant_updates_the_existing_row_in_place(app):
    from routes.api_routes import _upsert_property_ai_variant

    with app.app_context():
        prop = Property(source_email_id="blocker3-update", title="P")
        db.session.add(prop)
        db.session.commit()
        property_id = prop.id

        _upsert_property_ai_variant(
            property_id, "claude", model="m1", analysis={"a": 1}
        )
        _upsert_property_ai_variant(
            property_id, "claude", model="m2", analysis={"a": 2}
        )

        rows = PropertyAiAnalysisVariant.query.filter_by(
            property_id=property_id, provider="claude"
        ).all()

    assert len(rows) == 1
    assert rows[0].model == "m2"
    assert rows[0].analysis == {"a": 2}


def test_upsert_property_ai_variant_recovers_from_a_lost_insert_race(app):
    """Reproduces the exact interleaving blocker 3 describes: this call's own
    "does a row exist" check finds nothing, but by the time it tries to
    INSERT, a concurrent writer has already filled that (property_id,
    provider) slot. The old query-then-insert code had no way to notice;
    _upsert_property_ai_variant must recover by updating the winner instead
    of raising, silently dropping this run's result, or leaving two rows.
    """
    from routes.api_routes import _upsert_property_ai_variant

    with app.app_context():
        prop = Property(source_email_id="blocker3-race", title="P")
        db.session.add(prop)
        db.session.commit()
        property_id = prop.id

        real_query = db.session.query
        call_count = {"n": 0}

        class _ZeroUpdateQuery:
            """Reports 0 rows matched for exactly the first
            PropertyAiAnalysisVariant lookup -- as if this call's own
            "does a pair already exist" UPDATE genuinely found nothing,
            moments before a concurrent writer's INSERT filled the slot
            this call is about to insert into."""

            def __init__(self, real):
                self._real = real

            def filter_by(self, **kwargs):
                self._real = self._real.filter_by(**kwargs)
                return self

            def update(self, *args, **kwargs):
                return 0

        def _query(model, *args, **kwargs):
            real = real_query(model, *args, **kwargs)
            if model is PropertyAiAnalysisVariant and call_count["n"] == 0:
                call_count["n"] += 1
                return _ZeroUpdateQuery(real)
            return real

        db.session.query = _query
        try:
            # The concurrent writer that wins the race, committed for real
            # under the hood while this call still believes nothing exists.
            db.session.add(
                PropertyAiAnalysisVariant(
                    property_id=property_id,
                    provider="claude",
                    model="winner",
                    analysis={"winner": True},
                )
            )
            db.session.commit()

            _upsert_property_ai_variant(
                property_id, "claude", model="mine", analysis={"mine": True}
            )
        finally:
            db.session.query = real_query

        rows = PropertyAiAnalysisVariant.query.filter_by(
            property_id=property_id, provider="claude"
        ).all()

    assert len(rows) == 1, "the race must not leave two variants for the same pair"
    assert rows[0].analysis == {"mine": True}, (
        "losing the insert race must update the winner with this run's "
        "result, not silently drop it"
    )
    assert call_count["n"] == 1, "the patched zero-update must have fired exactly once"
