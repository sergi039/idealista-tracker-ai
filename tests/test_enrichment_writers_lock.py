"""Every writer of an `enrichment` block reads the stored row, not its copy.

`Property.enrichment` is one JSON column, so each writer of a block inside it
does a read-modify-write over the whole column after spending seconds on
external calls. A guard comparing against `prop.enrichment` compares against
the copy *its own session* loaded, which is correct within one process and
blind to what another commits meanwhile.

#339 measured that: two `utils.backfill_pool` runs overlapped and replaced two
measured pools with refusals. #344 locked the pool writer. #352 recorded that
`sea_distance_service` and `quality_of_life_service` still took no lock, and
that this is not "two more of the same" -- **a row lock is mutual exclusion
only among writers that take it.** One locked writer among three unlocked ones
can make things worse: the unlocked `UPDATE` blocks on the lock, then writes
the copy it read *before* it.

So this file covers all three through the one primitive they now share,
`services/enrichment_write.py`. The per-writer specifics stay in
`tests/test_pool_write_under_lock.py`, `tests/test_sea_distance_scoring.py`
and `tests/test_quality_of_life.py`.

**What is not proved here**: the suite runs on one in-memory SQLite
connection, so no two-process race is staged and SQLite ignores `FOR UPDATE`
outright. What is proved is the half that actually failed -- the stored row is
changed underneath the session and the writer has to see it.
"""

import json

import pytest
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.pool_service import PoolService  # noqa: E402
from services.quality_of_life_service import QualityOfLifeService  # noqa: E402
from services.sea_distance_service import SeaDistanceService  # noqa: E402


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


def _prop(source_email_id="enrich-lock-fixture", **overrides):
    fields = {
        "source_email_id": source_email_id,
        "title": "EnrichLockFixture",
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


def _store_behind_the_session(prop, block, payload):
    """Commit a block straight to the row, as another process would.

    `session.execute` does not expire the identity map, so the ORM object goes
    on returning the `enrichment` it loaded earlier -- the stale copy every one
    of these writers used to compare against.
    """
    db.session.execute(
        text("UPDATE properties SET enrichment = :e WHERE id = :i"),
        {"e": json.dumps({block: payload}), "i": prop.id},
    )


class _RefusingOverpass:
    """Every lookup refuses: the shape that produced the lost writes."""

    def __getattr__(self, _name):
        def _refuse(*_args, **_kwargs):
            return {"elements": [], "failure": "overpass_query_error"}

        return _refuse


class _NoTravel:
    def __getattr__(self, _name):
        def _nothing(*_args, **_kwargs):
            return None

        return _nothing


def _pool_writer():
    service = PoolService(
        enrichment_service=_RefusingOverpass(), travel_service=_NoTravel()
    )
    return lambda prop, commit: service.enrich(prop, commit=commit)


def _sea_writer(monkeypatch):
    from services import sea_distance_service as module

    def _refuse(*_args, **_kwargs):
        raise module.SeaViewSourceError("Overpass returned HTTP 504")

    monkeypatch.setattr(module, "fetch_coastline_points", _refuse)
    service = SeaDistanceService()
    return lambda prop, commit: service.update_property(prop, commit=commit)


def _qol_writer(monkeypatch):
    service = QualityOfLifeService(enrichment_service=_RefusingOverpass())

    def _boom(*_args, **_kwargs):
        raise RuntimeError("reference data unavailable")

    monkeypatch.setattr(service, "municipality_context", _boom)
    monkeypatch.setattr(service, "supermarket_reach", _boom)
    monkeypatch.setattr(service, "hospitals", _boom)
    return lambda prop, commit: service.enrich(prop, commit=commit)


class TestARefusalYieldsToAMeasurementItNeverSaw:
    """The defect, one case per writer.

    Each stores a *measured* block behind the session's back, then runs the
    writer with every source refusing. The refusal must lose.
    """

    def test_pool(self, app):
        prop = _prop()
        _store_behind_the_session(
            prop,
            "pool",
            {"status": "ok", "candidates": [{"name": "Piscina", "drive_min": 12}]},
        )

        part = _pool_writer()(prop, True)

        assert part["status"] == "ok", "a refusal overwrote another writer's pool"
        assert part["candidates"]

    def test_sea_distance(self, app, monkeypatch):
        prop = _prop()
        _store_behind_the_session(
            prop,
            "sea",
            {
                "status": "ok",
                "distance_m": 812.5,
                "searched_m": 20000,
                "source": "osm_coastline",
                "origin": {"lat": 43.5, "lon": -6.2},
                "updated_at": "2026-08-16T09:00:00+00:00",
            },
        )

        result = _sea_writer(monkeypatch)(prop, True)

        assert result["status"] == "ok", "a refusal overwrote a stored distance"
        assert result["distance_m"] == 812.5
        assert result["last_attempt_status"] == "unavailable"

    def test_quality_of_life(self, app, monkeypatch):
        prop = _prop()
        _store_behind_the_session(
            prop,
            "quality_of_life",
            {"hospitals": {"status": "ok", "nearest_km": 12.3}},
        )

        payload = _qol_writer(monkeypatch)(prop, True)

        assert payload["hospitals"]["status"] == "ok", (
            "a refusal overwrote another writer's QoL part"
        )
        assert payload["hospitals"]["last_attempt_status"] == "unavailable"


class TestTheOtherBlocksSurviveEachWrite:
    """The column is shared: writing one block must not drop the neighbours.

    This is the loss that has no symptom -- the page shows the block that was
    written and simply stops showing the one that vanished.
    """

    def test_a_pool_write_keeps_a_concurrently_stored_sea_block(self, app):
        prop = _prop()
        db.session.execute(
            text("UPDATE properties SET enrichment = :e WHERE id = :i"),
            {
                "e": json.dumps({"sea": {"status": "ok", "distance_m": 500.0}}),
                "i": prop.id,
            },
        )

        _pool_writer()(prop, True)

        db.session.expire_all()
        stored = db.session.get(Property, prop.id).enrichment
        assert stored["sea"]["distance_m"] == 500.0, (
            "the pool write dropped a block another writer had stored"
        )
        assert "pool" in stored

    def test_a_sea_write_keeps_a_concurrently_stored_pool_block(self, app, monkeypatch):
        prop = _prop()
        db.session.execute(
            text("UPDATE properties SET enrichment = :e WHERE id = :i"),
            {
                "e": json.dumps({"pool": {"status": "ok", "candidates": [{"x": 1}]}}),
                "i": prop.id,
            },
        )

        _sea_writer(monkeypatch)(prop, True)

        db.session.expire_all()
        stored = db.session.get(Property, prop.id).enrichment
        assert stored["pool"]["status"] == "ok", (
            "the sea write dropped a block another writer had stored"
        )
        assert "sea" in stored


class TestTheContractIsTheSameForAllThree:
    def _writers(self, monkeypatch):
        return {
            "pool": _pool_writer(),
            "sea": _sea_writer(monkeypatch),
            "qol": _qol_writer(monkeypatch),
        }

    @pytest.mark.parametrize("name", ["pool", "sea", "qol"])
    def test_the_row_is_read_for_update(self, app, monkeypatch, name):
        prop = _prop()
        writer = self._writers(monkeypatch)[name]
        seen = []
        original = db.session.refresh

        def _record(instance, *args, **kwargs):
            seen.append(kwargs.get("with_for_update"))
            return original(instance, *args, **kwargs)

        db.session.refresh = _record
        try:
            writer(prop, True)
        finally:
            db.session.refresh = original

        assert seen == [True], f"{name} did not take the lock exactly once"

    @pytest.mark.parametrize("name", ["pool", "sea", "qol"])
    def test_commit_false_takes_no_lock(self, app, monkeypatch, name):
        """That mode makes no concurrency promise -- #196's reasoning."""
        prop = _prop()
        writer = self._writers(monkeypatch)[name]
        seen = []
        original = db.session.refresh

        def _record(instance, *args, **kwargs):
            seen.append(kwargs.get("with_for_update"))
            return original(instance, *args, **kwargs)

        db.session.refresh = _record
        try:
            writer(prop, False)
        finally:
            db.session.refresh = original

        assert seen == []

    @pytest.mark.parametrize("name", ["pool", "sea", "qol"])
    def test_a_dirty_session_is_refused(self, app, monkeypatch, name):
        prop = _prop()
        other = _prop(source_email_id=f"dirty-{name}")
        other.title = "changed but not committed"
        writer = self._writers(monkeypatch)[name]

        with pytest.raises(RuntimeError, match="nothing pending"):
            writer(prop, True)

    @pytest.mark.parametrize("name", ["pool", "sea", "qol"])
    def test_a_property_this_session_does_not_hold_is_refused(
        self, app, monkeypatch, name
    ):
        prop = _prop()
        db.session.expunge(prop)
        writer = self._writers(monkeypatch)[name]

        with pytest.raises(RuntimeError, match="does not hold"):
            writer(prop, True)
