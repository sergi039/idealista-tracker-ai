"""Beaches within a short drive: the block, and what may reach it.

The owner asked for the beaches a listing can actually be at -- 20 minutes by
car, no further -- linked to Google Maps, at the top of the right-hand column.
Two things make that harder than a list of places:

* A beach is not a travel preset. Presets resolve one place and feed the
  scorer; this list holds every beach within the limit and must move no score,
  so a beach lookup Google refuses may not turn a good travel run into a
  degraded one.
* "No beach within 20 minutes" and "we could not find out" look identical once
  a block is hidden. That is the #98 defect in miniature, so the four statuses
  stay apart and only a *measured* absence hides the block.
"""

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.property_travel_service import (  # noqa: E402
    _BEACH_CANDIDATE_RADIUS_M,
    DistanceResult,
    PropertyTravelService,
)
from utils.google_api import GoogleApiFailure  # noqa: E402


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
def client(app):
    return app.test_client()


# Verbatim shapes from the Places Nearby Search reply, trimmed to what the
# service reads. Coordinates are real beaches on the Asturian coast.
def _place(name, lat, lon, place_id, types=None):
    return {
        "name": name,
        "place_id": place_id,
        "types": types or ["natural_feature", "point_of_interest"],
        "geometry": {"location": {"lat": lat, "lng": lon}},
    }


RODILES = _place("Playa de Rodiles", 43.531, -5.383, "PLACE_RODILES")
ESPANA = _place("Playa de España", 43.549, -5.502, "PLACE_ESPANA")
# 64 km west in a straight line: no road covers that in 20 minutes, so it must
# never cost a Distance Matrix element.
FAR_AWAY = _place("Playa del Silencio", 43.564, -6.187, "PLACE_SILENCIO")

ORIGIN = (43.520, -5.400)


class _Recorder:
    """Stands in for both Google calls, and remembers what was asked."""

    def __init__(self, beaches=None, failure=None, durations=None):
        self.beaches = beaches if beaches is not None else []
        self.failure = failure
        self.durations = durations or {}
        self.measured_keys = []

    def places_nearby(self, lat, lon, place_type, keyword=None):
        if keyword == "playa":
            if self.failure is not None:
                return [], self.failure
            return list(self.beaches), None
        # Every preset answers "nothing of this type nearby": a real answer, so
        # the run stays `ok` and only the beaches are under test here.
        return [], None

    def get_distances(self, lat, lon, destinations, mode):
        results = []
        for destination in destinations:
            key = destination["key"]
            self.measured_keys.append(key)
            entry = self.durations.get(key)
            if entry is None:
                results.append(DistanceResult())
                continue
            if isinstance(entry, DistanceResult):
                results.append(entry)
                continue
            distance_m, duration_min = entry
            results.append(
                DistanceResult(distance_m=distance_m, duration_s=duration_min * 60)
            )
        return results


def _service(monkeypatch, recorder):
    service = PropertyTravelService(
        google_maps_key="test-maps-key", google_places_key="test-places-key"
    )
    monkeypatch.setattr(service, "_places_nearby", recorder.places_nearby)
    monkeypatch.setattr(service, "_get_distances", recorder.get_distances)
    return service


def _listing(key, lat=ORIGIN[0], lon=ORIGIN[1], travel=None):
    prop = Property(
        source_email_id=f"beach-{key}",
        title=f"BeachFixture {key}",
        municipality="Villaviciosa",
        location_lat=lat,
        location_lon=lon,
    )
    if travel is not None:
        prop.travel = travel
    db.session.add(prop)
    db.session.commit()
    return prop


def _run(app, monkeypatch, recorder, prop):
    service = _service(monkeypatch, recorder)
    ok = service.calculate_for_property(prop, commit=True)
    return ok, (prop.travel or {}).get("beaches") or {}


class TestOnlyBeachesWithinTheLimitAreKept:
    def test_a_beach_over_the_limit_is_dropped_and_the_others_stay(
        self, app, monkeypatch
    ):
        recorder = _Recorder(
            beaches=[RODILES, ESPANA],
            durations={"beach:0": (5200, 7), "beach:1": (18400, 24)},
        )
        _, beaches = _run(app, monkeypatch, recorder, _listing("limit"))

        assert beaches["status"] == "ok"
        assert [item["name"] for item in beaches["items"]] == ["Playa de Rodiles"]
        assert beaches["max_drive_min"] == 20

    def test_the_list_is_ordered_by_drive_time(self, app, monkeypatch):
        recorder = _Recorder(
            beaches=[RODILES, ESPANA],
            durations={"beach:0": (9000, 14), "beach:1": (5200, 6)},
        )
        _, beaches = _run(app, monkeypatch, recorder, _listing("order", lat=43.521))

        assert [item["duration_min"] for item in beaches["items"]] == [6, 14]

    def test_every_beach_within_the_limit_is_kept_not_just_the_nearest(
        self, app, monkeypatch
    ):
        """The owner asked for all of them, not a top-N (2026-08-11)."""
        beaches_found = [
            _place(f"Playa {index}", 43.525 + index / 1000.0, -5.401, f"P{index}")
            for index in range(6)
        ]
        recorder = _Recorder(
            beaches=beaches_found,
            durations={
                f"beach:{index}": (1000 * index, index + 3) for index in range(6)
            },
        )
        _, beaches = _run(app, monkeypatch, recorder, _listing("all", lat=43.522))

        assert len(beaches["items"]) == 6

    def test_the_same_beach_under_two_place_ids_is_listed_once(self, app, monkeypatch):
        """Measured against the live API off La Caridad: Google returned "Playa
        de Torbas" twice, 5 and 12 minutes away. The nearer one is the answer;
        the repeat reads as a second place to drive to."""
        twin = _place("Playa de Torbas", 43.545, -5.395, "PLACE_TORBAS_2")
        near = _place("Playa de Torbas", 43.533, -5.390, "PLACE_TORBAS_1")
        recorder = _Recorder(
            beaches=[near, twin],
            durations={"beach:0": (3100, 5), "beach:1": (8000, 12)},
        )
        _, beaches = _run(app, monkeypatch, recorder, _listing("twin", lat=43.5205))

        assert [item["duration_min"] for item in beaches["items"]] == [5]


class TestMoneyIsNotSpentOnBeachesThatCannotQualify:
    def test_a_beach_beyond_the_radius_never_reaches_distance_matrix(
        self, app, monkeypatch
    ):
        recorder = _Recorder(
            beaches=[RODILES, FAR_AWAY], durations={"beach:0": (5200, 7)}
        )
        _, beaches = _run(app, monkeypatch, recorder, _listing("radius", lat=43.523))

        # Only one beach was close enough to be worth a billed element.
        assert recorder.measured_keys.count("beach:0") == 1
        assert "beach:1" not in recorder.measured_keys
        assert beaches["candidates"] == 1
        assert beaches["found"] == 2
        assert beaches["search_radius_m"] == _BEACH_CANDIDATE_RADIUS_M

    def test_a_business_named_after_the_beach_is_refused(self, app, monkeypatch):
        camping = _place(
            "Camping Playa de Rodiles",
            43.532,
            -5.384,
            "PLACE_CAMPING",
            types=["campground", "lodging"],
        )
        recorder = _Recorder(
            beaches=[camping, RODILES], durations={"beach:0": (5200, 7)}
        )
        _, beaches = _run(app, monkeypatch, recorder, _listing("camping", lat=43.524))

        assert [item["name"] for item in beaches["items"]] == ["Playa de Rodiles"]


class TestARefusalIsNeverAnAbsence:
    def test_a_refused_places_lookup_reports_unavailable(self, app, monkeypatch):
        recorder = _Recorder(failure=GoogleApiFailure(reason="over_query_limit"))
        _, beaches = _run(app, monkeypatch, recorder, _listing("refused", lat=43.525))

        assert beaches["status"] == "unavailable"
        assert beaches["items"] == []
        assert beaches["error"] == "over_query_limit"

    def test_an_unmeasured_candidate_is_not_reported_as_none_nearby(
        self, app, monkeypatch
    ):
        recorder = _Recorder(
            beaches=[RODILES],
            durations={
                "beach:0": DistanceResult(
                    failure=GoogleApiFailure(reason="request_failed")
                )
            },
        )
        _, beaches = _run(
            app, monkeypatch, recorder, _listing("unmeasured", lat=43.526)
        )

        assert beaches["status"] == "unavailable"
        assert beaches["unmeasured"] == 1

    def test_google_answering_with_no_beaches_is_a_measured_absence(
        self, app, monkeypatch
    ):
        recorder = _Recorder(beaches=[])
        _, beaches = _run(app, monkeypatch, recorder, _listing("none", lat=43.527))

        assert beaches["status"] == "not_found"
        assert beaches["items"] == []

    def test_beaches_all_too_far_are_a_measured_absence(self, app, monkeypatch):
        recorder = _Recorder(beaches=[RODILES], durations={"beach:0": (30000, 35)})
        _, beaches = _run(app, monkeypatch, recorder, _listing("far", lat=43.528))

        assert beaches["status"] == "none_within_limit"
        assert beaches["items"] == []


class TestBeachesDoNotSwayTheTravelVerdict:
    def test_a_refused_beach_lookup_leaves_the_run_ok(self, app, monkeypatch):
        """No score reads a beach, so one must not degrade the run (#153's rule
        applied the other way round: advisory sources cannot fail a run)."""
        recorder = _Recorder(failure=GoogleApiFailure(reason="over_query_limit"))
        prop = _listing("verdict", lat=43.529)
        ok, beaches = _run(app, monkeypatch, recorder, prop)

        assert ok is True
        assert prop.travel["api_status"]["state"] == "ok"
        assert prop.travel["api_status"]["targets"]["unavailable"] == 0
        assert beaches["status"] == "unavailable"

    def test_beaches_stay_out_of_the_scored_targets(self, app, monkeypatch):
        recorder = _Recorder(beaches=[RODILES], durations={"beach:0": (5200, 7)})
        prop = _listing("targets", lat=43.530)
        _run(app, monkeypatch, recorder, prop)

        assert not any(
            key.startswith("beach:") for key in (prop.travel.get("targets") or {})
        )


class TestTheBlockOnThePropertyPage:
    def _body(self, client, prop):
        return client.get(f"/properties/{prop.id}").get_data(as_text=True)

    def test_it_renders_each_beach_with_a_google_maps_link(self, app, client):
        prop = _listing(
            "page-ok",
            travel={
                "targets": {},
                "beaches": {
                    "status": "ok",
                    "max_drive_min": 20,
                    "items": [
                        {
                            "name": "Playa de Rodiles",
                            "place_id": "PLACE_RODILES",
                            "lat": 43.531,
                            "lon": -5.383,
                            "duration_min": 7,
                            "distance_km": 5.2,
                        }
                    ],
                },
            },
        )
        body = self._body(client, prop)

        assert "Playa de Rodiles" in body
        assert (
            "https://www.google.com/maps/search/?api=1&amp;query=43.531,-5.383"
            "&amp;query_place_id=PLACE_RODILES" in body
        )
        assert "7min" in body and "5.2km" in body

    def test_it_sits_between_dual_scoring_and_travel_times(self, app, client):
        prop = _listing(
            "page-order",
            travel={
                "targets": {},
                "beaches": {
                    "status": "ok",
                    "max_drive_min": 20,
                    "items": [
                        {
                            "name": "Playa de España",
                            "place_id": "PLACE_ESPANA",
                            "lat": 43.549,
                            "lon": -5.502,
                            "duration_min": 12,
                            "distance_km": 9.4,
                        }
                    ],
                },
            },
        )
        prop.score_investment = 78
        prop.score_lifestyle = 83
        db.session.commit()
        body = self._body(client, prop)

        assert (
            body.index("Dual Scoring Analysis")
            < body.index("Beaches ≤")
            < body.index("Travel Times &amp; Distances")
        )

    def test_it_is_absent_when_no_beach_is_within_the_limit(self, app, client):
        prop = _listing(
            "page-far",
            travel={
                "targets": {},
                "beaches": {
                    "status": "none_within_limit",
                    "max_drive_min": 20,
                    "items": [],
                },
            },
        )
        body = self._body(client, prop)

        assert "Beaches ≤" not in body

    def test_it_is_absent_for_a_listing_that_was_never_measured(self, app, client):
        prop = _listing("page-empty", travel={"targets": {}})

        assert "Beaches ≤" not in self._body(client, prop)

    def test_an_unavailable_lookup_still_says_so(self, app, client):
        """Hiding the block here would state "no beach nearby" on the strength
        of a lookup that never answered."""
        prop = _listing(
            "page-unavailable",
            travel={
                "targets": {},
                "beaches": {
                    "status": "unavailable",
                    "max_drive_min": 20,
                    "error": "over_query_limit",
                    "items": [],
                },
            },
        )
        body = self._body(client, prop)

        assert "Beaches ≤" in body
        assert "Not measured" in body
