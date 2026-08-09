"""Distance to the sea: measurement, failure contract and its scoring weight.

The through-line of this module is issue #98: an API refusal must never be
recorded, scored or displayed as "there is nothing nearby". Every failure mode
Overpass can produce is checked to land on `unavailable`, and only a real answer
is allowed to produce a zero.
"""

import json
import math
from decimal import Decimal

import pytest
import requests

from app import create_app, db
from models import Property, SearchProfile
from services import sea_distance_service as sds
from services.property_scoring_service import (
    HousingPropertyScorer,
    PropertyScoringService,
    _resolve_sea_distance_config,
    _sea_distance_score,
)
from services.sea_distance_service import (
    STATUS_NO_COASTLINE,
    STATUS_NO_COORDINATES,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    SeaDistanceService,
)
from tests import setup_test_environment
from utils.cache import cache
from utils.http import request_with_retries

# A short north-south coastline segment off the Asturian coast, used as the
# stand-in shoreline for the geometry tests.
COAST_LON = -5.85
COAST_LAT_SOUTH = 43.60
COAST_LAT_NORTH = 43.62


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        db.create_all()
        # The simple in-memory cache outlives a single test; a stale coastline
        # entry would silently satisfy a lookup another test expects to fail.
        cache.clear()
        yield app
        db.drop_all()


class FakeResponse:
    """Minimal stand-in for a streamed `requests` response."""

    def __init__(self, status_code=200, payload=None, body=None, chunks=None):
        self.status_code = status_code
        if chunks is not None:
            self._chunks = list(chunks)
        elif body is not None:
            self._chunks = [body]
        else:
            self._chunks = [json.dumps(payload or {}).encode("utf-8")]
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


def _way(points):
    return {
        "type": "way",
        "id": 1,
        "geometry": [{"lat": lat, "lon": lon} for lat, lon in points],
    }


def _coastline_payload():
    return {
        "elements": [_way([(COAST_LAT_SOUTH, COAST_LON), (COAST_LAT_NORTH, COAST_LON)])]
    }


def _patch_overpass(monkeypatch, response_factory):
    """Route every Overpass POST to `response_factory`, counting the calls."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        result = response_factory()
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def _service():
    service = SeaDistanceService()
    # Never sleep inside the suite.
    service.throttle_range_seconds = (0.0, 0.0)
    return service


def _property(**kwargs):
    defaults = {
        "source_email_id": "sea-test",
        "property_category": "housing",
        "property_subtype": "house",
        "municipality": "Luarca",
        "price": Decimal("200000"),
        "area": Decimal("100"),
        "location_lat": Decimal("43.6100000"),
        "location_lon": Decimal("-5.8600000"),
    }
    defaults.update(kwargs)
    return Property(**defaults)


# -- geometry -----------------------------------------------------------


def test_measures_distance_to_a_known_segment(app, monkeypatch):
    """A point due east of the segment is measured across the longitude gap."""
    _patch_overpass(monkeypatch, lambda: FakeResponse(payload=_coastline_payload()))

    result = _service().measure(43.61, COAST_LON + 0.01)

    # 0.01 degrees of longitude at 43.61 N: 0.01 * pi/180 * R * cos(lat).
    expected_m = math.radians(0.01) * sds.EARTH_RADIUS_M * math.cos(math.radians(43.61))
    assert result["status"] == STATUS_OK
    assert result["distance_m"] == pytest.approx(expected_m, rel=0.01)


def test_distance_grows_with_separation(app, monkeypatch):
    _patch_overpass(monkeypatch, lambda: FakeResponse(payload=_coastline_payload()))
    service = _service()

    near = service.measure(43.61, COAST_LON + 0.01)
    far = service.measure(43.61, COAST_LON + 0.05)

    assert near["distance_m"] < far["distance_m"]


# -- failure contract (issue #98) ---------------------------------------


def test_http_error_is_unavailable_not_missing_coastline(app, monkeypatch):
    _patch_overpass(monkeypatch, lambda: FakeResponse(status_code=500))

    result = _service().measure(43.61, -5.85)

    assert result["status"] == STATUS_UNAVAILABLE
    assert result["distance_m"] is None


def test_network_error_is_unavailable(app, monkeypatch):
    _patch_overpass(monkeypatch, lambda: requests.ConnectionError("no route"))

    assert _service().measure(43.61, -5.85)["status"] == STATUS_UNAVAILABLE


def test_overpass_remark_is_unavailable(app, monkeypatch):
    """Overpass reports its own timeouts in-band, with HTTP 200."""
    _patch_overpass(
        monkeypatch,
        lambda: FakeResponse(
            payload={"remark": "runtime error: Query timed out", "elements": []}
        ),
    )

    assert _service().measure(43.61, -5.85)["status"] == STATUS_UNAVAILABLE


@pytest.mark.parametrize(
    "element",
    [
        {"type": "way", "id": 7},
        {"type": "way", "id": 7, "geometry": []},
        {"type": "way", "id": 7, "geometry": [{"lat": 43.6}]},
        {"type": "way", "id": 7, "geometry": [{"lat": 200.0, "lon": -5.8}]},
    ],
    ids=["absent", "empty", "no-lon", "out-of-range"],
)
def test_unusable_geometry_is_unavailable(app, monkeypatch, element):
    """A partial answer must not shrink the coastline into a measured zero."""
    _patch_overpass(monkeypatch, lambda: FakeResponse(payload={"elements": [element]}))

    assert _service().measure(43.61, -5.85)["status"] == STATUS_UNAVAILABLE


def test_broken_stream_is_unavailable(app, monkeypatch):
    """The body can still fail after the headers looked fine."""

    class BreakingResponse(FakeResponse):
        def iter_content(self, chunk_size=None):
            yield b'{"elements":'
            raise requests.exceptions.ChunkedEncodingError("connection reset")

    _patch_overpass(monkeypatch, lambda: BreakingResponse())

    assert _service().measure(43.61, -5.85)["status"] == STATUS_UNAVAILABLE


def test_unparsable_body_is_unavailable(app, monkeypatch):
    _patch_overpass(monkeypatch, lambda: FakeResponse(body=b"<html>nope</html>"))

    assert _service().measure(43.61, -5.85)["status"] == STATUS_UNAVAILABLE


def test_oversized_body_is_unavailable_and_not_parsed(app, monkeypatch):
    monkeypatch.setattr(sds, "MAX_RESPONSE_BYTES", 64)
    _patch_overpass(monkeypatch, lambda: FakeResponse(chunks=[b"x" * 40, b"y" * 40]))

    assert _service().measure(43.61, -5.85)["status"] == STATUS_UNAVAILABLE


def test_empty_elements_is_a_measured_absence(app, monkeypatch):
    _patch_overpass(monkeypatch, lambda: FakeResponse(payload={"elements": []}))

    result = _service().measure(40.4, -3.7)

    assert result["status"] == STATUS_NO_COASTLINE
    assert result["distance_m"] is None


def test_failed_lookup_is_not_cached(app, monkeypatch):
    """A refusal must not be remembered as an answer for the whole cell."""
    calls = _patch_overpass(monkeypatch, lambda: FakeResponse(status_code=500))
    service = _service()

    service.measure(43.61, -5.85)
    after_first = len(calls)
    service.measure(43.61, -5.85)

    assert after_first > 0
    # The second lookup went back to the network instead of reading a cached
    # failure, which would have frozen the whole cell as "no coastline".
    assert len(calls) > after_first


# -- caching ------------------------------------------------------------


def test_one_request_serves_every_point_in_the_cell(app, monkeypatch):
    calls = _patch_overpass(
        monkeypatch, lambda: FakeResponse(payload=_coastline_payload())
    )
    service = _service()

    first = service.measure(43.61, -5.86)
    second = service.measure(43.62, -5.84)

    assert len(calls) == 1
    assert first["status"] == STATUS_OK
    assert second["status"] == STATUS_OK


# -- persistence --------------------------------------------------------


def test_measurement_survives_commit(app, monkeypatch):
    """`enrichment` is a plain JSON column, so the write needs flag_modified."""
    _patch_overpass(monkeypatch, lambda: FakeResponse(payload=_coastline_payload()))

    with app.app_context():
        prop = _property()
        db.session.add(prop)
        db.session.commit()

        _service().update_property(prop, commit=True)
        property_id = prop.id
        db.session.expire_all()

        reloaded = db.session.get(Property, property_id)
        assert reloaded.enrichment["sea"]["status"] == STATUS_OK
        assert reloaded.enrichment["sea"]["distance_m"] > 0


def test_missing_coordinates_are_recorded_without_geocoding(app, monkeypatch):
    calls = _patch_overpass(
        monkeypatch, lambda: FakeResponse(payload=_coastline_payload())
    )

    with app.app_context():
        prop = _property(location_lat=None, location_lon=None)
        db.session.add(prop)
        db.session.commit()

        payload = _service().update_property(prop, commit=True)

        assert payload["status"] == STATUS_NO_COORDINATES
        assert calls == []


def test_outage_keeps_the_last_good_measurement(app, monkeypatch):
    with app.app_context():
        prop = _property()
        db.session.add(prop)
        db.session.commit()

        _patch_overpass(monkeypatch, lambda: FakeResponse(payload=_coastline_payload()))
        first = _service().update_property(prop, commit=True)
        assert first["status"] == STATUS_OK

        cache.clear()
        _patch_overpass(monkeypatch, lambda: FakeResponse(status_code=503))
        second = _service().update_property(prop, commit=True)

        assert second["status"] == STATUS_OK
        assert second["distance_m"] == first["distance_m"]
        assert second["last_attempt_status"] == STATUS_UNAVAILABLE


def test_outage_keeps_a_measured_absence_too(app, monkeypatch):
    """A kept zero matters as much as a kept distance: it holds the denominator."""
    with app.app_context():
        prop = _property(
            location_lat=Decimal("40.4000000"), location_lon=Decimal("-3.7000000")
        )
        db.session.add(prop)
        db.session.commit()

        _patch_overpass(monkeypatch, lambda: FakeResponse(payload={"elements": []}))
        _service().update_property(prop, commit=True)

        cache.clear()
        _patch_overpass(monkeypatch, lambda: FakeResponse(status_code=503))
        second = _service().update_property(prop, commit=True)

        assert second["status"] == STATUS_NO_COASTLINE
        assert second["last_attempt_status"] == STATUS_UNAVAILABLE

        score, _meta = HousingPropertyScorer()._sea_score(prop, near_m=300, far_m=10000)
        assert score == 0.0


def test_moved_property_discards_the_old_measurement(app, monkeypatch):
    with app.app_context():
        prop = _property()
        db.session.add(prop)
        db.session.commit()

        _patch_overpass(monkeypatch, lambda: FakeResponse(payload=_coastline_payload()))
        _service().update_property(prop, commit=True)

        # Re-geocoded somewhere else: the stored distance is about another point.
        prop.location_lat = Decimal("40.4000000")
        prop.location_lon = Decimal("-3.7000000")
        cache.clear()
        _patch_overpass(monkeypatch, lambda: FakeResponse(status_code=503))
        result = _service().update_property(prop, commit=True)

        assert result["status"] == STATUS_UNAVAILABLE
        assert result["distance_m"] is None


# -- decay function -----------------------------------------------------


def test_decay_bounds_and_monotonicity():
    assert _sea_distance_score(0, near_m=300, far_m=10000) == 100.0
    assert _sea_distance_score(10000, near_m=300, far_m=10000) == 0.0
    assert _sea_distance_score(20000, near_m=300, far_m=10000) == 0.0
    assert _sea_distance_score(None, near_m=300, far_m=10000) is None

    close = _sea_distance_score(300, near_m=300, far_m=10000)
    mid = _sea_distance_score(1000, near_m=300, far_m=10000)
    far = _sea_distance_score(3000, near_m=300, far_m=10000)
    assert 100 > close > mid > far > 0


def test_a_nan_distance_scores_nothing_rather_than_everything(app):
    """NaN slips through every comparison and used to come out as a full 100."""
    with app.app_context():
        prop = _property(enrichment=_sea_enrichment(STATUS_OK, float("nan")))
        score, meta = HousingPropertyScorer()._sea_score(prop, near_m=300, far_m=10000)

    assert score is None
    assert meta["status"] == "missing_distance"


def test_absence_is_only_scored_within_the_radius_searched(app):
    """A horizon past the search radius asks about ground nobody looked at."""
    with app.app_context():
        prop = _property(enrichment=_sea_enrichment(STATUS_NO_COASTLINE, None))
        scorer = HousingPropertyScorer()

        inside, _ = scorer._sea_score(prop, near_m=300, far_m=10000)
        outside, meta = scorer._sea_score(prop, near_m=300, far_m=90000)

    assert inside == 0.0
    assert outside is None
    assert meta["status"] == "horizon_exceeds_search"


def test_invalid_overrides_fall_back_to_defaults():
    defaults = {"near_m": 300.0, "far_m": 10000.0}

    for bad in (
        {"near_m": 0},
        {"near_m": -5},
        {"far_m": 100},
        {"near_m": "abc"},
        {"near_m": float("inf")},
        {"far_m": float("inf")},
        {"far_m": float("nan")},
        "not-an-object",
    ):
        resolved, error = _resolve_sea_distance_config(bad, defaults)
        assert resolved == defaults
        assert error

    resolved, error = _resolve_sea_distance_config(
        {"near_m": 500, "far_m": 20000}, defaults
    )
    assert resolved == {"near_m": 500.0, "far_m": 20000.0}
    assert error is None


# -- scoring ------------------------------------------------------------


def _profile(name="Sea profile", scoring_config=None):
    presets = {
        key: {"enabled": False, "mode": "driving"}
        for key in (
            "airport",
            "train_station",
            "hospital",
            "police",
            "supermarket",
            "school",
        )
    }
    return SearchProfile(
        name=name,
        is_active=True,
        is_default=True,
        travel_targets={"presets": presets, "custom": []},
        scoring_config=scoring_config,
    )


def _sea_enrichment(status, distance_m, lat=43.61, lon=-5.86):
    return {
        "sea": {
            "status": status,
            "distance_m": distance_m,
            "searched_m": sds.MAX_SEARCH_M,
            "source": "osm_coastline",
            "origin": {"lat": lat, "lon": lon},
        }
    }


def test_closer_to_the_sea_scores_higher(app):
    with app.app_context():
        profile = _profile()
        db.session.add(profile)
        db.session.commit()

        near = _property(
            source_email_id="sea-near",
            search_profile_id=profile.id,
            enrichment=_sea_enrichment(STATUS_OK, 400.0),
        )
        far = _property(
            source_email_id="sea-far",
            search_profile_id=profile.id,
            enrichment=_sea_enrichment(STATUS_OK, 8000.0),
        )
        db.session.add_all([near, far])
        db.session.commit()

        service = PropertyScoringService()
        service.calculate_for_property(near)
        service.calculate_for_property(far)

        assert near.score_lifestyle > far.score_lifestyle
        assert near.scoring["details"]["sea"]["status"] == "ok"


def test_unavailable_measurement_does_not_score_zero(app):
    """Regression on #98: a refusal is dropped from the average, not counted."""
    with app.app_context():
        profile = _profile()
        db.session.add(profile)
        db.session.commit()

        # Peers so the size/value components have something to rank against;
        # without them every component is None and the comparison is vacuous.
        peers = [
            _property(
                source_email_id=f"sea-peer-{index}",
                search_profile_id=profile.id,
                price=Decimal("150000"),
                area=Decimal(str(50 + index * 5)),
            )
            for index in range(4)
        ]
        blind = _property(
            source_email_id="sea-blind",
            search_profile_id=profile.id,
            enrichment=_sea_enrichment(STATUS_UNAVAILABLE, None),
        )
        absent = _property(
            source_email_id="sea-absent",
            search_profile_id=profile.id,
            enrichment=_sea_enrichment(STATUS_NO_COASTLINE, None),
        )
        db.session.add_all([*peers, blind, absent])
        db.session.commit()

        service = PropertyScoringService()
        service.calculate_for_property(blind)
        service.calculate_for_property(absent)

        components = blind.scoring["profiles"]["lifestyle"]["components"]
        assert components["sea_score"] is None
        assert absent.scoring["profiles"]["lifestyle"]["components"]["sea_score"] == 0.0
        # Dropping the component must not drag the score down to the zero case.
        assert blind.score_lifestyle > absent.score_lifestyle


def test_profile_override_changes_the_sea_score(app):
    with app.app_context():
        override = {
            "categories": {"housing": {"sea_distance": {"near_m": 300, "far_m": 1000}}}
        }
        default_profile = _profile(name="Sea default horizon")
        tight_profile = _profile(name="Sea tight horizon", scoring_config=override)
        db.session.add_all([default_profile, tight_profile])
        db.session.commit()

        service = PropertyScoringService()
        scores = []
        for index, profile in enumerate([default_profile, tight_profile]):
            prop = _property(
                source_email_id=f"sea-override-{index}",
                search_profile_id=profile.id,
                enrichment=_sea_enrichment(STATUS_OK, 900.0),
            )
            db.session.add(prop)
            db.session.commit()
            service.calculate_for_property(prop)
            scores.append(
                prop.scoring["profiles"]["lifestyle"]["components"]["sea_score"]
            )

        # 900 m is almost the whole way to a 1 km horizon, but close by a 10 km one.
        assert scores[0] > scores[1]


def test_garage_ignores_the_sea_by_default():
    scorer = HousingPropertyScorer()
    assert scorer.DEFAULT_LIFESTYLE_WEIGHTS["sea_score"] > 0

    from services.property_scoring_service import GaragePropertyScorer

    assert GaragePropertyScorer.DEFAULT_LIFESTYLE_WEIGHTS["sea_score"] == 0.0
    assert GaragePropertyScorer.DEFAULT_INVESTMENT_WEIGHTS["sea_score"] == 0.0


# -- detail page --------------------------------------------------------


@pytest.mark.parametrize(
    "status,distance_m,expected",
    [
        (STATUS_OK, 2500.0, "2.5 km"),
        (STATUS_NO_COASTLINE, None, "No coastline within"),
        (STATUS_UNAVAILABLE, None, "Coastline data unavailable"),
        (None, None, "Not measured yet"),
    ],
)
def test_detail_page_states_the_real_sea_status(app, status, distance_m, expected):
    """An outage must read as an outage on the page, not as "no sea nearby"."""
    with app.app_context():
        enrichment = _sea_enrichment(status, distance_m) if status else {}
        prop = _property(source_email_id=f"sea-page-{status}", enrichment=enrichment)
        db.session.add(prop)
        db.session.commit()
        property_id = prop.id

    response = app.test_client().get(f"/properties/{property_id}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Distance to sea" in body
    assert expected in body


# -- pipeline wiring ----------------------------------------------------


def test_manual_enrichment_measures_before_it_scores(app, monkeypatch):
    """The Enrich flow must feed the sea distance into the same rescore."""
    from services.property_enrichment_service import PropertyEnrichmentService

    order = []

    class StubLocation:
        def ensure_coordinates(self, prop, refresh=False):
            return True

    class StubTravel:
        def calculate_for_property(self, prop, commit=False):
            return True

    class StubScoring:
        def calculate_for_property(self, prop, commit=False):
            order.append(
                ("scoring", (prop.enrichment or {}).get("sea", {}).get("status"))
            )
            return True

    class StubSea:
        def update_property(self, prop, *, commit=False):
            order.append(("sea", commit))
            enrichment = dict(prop.enrichment or {})
            enrichment["sea"] = {"status": STATUS_OK, "distance_m": 700.0}
            prop.enrichment = enrichment
            return enrichment["sea"]

    with app.app_context():
        prop = _property()
        db.session.add(prop)
        db.session.commit()

        PropertyEnrichmentService(
            location_service=StubLocation(),
            travel_service=StubTravel(),
            scoring_service=StubScoring(),
            sea_distance_service=StubSea(),
        ).enrich_property(prop)

        property_id = prop.id
        db.session.expire_all()
        reloaded = db.session.get(Property, property_id)

    # Measured first, on the shared commit, and the score saw the result.
    assert order == [("sea", False), ("scoring", STATUS_OK)]
    assert reloaded.enrichment["sea"]["distance_m"] == 700.0


def test_an_old_profile_override_inherits_the_new_sea_weight(app):
    """Profiles predating this criterion merge over the defaults, by design.

    The owner chose a non-zero default plus a backfill, so a saved override that
    only pins value/travel is meant to pick the sea weight up rather than opt
    out of it silently.
    """
    with app.app_context():
        legacy = _profile(
            name="Sea legacy override",
            scoring_config={
                "categories": {
                    "housing": {"lifestyle": {"travel_score": 0.6, "size_score": 0.4}}
                }
            },
        )
        db.session.add(legacy)
        db.session.commit()

        prop = _property(
            source_email_id="sea-legacy",
            search_profile_id=legacy.id,
            enrichment=_sea_enrichment(STATUS_OK, 400.0),
        )
        db.session.add(prop)
        db.session.commit()

        PropertyScoringService().calculate_for_property(prop)

        weights = prop.scoring["profiles"]["lifestyle"]["weights"]
        assert weights["travel_score"] == 0.6
        assert (
            weights["sea_score"]
            == HousingPropertyScorer.DEFAULT_LIFESTYLE_WEIGHTS["sea_score"]
        )


# -- backfill rollback --------------------------------------------------


def test_snapshot_restores_scores_and_enrichment(app, tmp_path):
    """The backfill rewrites scores; rolling the app back would not undo that."""
    from utils import recalc_sea_distance as backfill

    with app.app_context():
        prop = _property(
            enrichment=_sea_enrichment(STATUS_OK, 500.0),
            score_total=Decimal("71.50"),
            score_investment=Decimal("60.00"),
            score_lifestyle=Decimal("80.25"),
            scoring={"version": 1, "marker": "before"},
        )
        db.session.add(prop)
        db.session.commit()
        property_id = prop.id

        snapshot_path = str(tmp_path / "rollback.json")
        backfill._write_snapshot([backfill._snapshot_row(prop)], snapshot_path)

        prop.score_total = Decimal("12.00")
        prop.score_investment = None
        prop.score_lifestyle = Decimal("15.00")
        prop.scoring = {"version": 1, "marker": "after"}
        prop.enrichment = _sea_enrichment(STATUS_NO_COASTLINE, None)
        db.session.commit()

        assert backfill._restore(snapshot_path) == 1

        db.session.expire_all()
        restored = db.session.get(Property, property_id)
        assert restored.score_total == Decimal("71.50")
        assert restored.score_investment == Decimal("60.00")
        assert restored.scoring["marker"] == "before"
        assert restored.enrichment["sea"]["distance_m"] == 500.0


def test_snapshot_refuses_to_overwrite_an_existing_rollback_point(app, tmp_path):
    from utils import recalc_sea_distance as backfill

    path = tmp_path / "rollback.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit):
        backfill._write_snapshot([], str(path))


# -- shared HTTP primitive ----------------------------------------------


def test_retried_response_is_closed():
    """A stream=True caller would leak the socket otherwise."""
    responses = [FakeResponse(status_code=503), FakeResponse(payload={"elements": []})]
    served = []

    def fake_request(*args, **kwargs):
        response = responses[len(served)]
        served.append(response)
        return response

    final = request_with_retries(
        fake_request, backoff_base=0.0, backoff_max=0.0, max_attempts=2
    )

    assert responses[0].closed is True
    assert final is responses[1]
    assert responses[1].closed is False
