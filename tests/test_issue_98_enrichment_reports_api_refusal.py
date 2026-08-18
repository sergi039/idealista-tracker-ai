"""Regression tests for #98: a refused Google API is not a search result.

`PropertyTravelService.calculate_for_property()` used to return True and store
`status: "not_found"` for every target even when Google had rejected every
single request. Eight months of enrichment runs looked green while not one of
350 properties ever received a travel time.

The behaviour these tests pin down:

* transport failure (REQUEST_DENIED, HTTP error, no key, network error) is
  stored as `status: "unavailable"` with a reason code, never as "not_found";
* a run where every target failed that way returns False;
* an answered run with nothing nearby is still a success, still "not_found";
* the refusal is logged once per run at ERROR with the code Google returned;
* a refused answer is never written to the enrichment cache.

The `_mixed` test reproduces the exact configuration seen on 2026-08-08: the
Places key was from a different Google project and got REQUEST_DENIED while
Distance Matrix answered normally.
"""

import logging
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import requests

from app import create_app, db
from models import Land, Property, SearchProfile
from services.enrichment_service import EnrichmentService
from services.property_travel_service import (
    TRAVEL_STATE_DEGRADED,
    TRAVEL_STATE_OK,
    TRAVEL_STATE_UNAVAILABLE,
    PropertyTravelService,
)
from tests import setup_test_environment
from utils.cache import cache
from utils.google_api import (
    REASON_HTTP_ERROR,
    REASON_NETWORK_ERROR,
    REASON_NO_API_KEY,
    REASON_REQUEST_DENIED,
    read_api_payload,
)

BILLING_MESSAGE = (
    "You must enable Billing on the Google Cloud Project at "
    "https://console.cloud.google.com/project/_/billing/enable"
)


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


def _osm_refuses(reason="overpass_refused"):
    """Make the OSM preset lookup refuse, the way Overpass really can.

    The presets stopped asking Google on 2026-08-18 (services/osm_places.py),
    so a scenario about "the place lookup refused" has to refuse where the
    lookup now happens. What is pinned is unchanged and is the whole point of
    #98: a refusal is reported as `unavailable`, never as `not_found`, because
    "nobody answered" and "there is nothing there" are different facts and
    only the second may be stored as a result.
    """
    import services.osm_places as osm_places
    from utils.google_api import GoogleApiFailure

    osm_places.lookup_candidates = lambda service, specs, lat, lon: (
        None,
        GoogleApiFailure(reason=reason),
    )


def _osm_answers(candidates):
    """Make the OSM preset lookup answer with these candidates per preset.

    The counterpart of `_osm_refuses`: a preset with an empty list is
    "Overpass replied and there is nothing of that type here", which is the
    measured absence #98 exists to keep apart from a refusal.
    """
    import services.osm_places as osm_places

    osm_places.lookup_candidates = lambda service, specs, lat, lon: (
        {key: list(candidates.get(key) or []) for key in specs},
        None,
    )


def _make_profile(enabled_presets, custom=None):
    presets = {
        key: {"enabled": key in enabled_presets, "mode": "driving"}
        for key in [
            "airport",
            "train_station",
            "hospital",
            "police",
            "supermarket",
            "school",
        ]
    }
    profile = SearchProfile(
        name=f"Profile {len(enabled_presets)}-{id(enabled_presets)}",
        is_active=True,
        is_default=True,
        travel_targets={"presets": presets, "custom": custom or []},
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _make_property(profile, source_email_id):
    prop = Property(
        source_email_id=source_email_id,
        title="Land in Cudillero, Asturias",
        municipality="Cudillero",
        search_profile_id=profile.id,
        location_lat=Decimal("43.6516865"),
        location_lon=Decimal("-7.8400525"),
        # Google's refusal is the subject here, so the origin has to be one
        # travel would actually spend a request on.
        location_accuracy="precise",
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _denied_response():
    return Mock(
        status_code=200,
        json=lambda: {
            "status": "REQUEST_DENIED",
            "error_message": BILLING_MESSAGE,
            "results": [],
            "rows": [],
        },
    )


def _place_response(name, lat, lon):
    return Mock(
        status_code=200,
        json=lambda: {
            "status": "OK",
            "results": [
                {
                    "name": name,
                    "place_id": f"pid-{name}",
                    "types": ["airport"],
                    "geometry": {"location": {"lat": lat, "lng": lon}},
                }
            ],
        },
    )


def _zero_results_response():
    return Mock(status_code=200, json=lambda: {"status": "ZERO_RESULTS", "results": []})


def _matrix_response(destinations):
    elements = [
        {
            "status": "OK",
            "distance": {"value": 1000 * (idx + 1)},
            "duration": {"value": 600 * (idx + 1)},
        }
        for idx in range(len(destinations))
    ]
    return Mock(status_code=200, json=lambda: {"rows": [{"elements": elements}]})


def _statuses(prop):
    """Statuses of the targets that were actually looked up (skips disabled)."""
    targets = (prop.travel or {}).get("targets") or {}
    return {
        key: value.get("status")
        for key, value in targets.items()
        if value.get("status") != "disabled"
    }


def _api_status(prop):
    return (prop.travel or {}).get("api_status") or {}


class TestTravelRefusalIsNotAResult:
    def test_request_denied_returns_false_and_marks_targets_unavailable(
        self, app, caplog
    ):
        _osm_refuses()
        profile = _make_profile(
            {"airport", "supermarket"},
            custom=[
                {
                    "id": "home",
                    "name": "Home",
                    "lat": 43.36,
                    "lon": -5.84,
                    "mode": "driving",
                }
            ],
        )
        prop = _make_property(profile, "issue98_denied")

        def mock_get(url, params=None, timeout=0, headers=None):
            return _denied_response()

        with caplog.at_level(logging.ERROR):
            with patch(
                "services.property_travel_service.requests.get", side_effect=mock_get
            ):
                svc = PropertyTravelService(
                    google_maps_key="maps", google_places_key="places"
                )
                ok = svc.calculate_for_property(prop, commit=True)

        assert ok is False

        refreshed = db.session.get(Property, prop.id)
        statuses = _statuses(refreshed)
        assert statuses == {
            "airport": "unavailable",
            "supermarket": "unavailable",
            "custom:home": "unavailable",
        }
        assert "not_found" not in statuses.values()

        targets = refreshed.travel["targets"]
        # The preset lookup left Google on 2026-08-18; what refused it here is
        # Overpass, and the reason it carries is Overpass's own. What is under
        # test is unchanged: a refusal is `unavailable`, never `not_found`.
        assert targets["airport"]["error"] == "overpass_refused"
        assert targets["custom:home"]["error"] == REASON_REQUEST_DENIED
        assert targets["custom:home"]["stage"] == "distance_matrix"

        api_status = _api_status(refreshed)
        assert api_status["state"] == TRAVEL_STATE_UNAVAILABLE
        assert api_status["targets"]["resolved"] == 0
        assert api_status["targets"]["not_found"] == 0
        # Two presets refused by Overpass, one custom target refused by
        # Distance Matrix, which is still Google's. The tally counts
        # reasons, and after 2026-08-18 the run really does have two
        # sources of refusal -- flattening them to one would hide which
        # half is down when only one of them is.
        assert api_status["errors"] == {
            "overpass_refused": 2,
            REASON_REQUEST_DENIED: 1,
        }

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, "the refusal must be logged exactly once per run"
        # The line names the reasons, not a vendor: the presets are
        # Overpass's now and only the routing is Google's, so "could not reach
        # Google" would point whoever reads it at the wrong status page.
        assert "overpass_refused" in errors[0].getMessage()
        assert str(prop.id) in errors[0].getMessage()

    def test_refusal_is_not_cached_as_an_empty_result(self, app):
        """A denied run must not make the next run serve "nothing found"."""
        profile = _make_profile(
            set(),
            custom=[
                {
                    "id": "home",
                    "name": "Home",
                    "lat": 43.36,
                    "lon": -5.84,
                    "mode": "driving",
                }
            ],
        )
        prop = _make_property(profile, "issue98_cache")

        with patch(
            "services.property_travel_service.requests.get",
            side_effect=lambda *a, **k: _denied_response(),
        ):
            svc = PropertyTravelService(
                google_maps_key="maps", google_places_key="places"
            )
            assert svc.calculate_for_property(prop, commit=True) is False

        calls = []

        def answering_get(url, params=None, timeout=0, headers=None):
            calls.append(url)
            return _matrix_response((params or {}).get("destinations", "").split("|"))

        with patch(
            "services.property_travel_service.requests.get", side_effect=answering_get
        ):
            svc = PropertyTravelService(
                google_maps_key="maps", google_places_key="places"
            )
            assert svc.calculate_for_property(prop, commit=True) is True

        assert calls, "the retry must reach Google, not a cached refusal"
        refreshed = db.session.get(Property, prop.id)
        assert refreshed.travel["targets"]["custom:home"]["duration_min"] == 10
        assert _api_status(refreshed)["state"] == TRAVEL_STATE_OK

    def test_places_denied_while_distance_matrix_answers_is_degraded(self, app, caplog):
        """The 2026-08-08 configuration: one key works, the other does not."""
        _osm_refuses()
        profile = _make_profile(
            {"airport"},
            custom=[
                {
                    "id": "home",
                    "name": "Home",
                    "lat": 43.36,
                    "lon": -5.84,
                    "mode": "driving",
                }
            ],
        )
        prop = _make_property(profile, "issue98_mixed")

        def mock_get(url, params=None, timeout=0, headers=None):
            if "place/nearbysearch" in url:
                return _denied_response()
            if "distancematrix" in url:
                return _matrix_response(
                    (params or {}).get("destinations", "").split("|")
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with caplog.at_level(logging.ERROR):
            with patch(
                "services.property_travel_service.requests.get", side_effect=mock_get
            ):
                svc = PropertyTravelService(
                    google_maps_key="maps", google_places_key="places"
                )
                ok = svc.calculate_for_property(prop, commit=True)

        # Distance Matrix produced a real value, so the run is not a total loss
        # - but the airport target must not read as "no airport nearby".
        assert ok is True

        refreshed = db.session.get(Property, prop.id)
        targets = refreshed.travel["targets"]
        assert targets["airport"]["status"] == "unavailable"
        # Overpass refused the preset; Distance Matrix answered. The scenario
        # is the same shape it was on 2026-08-08 -- one source down, one up,
        # and the run reports `degraded` rather than success or failure --
        # only the down half is Overpass now.
        assert targets["airport"]["error"] == "overpass_refused"
        # `stage` names the step that failed, not the vendor that ran it:
        # the place-resolution stage is still "places" now that OSM does
        # the resolving, and renaming it would break every stored row.
        assert targets["airport"].get("stage") == "places"
        assert targets["custom:home"]["status"] == "ok"
        assert targets["custom:home"]["duration_min"] == 10

        api_status = _api_status(refreshed)
        assert api_status["state"] == TRAVEL_STATE_DEGRADED
        assert api_status["targets"] == {
            "total": 2,
            "resolved": 1,
            "estimated": 0,
            "not_found": 0,
            "unavailable": 1,
        }

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        # The line names the reasons, not a vendor: the presets are
        # Overpass's now and only the routing is Google's, so "could not reach
        # Google" would point whoever reads it at the wrong status page.
        assert "overpass_refused" in errors[0].getMessage()

    def test_answered_but_nothing_nearby_is_still_a_success(self, app, caplog):
        """Control: a real "nothing there" must keep working exactly as before."""
        # Both presets come from OpenStreetMap since 2026-08-18, so the control
        # is expressed there: an airport within reach, and no station.
        _osm_answers(
            {
                "airport": [
                    {
                        "name": "Aeropuerto de Asturias",
                        "lat": 43.56,
                        "lon": -6.03,
                        "distance_m": 30000,
                        "source": "osm",
                    }
                ],
                "train_station": [],
            }
        )
        profile = _make_profile({"airport", "train_station"})
        prop = _make_property(profile, "issue98_zero_results")

        def mock_get(url, params=None, timeout=0, headers=None):
            if "place/nearbysearch" in url:
                if (params or {}).get("type") == "airport":
                    return _place_response("Asturias Airport", 43.56, -6.03)
                return _zero_results_response()
            if "distancematrix" in url:
                return _matrix_response(
                    (params or {}).get("destinations", "").split("|")
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with caplog.at_level(logging.ERROR):
            with patch(
                "services.property_travel_service.requests.get", side_effect=mock_get
            ):
                svc = PropertyTravelService(
                    google_maps_key="maps", google_places_key="places"
                )
                ok = svc.calculate_for_property(prop, commit=True)

        assert ok is True

        refreshed = db.session.get(Property, prop.id)
        targets = refreshed.travel["targets"]
        assert targets["train_station"]["status"] == "not_found"
        assert targets["train_station"]["reason"] == "no_nearby_place"
        assert "error" not in targets["train_station"]
        assert targets["airport"]["status"] == "ok"
        assert targets["airport"]["duration_min"] == 10

        api_status = _api_status(refreshed)
        assert api_status["state"] == TRAVEL_STATE_OK
        assert api_status["errors"] == {}
        assert api_status["targets"]["not_found"] == 1
        assert api_status["targets"]["resolved"] == 1

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_http_error_is_unavailable_not_not_found(self, app):
        # Overpass answers 504 whenever both of its per-IP slots are busy, so
        # an HTTP error is if anything more likely here than it was at Google.
        _osm_refuses(REASON_HTTP_ERROR)
        profile = _make_profile({"airport"})
        prop = _make_property(profile, "issue98_http_500")

        # 500 is retried by request_with_retries; the final response still fails.
        with patch("utils.http.time.sleep", return_value=None):
            with patch(
                "services.property_travel_service.requests.get",
                side_effect=lambda *a, **k: Mock(
                    status_code=500, json=lambda: {"status": "UNKNOWN_ERROR"}
                ),
            ):
                svc = PropertyTravelService(
                    google_maps_key="maps", google_places_key="places"
                )
                assert svc.calculate_for_property(prop, commit=True) is False

        refreshed = db.session.get(Property, prop.id)
        assert refreshed.travel["targets"]["airport"]["status"] == "unavailable"
        assert refreshed.travel["targets"]["airport"]["error"] == REASON_HTTP_ERROR

    def test_network_error_is_unavailable_not_not_found(self, app):
        # Overpass can fail exactly this way, so the scenario survives the
        # move off Places unchanged -- only the host that drops the connection
        # is different.
        _osm_refuses(REASON_NETWORK_ERROR)
        profile = _make_profile({"airport"})
        prop = _make_property(profile, "issue98_network")

        with patch("utils.http.time.sleep", return_value=None):
            with patch(
                "services.property_travel_service.requests.get",
                side_effect=requests.ConnectionError("name resolution failed"),
            ):
                svc = PropertyTravelService(
                    google_maps_key="maps", google_places_key="places"
                )
                assert svc.calculate_for_property(prop, commit=True) is False

        refreshed = db.session.get(Property, prop.id)
        assert refreshed.travel["targets"]["airport"]["status"] == "unavailable"
        assert refreshed.travel["targets"]["airport"]["error"] == REASON_NETWORK_ERROR

    def test_missing_places_key_is_unavailable_not_not_found(self, app):
        # The presets stopped needing a Places key on 2026-08-18: they are
        # answered from OpenStreetMap, which has none. What the key still
        # governs is the beach lookup, which is Google's until step 2 finishes.
        # The #98 guarantee under test is unchanged -- a lookup that could not
        # be made is `unavailable`, never `not_found` -- so the scenario keeps
        # its shape and refuses where the preset lookup now happens.
        _osm_refuses(REASON_NO_API_KEY)
        profile = _make_profile({"airport"})
        prop = _make_property(profile, "issue98_no_key")

        def mock_get(url, params=None, timeout=0, headers=None):
            raise AssertionError("no request may be made without a key")

        with patch(
            "services.property_travel_service.requests.get", side_effect=mock_get
        ):
            svc = PropertyTravelService(google_maps_key="maps", google_places_key="")
            assert svc.calculate_for_property(prop, commit=True) is False

        refreshed = db.session.get(Property, prop.id)
        assert refreshed.travel["targets"]["airport"]["status"] == "unavailable"
        assert refreshed.travel["targets"]["airport"]["error"] == REASON_NO_API_KEY

    def test_refused_run_does_not_discard_previous_travel_times(self, app):
        _osm_refuses()
        profile = _make_profile({"airport"})
        prop = _make_property(profile, "issue98_preserve")
        prop.travel = {
            "updated_at": "2026-08-07T10:00:00+00:00",
            "origin": {"lat": 43.65, "lon": -7.84},
            "targets": {
                "airport": {
                    "kind": "preset",
                    "status": "ok",
                    "distance_m": 42000,
                    "duration_min": 38,
                }
            },
        }
        db.session.commit()

        with patch(
            "services.property_travel_service.requests.get",
            side_effect=lambda *a, **k: _denied_response(),
        ):
            svc = PropertyTravelService(
                google_maps_key="maps", google_places_key="places"
            )
            assert svc.calculate_for_property(prop, commit=True) is False

        refreshed = db.session.get(Property, prop.id)
        assert refreshed.travel["targets"]["airport"]["duration_min"] == 38
        assert _api_status(refreshed)["state"] == TRAVEL_STATE_UNAVAILABLE


class TestLandEnrichmentRefusal:
    """The legacy /lands path had the same defect in Google Places."""

    def _land(self):
        land = Land(
            source_email_id="issue98_land",
            title="Land in Cudillero",
            municipality="Cudillero",
            location_lat=Decimal("43.6516865"),
            location_lon=Decimal("-7.8400525"),
        )
        db.session.add(land)
        db.session.commit()
        return land

    def test_places_refusal_is_not_stored_as_amenity_unavailable(self, app, caplog):
        land = self._land()
        service = EnrichmentService()
        service.google_places_key = "places"

        with caplog.at_level(logging.ERROR):
            with patch(
                "services.enrichment_service.request_with_retries",
                side_effect=lambda *a, **k: _denied_response(),
            ):
                with patch("services.enrichment_service.time.sleep", return_value=None):
                    failure = service._enrich_with_google_places(land)

        assert failure is not None
        assert failure.reason == REASON_REQUEST_DENIED

        infrastructure = land.infrastructure_extended or {}
        transport = land.transport or {}
        assert "supermarket_available" not in infrastructure
        assert "train_station_available" not in transport
        # And no invented distances either.
        assert "supermarket_distance" not in infrastructure

        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_a_refused_airport_fallback_is_not_stored_as_no_airport(self, app):
        """#98's invariant reaches the airport Text Search too.

        The test above patches every request, so the *first* Places call
        fails and the airport branch is skipped before it ever asks again.
        The fallback added for the 50 km clamp is therefore a second, later
        place where a refusal could be written down as "no airport here" —
        this pins that it is not. The measurement behind the fallback and
        the rest of its behaviour live in
        `tests/test_legacy_land_airport_wide_search.py`.
        """
        land = self._land()
        service = EnrichmentService()
        service.google_places_key = "places"

        def _answer(_method, url, params=None, **_kwargs):
            if "textsearch" in url:
                return _denied_response()
            if (params or {}).get("type") == "airport":
                # Google answers, but only with a helipad -- #171's rules
                # refuse it, which is what sends the code to the fallback.
                return _place_response("Helipuerto Hospital de Jarrio", 43.506, -6.886)
            return Mock(
                status_code=200, json=lambda: {"status": "ZERO_RESULTS", "results": []}
            )

        with patch(
            "services.enrichment_service.request_with_retries", side_effect=_answer
        ):
            with patch("services.enrichment_service.time.sleep", return_value=None):
                failure = service._enrich_with_google_places(land)

        assert failure is not None
        assert failure.reason == REASON_REQUEST_DENIED
        transport = land.transport or {}
        assert "airport_available" not in transport
        assert "airport_distance" not in transport

    def test_enrich_land_reports_failure_when_google_refuses(self, app):
        land = self._land()
        service = EnrichmentService()
        service.google_places_key = "places"
        service.google_maps_key = "maps"

        with patch(
            "services.enrichment_service.request_with_retries",
            side_effect=lambda *a, **k: _denied_response(),
        ):
            with patch("services.enrichment_service.time.sleep", return_value=None):
                with patch(
                    "services.enrichment_service.EnrichmentService._enrich_with_osm_data",
                    # None is "Overpass answered": this test is about Google
                    # refusing, and since #153 the OSM return value decides
                    # whether the run is `degraded` on top of that.
                    return_value=None,
                ):
                    with patch("services.enrichment_service.ScoringService"):
                        with patch(
                            "services.travel_time_service.TravelTimeService."
                            "calculate_travel_times",
                            return_value=False,
                        ):
                            assert service.enrich_land(land.id) is False


class TestGoogleApiPayloadClassification:
    def test_zero_results_is_an_answer(self):
        payload, failure = read_api_payload(
            Mock(
                status_code=200, json=lambda: {"status": "ZERO_RESULTS", "results": []}
            )
        )
        assert failure is None
        assert payload == {"status": "ZERO_RESULTS", "results": []}

    def test_request_denied_is_a_failure_with_the_google_code(self):
        payload, failure = read_api_payload(_denied_response())
        assert payload is None
        assert failure.reason == REASON_REQUEST_DENIED
        assert failure.status == "REQUEST_DENIED"
        assert "Billing" in failure.describe()

    def test_over_query_limit_is_a_failure(self):
        _payload, failure = read_api_payload(
            Mock(status_code=200, json=lambda: {"status": "OVER_QUERY_LIMIT"})
        )
        assert failure.reason == "over_query_limit"

    def test_non_200_is_a_failure(self):
        _payload, failure = read_api_payload(Mock(status_code=503, json=lambda: {}))
        assert failure.reason == REASON_HTTP_ERROR
        assert failure.http_status == 503

    def test_missing_status_is_treated_as_an_answer(self):
        payload, failure = read_api_payload(
            Mock(status_code=200, json=lambda: {"rows": [{"elements": []}]})
        )
        assert failure is None
        assert payload["rows"] == [{"elements": []}]
