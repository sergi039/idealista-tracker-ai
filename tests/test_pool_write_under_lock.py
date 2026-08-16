"""The pool write consults the stored row, not the copy its session loaded.

`Property.enrichment` is one JSON column, so writing it is a read-modify-write
over everything in it, and `PoolService._compute` spends seconds on Overpass,
Places and Distance Matrix before that read happens. The "a refusal never
overwrites an answer" guard compared `previous` against the copy **this
session** loaded, which is correct within one process and blind to anything
another process commits inside that window.

Measured on the mini, 2026-08-16 (#339): two runs of `utils.backfill_pool`
overlapped on properties 399 and 400, both writing `enrichment`.

    supervisor  07:02:49  399  unavailable (0 candidates)
    other run   07:03:52  399  ok          (3 candidates)
    other run   07:04:08  400  ok          (3 candidates)
    supervisor  07:04:17  400  unavailable (0 candidates)

Both rows ended up holding `unavailable` with no candidates: two good
measurements replaced by refusals, and three rows billed to Google twice.
Nothing false was written -- `unavailable` means "not measured", it is
retryable and the rows stayed in scope -- so the cost was money and lost work.

`enrich(commit=True)` now re-reads the row under `FOR UPDATE` *after* the
measurement and owns the transaction outright, the shape
`sea_view_service.apply_to_property` settled for this same column in #196.

**What these tests do and do not prove.** The suite runs on one in-memory
SQLite connection, so a genuine two-process race cannot be staged here and
none of this claims to reproduce one; SQLite ignores `FOR UPDATE` entirely.
What is provable, and is what actually failed, is the read: the row is changed
underneath the session and the guard must see the change. A test that only
asserted `refresh` was called with `with_for_update=True` would pass against a
version that then ignored the result.
"""

import json

import pytest
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.pool_service import PoolService  # noqa: E402


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


class _RefusingEnrichment:
    """Overpass refuses: the shape that produced `unavailable` on the mini."""

    def fetch_osm_pools(self, *_args, **_kwargs):
        return {"elements": [], "failure": "overpass_query_error"}

    def __getattr__(self, _name):
        def _refuse(*_args, **_kwargs):
            return {"elements": [], "failure": "overpass_query_error"}

        return _refuse


class _NoTravel:
    def __getattr__(self, _name):
        def _nothing(*_args, **_kwargs):
            return None

        return _nothing


def _refusing_service():
    return PoolService(
        enrichment_service=_RefusingEnrichment(), travel_service=_NoTravel()
    )


def _prop(**overrides):
    fields = {
        "source_email_id": "pool-lock-fixture",
        "title": "PoolLockFixture",
        "municipality": "Cudillero",
        "location_lat": 43.5,
        "location_lon": -6.2,
        "location_accuracy": "approximate",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def _store_measured_pool_behind_the_session(prop):
    """Commit a measured pool straight to the row, as another process would.

    `session.execute` does not expire the identity map, so the ORM object goes
    on returning the `enrichment` it loaded earlier -- which is exactly the
    stale copy the old code compared against.
    """
    measured = {
        "pool": {
            "status": "ok",
            "candidates": [{"name": "Piscina Municipal", "drive_min": 12}],
            "updated_at": "2026-08-16T07:03:52+00:00",
        }
    }
    db.session.execute(
        text("UPDATE properties SET enrichment = :e WHERE id = :i"),
        {"e": json.dumps(measured), "i": prop.id},
    )
    return measured


class TestARefusalDoesNotOverwriteAMeasurementItNeverSaw:
    def test_the_stored_measurement_survives(self, app):
        """The row changed during `_compute`; the refusal must yield to it."""
        prop = _prop()
        assert (prop.enrichment or {}).get("pool") is None
        _store_measured_pool_behind_the_session(prop)
        # The session still believes there is no pool block.
        assert (prop.enrichment or {}).get("pool") is None

        part = _refusing_service().enrich(prop, commit=True)

        assert part["status"] == "ok", (
            "a refusal overwrote a measurement committed by another writer"
        )
        assert part["candidates"], "the measured candidates were lost"
        assert part["last_attempt_status"] == "unavailable", (
            "the refused attempt must still be recorded"
        )

    def test_it_is_persisted_that_way(self, app):
        prop = _prop()
        _store_measured_pool_behind_the_session(prop)

        _refusing_service().enrich(prop, commit=True)

        db.session.expire_all()
        stored = (db.session.get(Property, prop.id).enrichment or {})["pool"]
        assert stored["status"] == "ok"
        assert stored["candidates"]


class TestTheLockIsTakenAndReleased:
    def test_the_row_is_read_for_update(self, app):
        prop = _prop()
        seen = {}
        original = db.session.refresh

        def _record(instance, *args, **kwargs):
            seen["with_for_update"] = kwargs.get("with_for_update")
            return original(instance, *args, **kwargs)

        db.session.refresh = _record
        try:
            _refusing_service().enrich(prop, commit=True)
        finally:
            db.session.refresh = original

        assert seen.get("with_for_update") is True

    def test_commit_false_takes_no_lock(self, app):
        """The documented contract: that mode makes no concurrency promise.

        Holding a lock for an interval the method cannot see the end of is
        worse than the race it closes -- #196's reasoning, and why
        `enrich_property` still uses this mode.
        """
        prop = _prop()
        calls = []
        original = db.session.refresh

        def _record(instance, *args, **kwargs):
            calls.append(kwargs.get("with_for_update"))
            return original(instance, *args, **kwargs)

        db.session.refresh = _record
        try:
            _refusing_service().enrich(prop, commit=False)
        finally:
            db.session.refresh = original

        assert calls == []

    def test_a_failure_after_the_lock_ends_the_transaction(self, app):
        """A row locked and then abandoned would stall every writer behind it.

        The failure has to be staged *after* the `FOR UPDATE`, which is why
        this breaks the commit rather than `_compute`: a `_compute` that raises
        never reaches the lock, so using it here would exercise nothing.
        """
        prop = _prop()
        rollbacks = []
        original_rollback = db.session.rollback
        original_commit = db.session.commit

        def _boom():
            raise RuntimeError("commit blew up")

        db.session.rollback = lambda: rollbacks.append(1) or original_rollback()
        db.session.commit = _boom
        try:
            with pytest.raises(RuntimeError, match="commit blew up"):
                _refusing_service().enrich(prop, commit=True)
        finally:
            db.session.commit = original_commit
            db.session.rollback = original_rollback

        assert rollbacks, "the held FOR UPDATE was never released"
        assert db.session.get(Property, prop.id) is not None


class TestTheCommitModeRefusesWhatItCannotHonour:
    def test_a_dirty_session_is_refused(self, app):
        """The locked refresh autoflushes, which would write out the pending
        change and could erase the very block the locked read inspects."""
        prop = _prop()
        other = _prop(source_email_id="pool-lock-other", title="pending-change")
        other.title = "changed but not committed"

        with pytest.raises(RuntimeError, match="nothing pending"):
            _refusing_service().enrich(prop, commit=True)

    def test_a_property_this_session_does_not_hold_is_refused(self, app):
        prop = _prop()
        db.session.expunge(prop)

        with pytest.raises(RuntimeError, match="does not hold"):
            _refusing_service().enrich(prop, commit=True)

    def test_the_refusal_comes_before_the_money_is_spent(self, app):
        """A caller error must cost a raise, not a round of paid lookups.

        `_compute` is Overpass plus up to three Distance Matrix elements plus,
        on the empty path, a Places Text Search. Validating the session after
        that would bill the owner for a call that was never going to persist.
        """
        prop = _prop()
        db.session.expunge(prop)
        service = _refusing_service()
        computed = []
        service._compute = lambda _p: computed.append(1) or {"status": "unavailable"}

        with pytest.raises(RuntimeError, match="does not hold"):
            service.enrich(prop, commit=True)

        assert computed == [], "the measurement ran before the caller check"
