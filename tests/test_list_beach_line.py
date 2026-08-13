"""The list's Travel cell carries the nearest beach — in three honest states.

Proposal D1 (approved 2026-08-13): a measured beach renders as a route link
with minutes; a *measured* absence (none_within_limit, or not_found — Places
answered empty) says "no beach ≤ N min"; a lookup that never answered
(`unavailable`) reads "not measured". A row with no beaches key predates the
feature and shows nothing. Collapsing those states is the #98 defect, so each
one is pinned here, as is the D24 link contract: the official
`dir/?api=1&…destination_place_id=` form, never the old free-text-name path.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

BEACH_ITEMS = [
    {
        "name": "Playa de Riboira",
        "place_id": "ChIJbeachRiboira000000",
        "lat": 43.5601,
        "lon": -6.8302,
        "duration_min": 4,
        "distance_m": 1500,
        "distance_km": 1.5,
        "estimated": False,
    },
    {
        "name": "Cambaredo Beach",
        "place_id": "ChIJbeachCambaredo0000",
        "lat": 43.5622,
        "lon": -6.8203,
        "duration_min": 5,
        "distance_m": 1500,
        "distance_km": 1.5,
        "estimated": False,
    },
]


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _add_property(app, travel):
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        db.session.add(
            Property(
                source_email_id="beach_line",
                title="BeachLineUniqueTitle",
                municipality="El Franco",
                search_profile_id=profile.id,
                listing_status="active",
                property_category="land",
                property_subtype="plot",
                price=99000,
                area=2600,
                location_lat=43.551663,
                location_lon=-6.831426,
                score_total=66.5,
                travel=travel,
            )
        )
        db.session.commit()


def _travel(beaches):
    return {
        "origin": {"lat": 43.551663, "lon": -6.831426},
        "targets": {},
        "beaches": beaches,
        "api_status": {"state": "ok"},
    }


class TestMeasuredBeach:
    def test_nearest_beach_renders_as_a_route_link(self, app, client):
        _add_property(
            app,
            _travel({"status": "ok", "max_drive_min": 20, "items": BEACH_ITEMS}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "fa-umbrella-beach" in body
        assert "4min" in body
        assert "Playa de Riboira" in body

    def test_link_is_the_official_directions_form(self, app, client):
        _add_property(
            app,
            _travel({"status": "ok", "max_drive_min": 20, "items": BEACH_ITEMS}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "https://www.google.com/maps/dir/?api=1" in body
        assert "destination_place_id=ChIJbeachRiboira000000" in body

    def test_second_nearest_lives_in_the_tooltip(self, app, client):
        _add_property(
            app,
            _travel({"status": "ok", "max_drive_min": 20, "items": BEACH_ITEMS}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "Cambaredo Beach, 5min" in body

    def test_estimated_time_carries_a_tilde(self, app, client):
        items = [dict(BEACH_ITEMS[0], estimated=True)]
        _add_property(
            app,
            _travel({"status": "ok", "max_drive_min": 20, "items": items}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "~4min" in body


class TestMeasuredAbsence:
    def test_none_within_limit_says_so(self, app, client):
        _add_property(
            app,
            _travel({"status": "none_within_limit", "max_drive_min": 20, "items": []}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "no beach ≤ 20 min" in body

    def test_not_found_reads_the_same_as_none_within_limit(self, app, client):
        _add_property(
            app,
            _travel({"status": "not_found", "max_drive_min": 20, "items": []}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "no beach ≤ 20 min" in body


class TestRefusalAndLegacy:
    def test_unavailable_reads_not_measured_never_absence(self, app, client):
        _add_property(
            app,
            _travel({"status": "unavailable", "stage": "places", "items": []}),
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "did not answer" in body
        assert "no beach" not in body

    def test_row_without_beaches_key_shows_nothing(self, app, client):
        _add_property(
            app,
            {
                "origin": {"lat": 43.551663, "lon": -6.831426},
                "targets": {},
                "api_status": {"state": "ok"},
            },
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "fa-umbrella-beach" not in body

    def test_no_travel_at_all_still_renders_the_row(self, app, client):
        _add_property(app, None)
        body = client.get("/properties").get_data(as_text=True)
        assert "BeachLineUniqueTitle" in body
        assert "fa-umbrella-beach" not in body
