"""`enrichment.pool` now records the coordinate it was measured from (#346).

Every other enrichment block already carried this provenance --
`travel.origin`, `enrichment.environment.sea_view_detail.origin`,
`enrichment.sea.origin` -- and the pool block was the one exception, which
cost a fifteen-row hand audit to answer a question the others answer with one
query (issue #346).

The precedent this copies, not redesigns: `services/sea_view_service.py`
already solved this for the same column. `services/enrichment_origin.py` now
holds the two primitives it used -- `origin_of()` captures `{lat, lon}` at
computation time, `origins_agree()` compares at a ~1 m tolerance and returns
`None` ("proves nothing either way") whenever either side is unreadable --
and `pool_service.py` reuses them rather than growing a second copy.

The rule this file exists to pin: a refusal must never stamp today's
coordinate onto a measurement it did not make. And a block measured before
this ticket -- the ~200 legacy rows with no `origin` at all -- must read as
`unknown`, never as `matches`: assuming a pre-origin block belongs to today's
coordinate is exactly the provenance nobody can claim.
"""

from types import SimpleNamespace

import pytest

from app import create_app, db
from models import Property
from services.enrichment_origin import origin_of, origins_agree
from services.pool_service import PoolService, pool_origin_state
from tests import setup_test_environment

SPORTS_CENTRE = {
    "type": "way",
    "center": {"lat": 43.53, "lon": -7.05},
    "tags": {
        "leisure": "sports_centre",
        "sport": "swimming",
        "name": "Piscina Municipal de Ribadeo",
        "covered": "yes",
    },
}


class _FakeEnrichment:
    def __init__(self, elements=None, failure=None):
        self.elements = elements
        self.failure = failure

    def _overpass_elements(self, query):
        if self.failure is not None:
            return None, SimpleNamespace(reason=self.failure)
        return self.elements, None


class _FakeTravel:
    def __init__(self, minutes=None):
        self.minutes = minutes if minutes is not None else []

    def measure_drive_minutes(self, lat, lon, points):
        padded = (self.minutes + [None] * len(points))[: len(points)]
        return [{"minutes": value, "refused": value is None} for value in padded]

    def _nearest_place_text_search(self, *_args, **_kwargs):
        # No candidate, no refusal -- the cross-check's "empty" outcome, which
        # leaves `unverified_absence` for the empty-OSM tests below.
        return SimpleNamespace(place=None, failure=None)


def _service(elements=None, failure=None, minutes=None):
    return PoolService(
        enrichment_service=_FakeEnrichment(elements=elements, failure=failure),
        travel_service=_FakeTravel(minutes=minutes),
    )


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _prop(**overrides):
    fields = dict(
        source_email_id=f"pool-origin-{overrides.get('title', 'x')}",
        title="PoolOriginFixture",
        municipality="Navia",
        location_lat=43.55,
        location_lon=-6.83,
    )
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestSharedOriginPrimitives:
    """`services/enrichment_origin.py` in isolation, no property involved."""

    def test_origin_of_reads_the_coordinates(self):
        prop = SimpleNamespace(location_lat=43.5, location_lon=-6.8)
        assert origin_of(prop) == {"lat": 43.5, "lon": -6.8}

    def test_origin_of_is_none_without_coordinates(self):
        prop = SimpleNamespace(location_lat=None, location_lon=-6.8)
        assert origin_of(prop) is None

    def test_origins_agree_is_none_when_either_side_is_unreadable(self):
        readable = {"lat": 43.5, "lon": -6.8}
        assert origins_agree(None, readable) is None
        assert origins_agree(readable, None) is None
        assert origins_agree({"lat": "not-a-number", "lon": -6.8}, readable) is None

    def test_origins_agree_true_within_tolerance_false_outside_it(self):
        base = {"lat": 43.5, "lon": -6.8}
        assert origins_agree(base, {"lat": 43.5, "lon": -6.8}) is True
        assert origins_agree(base, {"lat": 43.9, "lon": -6.8}) is False


class TestOriginIsWrittenOnCompute:
    def test_origin_written_on_a_measured_block(self, app):
        prop = _prop(title="measured")
        part = _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        assert part["status"] == "ok"
        assert part["origin"] == {"lat": 43.55, "lon": -6.83}

    def test_a_computed_negative_still_gets_an_origin(self, app):
        """`unverified_absence` is a real answer about this coordinate, not a
        refusal -- it must be stamped exactly as `ok` is."""
        prop = _prop(title="absence")
        part = _service(elements=[]).enrich(prop)
        assert part["status"] == "unverified_absence"
        assert part["origin"] == {"lat": 43.55, "lon": -6.83}

    def test_origin_is_none_without_coordinates(self, app):
        prop = _prop(title="no-coords", location_lat=None, location_lon=None)
        part = _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        assert part["status"] == "no_coordinates"
        assert part["origin"] is None


class TestARefusalPreservesThePreviousOrigin:
    def test_kept_previous_keeps_its_own_origin_not_todays(self, app):
        prop = _prop(title="kept")
        _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        stored_origin = prop.enrichment["pool"]["origin"]
        assert stored_origin == {"lat": 43.55, "lon": -6.83}

        # The property moves, then the next attempt refuses outright.
        prop.location_lat, prop.location_lon = 43.9, -7.5
        db.session.commit()
        part = _service(failure="overpass_query_error").enrich(prop)

        assert part["status"] == "ok", "the refusal must not drop the measurement"
        assert part["origin"] == stored_origin, (
            "a refused attempt described nothing and must not stamp today's "
            "moved coordinate onto a kept measurement"
        )

    def test_a_legacy_block_with_no_origin_stays_originless_when_kept(self, app):
        prop = _prop(
            title="legacy-kept",
            enrichment={"pool": {"status": "ok", "candidates": [{"name": "P"}]}},
        )
        part = _service(failure="overpass_query_error").enrich(prop)
        assert part["status"] == "ok"
        assert "origin" not in part, (
            "a kept legacy block must not gain an origin it was never measured with"
        )


class TestPoolOriginState:
    def test_matches_when_the_property_has_not_moved(self, app):
        prop = _prop(title="matches")
        _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        assert pool_origin_state(prop) == "matches"

    def test_differs_when_the_coordinate_has_moved_since(self, app):
        prop = _prop(title="differs")
        _service(elements=[SPORTS_CENTRE], minutes=[12]).enrich(prop)
        prop.location_lat, prop.location_lon = 43.9, -7.5
        db.session.commit()
        assert pool_origin_state(prop) == "differs"

    def test_unknown_when_there_is_no_pool_block_at_all(self, app):
        prop = _prop(title="no-block")
        assert pool_origin_state(prop) == "unknown"

    def test_unknown_for_a_legacy_block_with_no_origin(self, app):
        """The ~200 blocks measured before #346. Reading these as 'matches'
        would invent provenance the block was never stamped with."""
        prop = _prop(
            title="legacy",
            enrichment={"pool": {"status": "ok", "candidates": [{"name": "P"}]}},
        )
        assert pool_origin_state(prop) == "unknown"

    def test_unknown_when_the_property_has_no_coordinates(self, app):
        prop = _prop(
            title="no-coords-state",
            location_lat=None,
            location_lon=None,
            enrichment={
                "pool": {"status": "ok", "origin": {"lat": 43.55, "lon": -6.83}}
            },
        )
        assert pool_origin_state(prop) == "unknown"
