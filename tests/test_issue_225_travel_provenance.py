"""Issue #225: an estimate stopped impersonating a measurement.

`TravelTimeService.calculate_travel_times()` fell back to a haversine distance
over an assumed speed whenever Google Distance Matrix was unavailable or a
destination was missing from the response, and wrote the result straight into
`land.travel_time_*` — the same columns a real measurement fills, with nothing
distinguishing the two. A straight line to Oviedo at 55 km/h is not a drive
through Asturian mountains, and per issue #98 an unpaid Distance Matrix is
exactly the state this app has been in, so the fallback *is* what ran.

`PropertyTravelService` already records provenance; this gives the legacy land
path the same honesty:

* a travel column holds a measurement or nothing;
* the estimate survives, labelled, in the new `Land.travel["targets"]`;
* the run says `ok` / `degraded` / `unavailable`, the vocabulary #153 settled on;
* a run that measured nothing does not poison the seven-day cache.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Land
from services.travel_time_service import TravelTimeService
from tests import setup_test_environment

PLACE = {"name": "Somewhere", "lat": 43.5, "lon": -6.8}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def land(app):
    land = Land(
        source_email_id="issue-225",
        title="A plot",
        location_lat=43.55,
        location_lon=-6.83,
    )
    db.session.add(land)
    db.session.commit()
    return land


def _service(google_answer):
    """A service whose Places lookups succeed and whose Distance Matrix answers
    `google_answer` (a list, or None for "no key configured")."""
    service = TravelTimeService()
    service.google_places_key = "places-key"
    service.google_maps_key = "maps-key" if google_answer is not None else None
    return service


def _run(service, land, google_answer, fallback={"time": 42, "distance": 30.0}):
    with (
        patch.object(TravelTimeService, "_nearest_place", return_value=dict(PLACE)),
        patch.object(
            TravelTimeService, "_get_google_travel_times", return_value=google_answer
        ),
        patch.object(
            TravelTimeService,
            "_calculate_fallback_travel_time",
            return_value=dict(fallback) if fallback else None,
        ),
        patch(
            "services.travel_time_service.get_cached_enrichment_data", return_value=None
        ),
        patch("services.travel_time_service.cache_enrichment_data") as cache,
    ):
        ok = service.calculate_travel_times(land.id)
    return ok, cache


class TestAnUnmeasuredTargetStaysUnmeasured:
    def test_no_google_key_leaves_every_column_empty(self, app, land):
        service = _service(None)

        ok, _cache = _run(service, land, None)

        assert ok is True
        assert land.travel_time_oviedo is None, (
            "a haversine guess used to be written here, indistinguishable from "
            "a measured drive time"
        )
        assert land.travel_time_gijon is None
        assert land.travel_time_airport is None

    def test_the_estimate_survives_labelled(self, app, land):
        service = _service(None)

        _run(service, land, None)
        travel = land.travel

        assert travel["api_status"] == "unavailable"
        assert travel["checked_at"]
        assert travel["targets"]["oviedo"] == {
            "source": "estimate",
            "time_min": 42,
            "distance_km": 30.0,
        }

    def test_a_target_with_no_estimate_either_reads_as_unavailable(self, app, land):
        service = _service(None)

        _run(service, land, None, fallback=None)

        assert land.travel["api_status"] == "unavailable"
        assert land.travel["targets"]["oviedo"] == {"source": "unavailable"}

    def test_a_run_that_measured_nothing_does_not_fill_the_cache(self, app, land):
        """Seven days of a cached non-answer would hide the next real one."""
        service = _service(None)

        _ok, cache = _run(service, land, None)

        cache.assert_not_called()


class TestAMeasuredTargetIsStillWritten:
    def _google(self, count):
        return [{"time": 55, "distance": 60.0} for _ in range(count)]

    def test_a_full_answer_writes_the_columns_and_reports_ok(self, app, land):
        service = _service([])

        with (
            patch.object(TravelTimeService, "_nearest_place", return_value=dict(PLACE)),
            patch.object(
                TravelTimeService,
                "_get_google_travel_times",
                side_effect=lambda origin, dests: self._google(len(dests)),
            ),
            patch(
                "services.travel_time_service.get_cached_enrichment_data",
                return_value=None,
            ),
            patch("services.travel_time_service.cache_enrichment_data") as cache,
        ):
            assert service.calculate_travel_times(land.id) is True

        assert land.travel_time_oviedo == 55
        assert land.travel_time_airport == 55
        assert land.distance_airport == 60.0
        assert land.travel["api_status"] == "ok"
        assert land.travel["targets"]["oviedo"]["source"] == "google"
        cache.assert_called_once()

    def test_a_partial_answer_is_degraded_and_only_writes_what_was_measured(
        self, app, land
    ):
        """The first destination answered, the rest did not."""
        service = _service([])

        def partial(origin, dests):
            return [{"time": 55, "distance": 60.0}] + [None] * (len(dests) - 1)

        with (
            patch.object(TravelTimeService, "_nearest_place", return_value=dict(PLACE)),
            patch.object(
                TravelTimeService, "_get_google_travel_times", side_effect=partial
            ),
            patch.object(
                TravelTimeService,
                "_calculate_fallback_travel_time",
                return_value={"time": 42, "distance": 30.0},
            ),
            patch(
                "services.travel_time_service.get_cached_enrichment_data",
                return_value=None,
            ),
            patch("services.travel_time_service.cache_enrichment_data"),
        ):
            service.calculate_travel_times(land.id)

        travel = land.travel
        assert travel["api_status"] == "degraded"
        assert land.travel_time_oviedo == 55
        assert land.travel_time_gijon is None, "an estimate reached a column"
        assert travel["targets"]["gijon"]["source"] == "estimate"


class TestThePageSaysWhichItIs:
    def test_the_note_names_every_target_that_was_not_measured(self, app, land):
        land.travel = {
            "api_status": "degraded",
            "checked_at": "2026-08-10T00:00:00+00:00",
            "targets": {
                "oviedo": {"source": "google", "time_min": 55},
                "gijon": {"source": "estimate", "time_min": 42},
                "airport": {"source": "unavailable"},
            },
        }
        db.session.commit()

        body = app.test_client().get(f"/lands/{land.id}").get_data(as_text=True)

        assert "travel-measurement-note" in body
        assert "answered only part of the last run" in body
        assert "42min estimated" in body
        assert "not measured" in body

    def test_a_fully_measured_run_says_nothing(self, app, land):
        land.travel = {
            "api_status": "ok",
            "targets": {"oviedo": {"source": "google", "time_min": 55}},
        }
        land.travel_time_oviedo = 55
        db.session.commit()

        body = app.test_client().get(f"/lands/{land.id}").get_data(as_text=True)

        assert "travel-measurement-note" not in body

    def test_a_land_with_no_run_recorded_claims_nothing(self, app, land):
        """The 168 legacy rows: their provenance is unknown, and inventing a
        label for them would be the same defect facing the other way."""
        land.travel_time_oviedo = 55
        db.session.commit()

        body = app.test_client().get(f"/lands/{land.id}").get_data(as_text=True)

        assert "travel-measurement-note" not in body
