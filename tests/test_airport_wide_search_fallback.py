"""A real airport past Nearby Search's reach must still resolve.

Property 360 (La Caridad, El Franco, Asturias -- 43.551663,-6.831426) read
"Nearest Airport: not found", one of 36 of the owner's 366 properties in the
same state while every other preset resolved. Measured live against that
exact coordinate on 2026-08-11:

* `rankby=distance` (no radius) returned 7 places, farthest 45.2 km --
  "Helipuerto Hospital de Jarrio", "Aeródromo de Vilaframil", "Club Aéreo de
  Ribadeo", "Helipuerto", "Helipuerto Parque Bomberos Grandas Salime",
  "Aeródromo la Curiscada" and "Base de Fonsagrada" (all reproduced below,
  verbatim).
* An explicit `radius=75000` and `radius=120000` both returned that identical
  set of 7 -- Google silently clamps the radius to its documented 50,000 m
  maximum, so asking for more bought nothing.
* Every one of those 7 is correctly refused by issue #171's name rule (none
  is named "airport"/"aeropuerto"/...); Asturias Airport itself sits 64.3 km
  away, past the reach of both call shapes, so it could never appear in
  either response. The "not found" was Google never being asked about the
  place that mattered, not an over-eager rejection.
* A Places Text Search (`query=airport&type=airport`, no `radius`, so no cap)
  found Asturias Airport as the nearest qualifying result on the first try.

`wide_search_query` on the airport preset (search_profile_service.py) opts it
into that Text Search as a fallback -- a second, paid call fired only when
Nearby Search already answered and still found nothing this preset accepts.
"""

from unittest.mock import Mock, patch

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from services.property_travel_service import PropertyTravelService  # noqa: E402
from services.search_profile_service import TRAVEL_PRESET_DEFS  # noqa: E402
from utils.google_api import REASON_REQUEST_DENIED  # noqa: E402


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _place(name, types, lat=43.5, lon=-6.8):
    """A Nearby/Text Search result, shaped as Google returns it."""
    return {
        "name": name,
        "place_id": f"place-{name}",
        "types": types,
        "geometry": {"location": {"lat": lat, "lng": lon}},
    }


def _payload(results):
    class _Response:
        status_code = 200

        def json(self):
            return {"status": "OK", "results": results}

    return _Response()


def _denied_payload():
    class _Response:
        status_code = 200

        def json(self):
            return {"status": "REQUEST_DENIED", "results": []}

    return _Response()


# Verbatim from the live Nearby Search response for property 360
# (43.551663,-6.831426), measured 2026-08-11: every one of the 7 airport-
# tagged places within Nearby Search's ~50 km reach, farthest 45.2 km. None
# is named "airport"/"aeropuerto"/"aeroport"/"aeroporto"/"aéroport", so #171's
# require-name rule refuses every one of them.
NEARBY_CANDIDATES = [
    _place(
        "Helipuerto Hospital de Jarrio",
        ["airport", "point_of_interest", "establishment"],
    ),
    _place(
        "Aeródromo de Vilaframil", ["airport", "point_of_interest", "establishment"]
    ),
    _place("Club Aéreo de Ribadeo", ["airport", "point_of_interest", "establishment"]),
    _place("Helipuerto", ["airport", "point_of_interest", "establishment"]),
    _place(
        "Helipuerto Parque Bomberos Grandas Salime",
        ["airport", "point_of_interest", "establishment"],
    ),
    _place("Aeródromo la Curiscada", ["airport", "point_of_interest", "establishment"]),
    _place("Base de Fonsagrada", ["airport", "point_of_interest", "establishment"]),
]

# The real airport, at its actual coordinates -- 64.3 km from property 360,
# measured via Text Search on the same date.
REAL_AIRPORT = _place(
    "Asturias Airport",
    ["airport", "establishment", "point_of_interest"],
    lat=43.5636,
    lon=-6.0353,
)


def _service():
    service = PropertyTravelService()
    service.google_places_key = "test-key"
    return service


def _routed_response(url, **_kwargs):
    """Send Nearby Search and Text Search calls to different fixtures, the
    way the real Places API endpoints differ."""
    if "nearbysearch" in url:
        return _payload(NEARBY_CANDIDATES)
    if "textsearch" in url:
        return _payload([REAL_AIRPORT])
    raise AssertionError(f"unexpected Places endpoint: {url}")


class TestTheWideSearchFallback:
    def test_a_real_airport_past_fifty_km_still_resolves(self, app):
        """Nearby Search's 7 candidates are all real, all correctly refused;
        the wide search must still find the true airport."""
        service = _service()
        seen_urls = []

        def _capture(_fn, url, **kwargs):
            seen_urls.append(url)
            return _routed_response(url, **kwargs)

        with patch(
            "services.property_travel_service.request_with_retries",
            side_effect=_capture,
        ):
            lookup = service._nearest_place_for_preset(
                43.551663, -6.831426, "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place is not None
        assert lookup.place["name"] == "Asturias Airport"
        assert lookup.failure is None
        assert any("nearbysearch" in u for u in seen_urls), (
            "the primary Nearby Search must run first"
        )
        assert any("textsearch" in u for u in seen_urls), (
            "the wide fallback must fire, since nothing nearby qualified"
        )

    def test_the_wide_search_still_refuses_a_closer_decoy(self, app):
        """Even inside the fallback, a nearer non-airport must lose to a
        farther real one -- issue #171's contract applies here too, not only
        to Nearby Search's results."""
        service = _service()
        # ~12 km away, much closer than the real airport, but "Aeródromo"
        # carries none of the required name patterns.
        decoy = _place(
            "Aeródromo de Vilaframil", ["airport", "establishment"], lat=43.6, lon=-6.7
        )

        def _mixed(_fn, url, **kwargs):
            if "nearbysearch" in url:
                return _payload(NEARBY_CANDIDATES)
            if "textsearch" in url:
                return _payload([decoy, REAL_AIRPORT])
            raise AssertionError(f"unexpected Places endpoint: {url}")

        with patch(
            "services.property_travel_service.request_with_retries",
            side_effect=_mixed,
        ):
            lookup = service._nearest_place_for_preset(
                43.551663, -6.831426, "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place["name"] == "Asturias Airport"

    def test_the_fallback_never_fires_when_nearby_search_already_found_one(self, app):
        """A preset that already resolved within 50 km must not pay for a
        second call."""
        service = _service()
        close_airport = _place(
            "Ribadeo Airport", ["airport", "establishment"], lat=43.55, lon=-6.83
        )
        call_count = {"n": 0}

        def _count(_fn, url, **kwargs):
            call_count["n"] += 1
            if "nearbysearch" in url:
                return _payload([close_airport])
            raise AssertionError("the wide fallback must not run here")

        with patch(
            "services.property_travel_service.request_with_retries",
            side_effect=_count,
        ):
            lookup = service._nearest_place_for_preset(
                43.551663, -6.831426, "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place["name"] == "Ribadeo Airport"
        assert call_count["n"] == 1, "one accepted candidate must stop the search"

    def test_a_refusal_on_nearby_search_is_not_chased_with_a_second_call(self, app):
        """An API refusal must stay a refusal (#98) -- never trigger the
        paid fallback chasing an answer that already failed to arrive."""
        service = _service()

        def _denied(_fn, url, **_kwargs):
            if "nearbysearch" in url:
                return _denied_payload()
            raise AssertionError("a refusal must not be chased with a second call")

        with patch(
            "services.property_travel_service.request_with_retries",
            side_effect=_denied,
        ):
            lookup = service._nearest_place_for_preset(
                43.551663, -6.831426, "airport", TRAVEL_PRESET_DEFS["airport"]
            )

        assert lookup.place is None
        assert lookup.failure is not None
        assert lookup.failure.reason == REASON_REQUEST_DENIED

    @pytest.mark.parametrize("preset", ["train_station", "supermarket", "school"])
    def test_other_presets_do_not_opt_into_the_fallback(self, app, preset):
        """The dense presets have never shown this failure across the owner's
        database and must not start paying for a call they do not need.

        "hospital" was in this list until 2026-08-15 and has been moved out:
        #323 narrowed its rules to refuse primary care, and the recalc that
        followed left 48 of 187 rows unresolved because a town fills Nearby
        Search's single 20-result page with private practices before any
        hospital appears. It now carries `wide_search_query` for the same
        reason "airport" does, and `tests/test_hospital_wide_search_fallback.py`
        holds that measurement."""
        service = _service()
        place_type = TRAVEL_PRESET_DEFS[preset]["place_types"][0]
        call_count = {"n": 0}

        def _count(_fn, url, **_kwargs):
            call_count["n"] += 1
            if "nearbysearch" in url:
                return _payload([])
            raise AssertionError(f"{preset} must not reach a second Places call")

        with patch(
            "services.property_travel_service.request_with_retries",
            side_effect=_count,
        ):
            lookup = service._nearest_place_for_preset(
                43.551663, -6.831426, preset, TRAVEL_PRESET_DEFS[preset]
            )

        assert lookup.place is None
        assert lookup.failure is None
        assert call_count["n"] == 1
        assert place_type  # sanity: the preset really does define a type


class TestEndToEndPersistedTravel:
    """The full `calculate_for_property` pipeline, proving the fix reaches
    what the property page actually renders."""

    def test_calculate_for_property_persists_the_real_airport(self, app):
        profile = SearchProfile(
            name="WideSearchProfile",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": {
                    key: {"enabled": key == "airport", "mode": "driving"}
                    for key in TRAVEL_PRESET_DEFS
                },
                "custom": [],
            },
        )
        db.session.add(profile)
        db.session.commit()

        prop = Property(
            source_email_id="p360-fixture",
            title="La Caridad plot",
            municipality="El Franco",
            search_profile_id=profile.id,
            location_lat=43.551663,
            location_lon=-6.831426,
        )
        db.session.add(prop)
        db.session.commit()

        def mock_get(url, params=None, timeout=0, headers=None):
            if "place/nearbysearch" in url:
                return _payload(NEARBY_CANDIDATES)
            if "place/textsearch" in url:
                return _payload([REAL_AIRPORT])
            if "distancematrix" in url:
                dests = (params or {}).get("destinations", "").split("|")
                elements = [
                    {
                        "status": "OK",
                        # Roughly an hour's drive, matching the owner's own
                        # estimate for property 360.
                        "distance": {"value": 90000},
                        "duration": {"value": 3600},
                    }
                    for _ in dests
                ]
                return Mock(
                    status_code=200, json=lambda: {"rows": [{"elements": elements}]}
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with patch(
            "services.property_travel_service.requests.get", side_effect=mock_get
        ):
            svc = PropertyTravelService(
                google_maps_key="maps-key", google_places_key="places-key"
            )
            ok = svc.calculate_for_property(prop, commit=True)

        assert ok is True

        refreshed = db.session.get(Property, prop.id)
        airport = refreshed.travel["targets"]["airport"]
        assert airport["status"] == "ok", (
            f"must resolve rather than read 'not_found': {airport!r}"
        )
        assert airport["place"]["name"] == "Asturias Airport"
        assert airport["duration_min"] == 60

        api_status = refreshed.travel["api_status"]
        assert api_status["state"] == "ok"
        assert api_status["targets"]["not_found"] == 0
