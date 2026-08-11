"""The legacy `Land` enrichment must not call a helipad an airport.

`EnrichmentService._enrich_with_google_places` looked for an airport with
`type=airport`, took `min(results, key=distance)` with no name rule at all,
and — when nothing was within its 5 km primary radius — retried with
`radius=100000`, described in the code as "within 100km radius".

Both halves were wrong, measured live on 2026-08-11 against 43.551663,
-6.831426 (property 360, La Caridad, El Franco, Asturias):

* `radius=50000`, `radius=100000` and `radius=200000` returned the **identical
  seven places**, same seven `place_id`s, farthest 45.21 km. Google clamps
  Nearby Search to its documented 50,000 m maximum and says nothing about it,
  so the "100km" retry never reached past 50 km in its life.
* All seven — reproduced verbatim in `_MEASURED_NEARBY` below — are helipads,
  light-aircraft aerodromes or an aeroclub. With no name rule the nearest of
  them, a hospital helipad 6.75 km away, was recorded as *the airport*.
* Asturias Airport (OVD, 43.5636,-6.0353) is 64.3 km away: past the clamp, so
  it could never appear in either response.

The damage was live. Across the owner's 168 lands, `transport`'s stored
airport sat at a median **0.27x** the straight-line distance to the real
airport while `Land.distance_airport` — filled from Distance Matrix by another
path — sat at a median 1.53x, i.e. a genuine road distance to the genuine
airport. 145 of the 168 disagreed by more than 3 km; `/lands/15` rendered both
figures at once, "Airport Distance 56min 85km" directly above "Airport
Distance 8min". `ScoringService._score_transport` reads `airport_available`
and `airport_distance`, so the helipad also moved a score: dropping it shifts
158 of the 168 by a median +1.26 points of `score_total`.

The fix pairs issue #171's name rules (shared, not copied — see
`services/place_rules.py`) with a Places **Text Search** fallback, which takes
no `radius` and so carries none of Nearby Search's cap. Note that the two
corrections only work together: the rules alone would turn a wrong distance
into a wrong "no airport here", and the Text Search alone would never fire
because the unfiltered helipad already counted as a hit.
"""

import logging
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from app import create_app, db
from models import Land
from services.enrichment_service import EnrichmentService
from tests import setup_test_environment
from utils.cache import cache
from utils.google_api import REASON_REQUEST_DENIED

# Property 360's coordinates, the ones every number in this file was measured
# against.
LAT, LON = 43.551663, -6.831426

# Verbatim from the live `type=airport` response at those coordinates on
# 2026-08-11 — the same seven for radius=50000, 100000 and 200000.
_MEASURED_NEARBY = [
    ("Helipuerto Hospital de Jarrio", 43.5060, -6.8862),
    ("Aeródromo de Vilaframil", 43.5386, -7.0862),
    ("Club Aéreo de Ribadeo", 43.5372, -7.0885),
    ("Helipuerto", 43.4230, -7.0563),
    ("Helipuerto Parque Bomberos Grandas Salime", 43.2225, -6.8757),
    ("Aeródromo la Curiscada", 43.3155, -6.3336),
    ("Base de Fonsagrada", 43.1391, -7.0682),
]

# The place the old code could never reach: 64.3 km away, past the clamp.
_REAL_AIRPORT = ("Aeropuerto de Asturias", 43.5636, -6.0353)

_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
_TEXT_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        cache.clear()
        db.create_all()
        yield app
        db.drop_all()
        cache.clear()


@pytest.fixture
def land(app):
    land = Land(
        source_email_id="airport_wide_search",
        title="Land in La Caridad",
        municipality="El Franco",
        location_lat=Decimal(str(LAT)),
        location_lon=Decimal(str(LON)),
    )
    db.session.add(land)
    db.session.commit()
    return land


def _results(places, extra_types=()):
    return [
        {
            "name": name,
            "place_id": f"pid::{name}",
            "types": ["airport", "point_of_interest", *extra_types],
            "geometry": {"location": {"lat": lat, "lng": lng}},
        }
        for name, lat, lng in places
    ]


def _ok(results):
    return Mock(status_code=200, json=lambda: {"status": "OK", "results": results})


def _zero():
    return Mock(status_code=200, json=lambda: {"status": "ZERO_RESULTS", "results": []})


def _denied():
    return Mock(
        status_code=200,
        json=lambda: {
            "status": "REQUEST_DENIED",
            "error_message": "Billing is not enabled",
            "results": [],
        },
    )


class _Google:
    """Stands in for Google, recording every request the service makes.

    Only the airport lookups matter here, so every other amenity answers
    ZERO_RESULTS — an answer, not a refusal, so the rest of the run behaves
    normally and nothing else writes to `transport`.
    """

    def __init__(self, nearby_airports=None, text_airports=None, text_response=None):
        self.nearby_airports = nearby_airports or []
        self.text_airports = text_airports
        self.text_response = text_response
        self.calls = []

    def __call__(self, _method, url, params=None, **_kwargs):
        params = params or {}
        self.calls.append((url, dict(params)))
        if url == _TEXT_URL:
            if self.text_response is not None:
                return self.text_response
            return _ok(_results(self.text_airports or []))
        if params.get("type") == "airport":
            return (
                _ok(_results(self.nearby_airports)) if self.nearby_airports else _zero()
            )
        return _zero()

    @property
    def airport_calls(self):
        return [
            (url, p)
            for url, p in self.calls
            if url == _TEXT_URL or p.get("type") == "airport"
        ]


def _run(service, land, google):
    with patch("services.enrichment_service.request_with_retries", side_effect=google):
        with patch("services.enrichment_service.time.sleep", return_value=None):
            return service._enrich_with_google_places(land)


@pytest.fixture
def service():
    service = EnrichmentService()
    service.google_places_key = "places"
    return service


class TestTheMeasuredFailure:
    def test_the_seven_measured_places_no_longer_become_the_airport(
        self, land, service
    ):
        """The exact response that made a hospital helipad "the airport"."""
        google = _Google(
            nearby_airports=_MEASURED_NEARBY, text_airports=[_REAL_AIRPORT]
        )
        assert _run(service, land, google) is None

        transport = land.transport or {}
        assert transport["airport_available"] is True
        # 64.3 km to the real airport, not 6.75 km to Helipuerto Jarrio.
        assert transport["airport_distance"] == pytest.approx(64_300, abs=1_500)
        assert transport["airport_distance"] > 50_000

    def test_the_wide_lookup_is_not_a_radius_google_ignores(self, land, service):
        """The bug itself: `radius=100000` came back clamped to 50 km.

        Nothing may depend on a radius past Google's documented maximum, so
        no request may ask for one — the wide reach has to come from the
        endpoint that has no cap.
        """
        google = _Google(
            nearby_airports=_MEASURED_NEARBY, text_airports=[_REAL_AIRPORT]
        )
        _run(service, land, google)

        for url, params in google.calls:
            radius = params.get("radius")
            assert radius is None or int(radius) <= 50_000, (
                f"{url} asked for radius={radius}; Google clamps to 50,000 m "
                "and returns the identical result set (measured 2026-08-11)"
            )

        assert [url for url, _ in google.airport_calls] == [_NEARBY_URL, _TEXT_URL]
        text_params = google.airport_calls[-1][1]
        assert "radius" not in text_params
        assert text_params["query"] == "airport"
        assert text_params["type"] == "airport"
        assert text_params["location"] == f"{LAT},{LON}"


class TestTheRulesApplyToBothCalls:
    def test_a_closer_decoy_in_the_fallback_still_loses(self, land, service):
        """Text Search ranks by relevance, so its own results get filtered too."""
        decoy = ("Helipuerto de Navia", 43.5400, -6.7200)  # ~9 km, not an airport
        google = _Google(
            nearby_airports=_MEASURED_NEARBY,
            text_airports=[decoy, _REAL_AIRPORT],
        )
        _run(service, land, google)

        assert (land.transport or {})["airport_distance"] > 50_000

    def test_a_rejected_type_cannot_win_on_distance(self, land, service):
        """#171's reject_types: a hospital tagged `airport` is not an airport."""
        google = _Google(
            nearby_airports=[],
            text_airports=[_REAL_AIRPORT],
        )
        google.text_response = _ok(
            _results([("Airport Hotel Asturias", 43.5520, -6.8400)], ("lodging",))
            + _results([_REAL_AIRPORT])
        )
        _run(service, land, google)

        assert (land.transport or {})["airport_distance"] > 50_000


class TestCostAndRefusal:
    def test_a_real_airport_nearby_costs_no_second_call(self, land, service):
        """The fallback is the exception, not the rule."""
        google = _Google(nearby_airports=[("Aeropuerto de Asturias", 43.56, -6.80)])
        _run(service, land, google)

        assert [url for url, _ in google.airport_calls] == [_NEARBY_URL]
        assert (land.transport or {})["airport_available"] is True

    def test_a_refused_fallback_is_never_stored_as_no_airport(
        self, land, service, caplog
    ):
        """#98, in the new call: we did not get to look, so we claim nothing."""
        google = _Google(nearby_airports=_MEASURED_NEARBY, text_response=_denied())

        with caplog.at_level(logging.ERROR):
            failure = _run(service, land, google)

        assert failure is not None
        assert failure.reason == REASON_REQUEST_DENIED

        transport = land.transport or {}
        assert "airport_available" not in transport
        assert "airport_distance" not in transport
        assert "airport_travel_time" not in transport

    def test_an_answered_fallback_with_nothing_qualifying_is_a_real_absence(
        self, land, service
    ):
        """The other half of #98: a measured absence *is* recordable."""
        google = _Google(nearby_airports=_MEASURED_NEARBY, text_airports=[])
        assert _run(service, land, google) is None

        transport = land.transport or {}
        assert transport["airport_available"] is False
        assert "airport_distance" not in transport


class TestOtherAmenitiesAreUntouched:
    def test_only_the_airport_gets_a_wide_lookup(self, land, service):
        """Train and bus stations never reach a second Places call."""
        google = _Google(nearby_airports=_MEASURED_NEARBY, text_airports=[])
        _run(service, land, google)

        text_calls = [p for url, p in google.calls if url == _TEXT_URL]
        assert len(text_calls) == 1
        assert text_calls[0]["query"] == "airport"


class TestTheRulesAreSharedNotCopied:
    def test_the_legacy_path_reads_the_same_rules_as_properties(self):
        """A second copy of the patterns is what let this path drift for months."""
        from services.enrichment_service import _AIRPORT_RULES
        from services.place_rules import place_rules_from
        from services.search_profile_service import TRAVEL_PRESET_DEFS

        assert _AIRPORT_RULES == place_rules_from(TRAVEL_PRESET_DEFS["airport"])
        # And it really does refuse every one of the seven measured places.
        assert (
            EnrichmentService._accepted_airports(
                [
                    {"name": name, "types": ["airport"]}
                    for name, _lat, _lon in _MEASURED_NEARBY
                ]
            )
            == []
        )
