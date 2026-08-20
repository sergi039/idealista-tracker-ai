"""A row records which engine measured its drive times, and the page shows it.

Two engines read the same OpenStreetMap roads and disagree about how fast they
are driven. Measured over 30 production pairs before OSRM shipped: the
**distances agree** -- 49.0 vs 49.3 km, 75.4 vs 77.2 -- while OSRM is **26% to
34% slower on motorway runs of 30-75 km**, all five airport pairs. On the
scorer's 10-minutes-is-100-points scale that is up to **25 points off a single
target**, and one row went from 20 points to 0.

The owner's question was the right one: does that mean the existing rows have
to be recomputed? Measured, no -- the median per-target change is **0.0** and
only 7 of 30 targets move by 10 points or more. What it does mean is that a
table holding both is comparing two things, and until this the difference was
**invisible**: `api_status` recorded `origin_accuracy` and nothing about the
engine, so two identical plots 50 km from the airport could score 20 and 0
with nothing on the page to say why.

So the record is the fix and the recomputation is optional. What these tests
pin is that the record is a *record*: taken from what answered, never inferred
from the configuration at read time, and absent -- rather than guessed -- on
the rows measured before it existed.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import osrm_routing
from services.property_travel_service import (
    ENGINE_GOOGLE,
    ENGINE_OSRM,
    PropertyTravelService,
    _RunTally,
)
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.drop_all()


class TestTheRunRecordsWhatAnswered:
    def test_one_engine_is_named(self):
        tally = _RunTally()
        tally.engines.add(ENGINE_OSRM)
        assert tally.summary()["routing_engine"] == ENGINE_OSRM

    def test_no_routing_call_records_no_engine(self):
        """Every target failed at the place stage: naming an engine would name
        one that never ran."""
        assert "routing_engine" not in _RunTally().summary()

    def test_two_engines_in_one_run_are_both_named(self):
        """Not collapsed to one: a run that somehow used both is a fact worth
        keeping, and picking a winner would hide it."""
        tally = _RunTally()
        tally.engines.add(ENGINE_OSRM)
        tally.engines.add(ENGINE_GOOGLE)
        assert tally.summary()["routing_engine"] == sorted([ENGINE_GOOGLE, ENGINE_OSRM])


class TestTheResultCarriesIt:
    def test_the_osrm_path_stamps_its_answers(self, app, monkeypatch):
        monkeypatch.setattr(osrm_routing, "is_enabled", lambda: True)
        monkeypatch.setattr(
            osrm_routing,
            "table",
            lambda origin, destinations, mode="driving": (
                [osrm_routing.RouteLeg(distance_m=51700, duration_s=2880)],
                None,
            ),
        )

        results = PropertyTravelService()._distance_matrix_batch(
            43.35, -5.87, ["43.56,-6.03"], mode="driving"
        )

        assert results[0].engine == ENGINE_OSRM

    def test_an_osrm_refusal_is_still_attributed(self, app, monkeypatch):
        """ "OSRM was asked and would not answer" is as much a fact about the
        row as a duration is."""
        from utils.google_api import GoogleApiFailure

        monkeypatch.setattr(osrm_routing, "is_enabled", lambda: True)
        monkeypatch.setattr(
            osrm_routing,
            "table",
            lambda origin, destinations, mode="driving": (
                None,
                GoogleApiFailure(reason="network_error"),
            ),
        )

        results = PropertyTravelService()._distance_matrix_batch(
            43.35, -5.87, ["43.56,-6.03"], mode="driving"
        )

        assert results[0].failure is not None
        assert results[0].engine == ENGINE_OSRM

    def test_the_google_path_stamps_its_own(self, app, monkeypatch):
        import services.property_travel_service as travel_module

        monkeypatch.setattr(osrm_routing, "is_enabled", lambda: False)

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "rows": [
                        {
                            "elements": [
                                {
                                    "status": "OK",
                                    "distance": {"value": 40000},
                                    "duration": {"value": 2520},
                                }
                            ]
                        }
                    ]
                }

        monkeypatch.setattr(
            travel_module, "request_with_retries", lambda *a, **k: _Response()
        )

        service = PropertyTravelService(google_maps_key="key", google_places_key="key")
        results = service._distance_matrix_batch(
            43.35, -5.87, ["43.56,-6.03"], mode="driving"
        )

        assert results[0].engine == ENGINE_GOOGLE
        assert results[0].duration_s == 2520


class TestThePageSaysIt:
    def _listing(self, app, api_status):
        with app.app_context():
            profile = SearchProfile(name="P", is_active=True, is_default=True)
            db.session.add(profile)
            db.session.commit()
            prop = Property(
                source_email_id=f"engine-{api_status.get('routing_engine', 'none')}",
                title="EngineRowUniqueTitle",
                property_category="land",
                search_profile_id=profile.id,
                location_lat=43.35,
                location_lon=-5.87,
                location_accuracy="precise",
                travel={
                    "targets": {
                        "airport": {
                            "status": "ok",
                            "mode": "driving",
                            "duration_min": 42,
                            "place": {"name": "Aeropuerto de Asturias"},
                        }
                    },
                    "api_status": api_status,
                },
            )
            db.session.add(prop)
            db.session.commit()
            return prop.id

    def test_an_osrm_row_says_so(self, app):
        property_id = self._listing(app, {"state": "ok", "routing_engine": ENGINE_OSRM})
        body = (
            app.test_client().get(f"/properties/{property_id}").get_data(as_text=True)
        )

        # The page really rendered: a template error is flashed and re-rendered,
        # which would pass every "is absent" assertion below on its own.
        assert "EngineRowUniqueTitle" in body
        assert "travel-routing-engine" in body
        assert "OSRM" in body

    def test_a_google_row_says_so(self, app):
        property_id = self._listing(
            app, {"state": "ok", "routing_engine": ENGINE_GOOGLE}
        )
        body = (
            app.test_client().get(f"/properties/{property_id}").get_data(as_text=True)
        )

        assert "EngineRowUniqueTitle" in body
        assert "travel-routing-engine" in body
        assert "Distance Matrix" in body

    def test_a_row_measured_before_this_says_nothing(self, app):
        """The 700+ rows already in the table. Their engine was not recorded,
        and inferring one from today's configuration would be a fact nobody
        established -- the absence is what tells them apart."""
        property_id = self._listing(app, {"state": "ok"})
        body = (
            app.test_client().get(f"/properties/{property_id}").get_data(as_text=True)
        )

        assert "EngineRowUniqueTitle" in body
        assert "travel-routing-engine" not in body


class TestItReachesTheStoredRow:
    """The seam, not the pieces.

    Removing the step that collects engines from the results left every test
    above green -- the summary was still tested with a pre-filled set, and the
    results were still stamped, and nothing asserted that one reached the
    other. That is the shape of defect this repository keeps rediscovering
    (#309), so the whole path gets its own test: a real run, and the record
    read back off the row.
    """

    def _profile(self, enabled=("airport",)):
        """Every preset named explicitly, because absent means *enabled*.

        `bool(preset_cfg.get("enabled", True))` -- so a config listing only the
        airport leaves the other five on, and the hospital is answered from the
        national register rather than from OSM, which no stub of the OSM lookup
        can silence. The first version of the no-routing test below was routed
        to a hospital 1 km away for exactly that reason.
        """
        from services.search_profile_service import TRAVEL_PRESET_DEFS

        profile = SearchProfile(
            name="Norte",
            is_active=True,
            is_default=True,
            travel_targets={
                "presets": {
                    key: {"enabled": key in enabled, "mode": "driving"}
                    for key in TRAVEL_PRESET_DEFS
                },
                "custom": [],
            },
        )
        db.session.add(profile)
        db.session.commit()
        return profile

    def _listing(self, profile):
        prop = Property(
            source_email_id="engine-end-to-end",
            title="EndToEndEngineRow",
            property_category="land",
            search_profile_id=profile.id,
            location_lat=43.35,
            location_lon=-5.87,
            location_accuracy="precise",
        )
        db.session.add(prop)
        db.session.commit()
        return prop

    def _answer_places(self, monkeypatch):
        import services.osm_places as osm_places

        monkeypatch.setattr(
            osm_places,
            "lookup_candidates",
            lambda service, specs, lat, lon: (
                {
                    key: (
                        [
                            {
                                "name": "Aeropuerto de Asturias",
                                "lat": 43.5636,
                                "lon": -6.0348,
                                "distance_m": 30000,
                                "source": "osm",
                            }
                        ]
                        if key == "airport"
                        else []
                    )
                    for key in specs
                },
                None,
            ),
        )

    def test_a_run_writes_the_engine_onto_the_row(self, app, monkeypatch):
        with app.app_context():
            self._answer_places(monkeypatch)
            monkeypatch.setattr(osrm_routing, "is_enabled", lambda: True)
            monkeypatch.setattr(
                osrm_routing,
                "table",
                lambda origin, destinations, mode="driving": (
                    [
                        osrm_routing.RouteLeg(distance_m=51700, duration_s=2880)
                        for _ in destinations
                    ],
                    None,
                ),
            )

            prop = self._listing(self._profile())
            assert PropertyTravelService().calculate_for_property(prop, commit=True)

            assert prop.travel["api_status"]["routing_engine"] == ENGINE_OSRM
            assert prop.travel["targets"]["airport"]["duration_min"] == 48

    def test_a_run_that_never_routed_records_no_engine(self, app, monkeypatch):
        """Overpass answered with nothing, so no routing call was made: the row
        must not claim an engine that never ran."""
        import services.osm_places as osm_places

        with app.app_context():
            monkeypatch.setattr(
                osm_places,
                "lookup_candidates",
                lambda service, specs, lat, lon: ({key: [] for key in specs}, None),
            )
            monkeypatch.setattr(osrm_routing, "is_enabled", lambda: True)

            def _never(*args, **kwargs):
                raise AssertionError("routing was called with nothing to route to")

            monkeypatch.setattr(osrm_routing, "table", _never)

            prop = self._listing(self._profile(enabled=()))
            PropertyTravelService().calculate_for_property(prop, commit=True)

            assert "routing_engine" not in prop.travel["api_status"]
