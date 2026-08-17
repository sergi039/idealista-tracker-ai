"""A listing with no coordinate says so, instead of drawing an empty measurement.

Measured on production 2026-08-17: the "Manus AI" subscription holds 36 rows
imported from the owner's research sheet, and every one has `location_lat`
NULL and an empty `travel` block -- by design, since no portal pin stands
behind those rows (`utils/import_research_sheet.py`). The list drew them with
a marker beside the municipality in the Coords column and a dash per preset in
Travel, ending in "+3 more". That is indistinguishable from a listing whose
targets *were* looked up and came back empty, which is #98 in the two columns
the owner reads first -- and it is why the missing beach line read as "no
beach near this plot" rather than "no request was ever made".

The beach line itself needs no change: beaches ride in the presets' own
Distance Matrix batch, so a row with no origin has no `beaches` key and the
macro already renders nothing for one. What was missing is the reason.

The state is `effective_travel_state`'s, not the stored block's, for the
reason the approximate-origin rule already gives: the row's coordinate
outranks whatever the last run wrote.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_travel_service import (
    TRAVEL_STATE_NOT_LOCATED,
    effective_travel_state,
)
from tests import setup_test_environment

# The dash branch of the Travel macro renders this in its tooltip and nothing
# else does, so it is how the tests tell "a preset row was drawn" from "no
# preset row was drawn" without matching a caption that also appears in the
# page's sort dropdown.
DASH_ROW_MARKER = "not calculated"

MEASURED_TRAVEL = {
    "origin": {"lat": 43.551663, "lon": -6.831426},
    "targets": {
        "airport": {
            "duration_min": 21,
            "distance_km": 30.2,
            "mode": "driving",
            "place": {"name": "Asturias Airport", "lat": 43.56, "lon": -6.03},
        }
    },
    "api_status": {"state": "ok"},
}


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


def _add_property(app, **overrides):
    """One listing in one subscription with every preset column enabled."""
    with app.app_context():
        profile = SearchProfile.query.first()
        if profile is None:
            profile = SearchProfile(
                name="Manus AI",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add(profile)
            db.session.commit()
        fields = {
            "source_email_id": f"research_sheet:{overrides.get('title', 'row')}",
            "title": "SheetRowUniqueTitle",
            "municipality": "Gozón",
            "search_profile_id": profile.id,
            "listing_status": "active",
            "property_category": "land",
            "property_subtype": "plot",
            "price": 50000,
            "area": 3192,
            "score_total": 92.0,
        }
        fields.update(overrides)
        prop = Property(**fields)
        db.session.add(prop)
        db.session.commit()
        return prop.id


class TestTheState:
    def test_a_row_with_no_coordinate_is_not_located(self, app):
        with app.app_context():
            prop = Property(source_email_id="s1", title="t", listing_status="active")
            assert effective_travel_state(prop) == TRAVEL_STATE_NOT_LOCATED

    def test_a_stored_run_does_not_outrank_a_missing_coordinate(self, app):
        """#350's orphaned block describes a point the row can no longer name."""
        with app.app_context():
            prop = Property(
                source_email_id="s2",
                title="t",
                listing_status="active",
                travel={"targets": {}, "api_status": {"state": "ok"}},
            )
            assert effective_travel_state(prop) == TRAVEL_STATE_NOT_LOCATED

    def test_a_located_row_keeps_its_own_verdict(self, app):
        """The new state must fire on the coordinate, not on an empty block."""
        with app.app_context():
            prop = Property(
                source_email_id="s3",
                title="t",
                listing_status="active",
                location_lat=43.55,
                location_lon=-6.83,
                location_accuracy="approximate",
            )
            assert effective_travel_state(prop) == "approximate_origin"


class TestTheList:
    def test_the_travel_cell_says_why_it_is_empty(self, app, client):
        _add_property(app)
        body = client.get("/properties").get_data(as_text=True)
        # The page really rendered: a template error is flashed and re-rendered
        # with no rows, which would pass every "is absent" assertion below.
        assert "SheetRowUniqueTitle" in body
        assert "Not located" in body

    def test_no_preset_row_is_drawn_for_a_row_with_no_origin(self, app, client):
        _add_property(app)
        body = client.get("/properties").get_data(as_text=True)
        assert "SheetRowUniqueTitle" in body
        assert DASH_ROW_MARKER not in body
        # Six presets are enabled, three are drawn, so the macro's own
        # "+3 more" is what a suppressed remainder would still promise.
        assert "+3 more" not in body

    def test_a_measured_row_still_shows_its_times(self, app, client):
        _add_property(
            app,
            location_lat=43.551663,
            location_lon=-6.831426,
            location_accuracy="precise",
            travel=MEASURED_TRAVEL,
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "21min" in body
        assert "Not located" not in body

    def test_an_approximate_row_keeps_its_own_wording(self, app, client):
        """Two different absences; one label for both would be the defect back."""
        _add_property(
            app,
            location_lat=43.551663,
            location_lon=-6.831426,
            location_accuracy="approximate",
            travel=MEASURED_TRAVEL,
        )
        body = client.get("/properties").get_data(as_text=True)
        assert "Approximate location" in body
        assert "Not located" not in body

    def test_the_coords_cell_no_longer_shows_a_bare_marker(self, app, client):
        """A marker beside a town name reads as a coordinate; it was not one."""
        _add_property(app)
        body = client.get("/properties").get_data(as_text=True)
        assert "SheetRowUniqueTitle" in body
        assert "Coordinates unavailable" not in body
        # The municipality still shows -- it is a real fact from the advert.
        assert "Gozón" in body


class TestThePropertyPage:
    def test_the_travel_card_carries_the_notice(self, app, client):
        property_id = _add_property(app)
        body = client.get(f"/properties/{property_id}").get_data(as_text=True)
        assert "SheetRowUniqueTitle" in body
        assert "travel-not-located-notice" in body

    def test_a_located_row_gets_no_notice(self, app, client):
        property_id = _add_property(
            app,
            location_lat=43.551663,
            location_lon=-6.831426,
            location_accuracy="precise",
            travel=MEASURED_TRAVEL,
        )
        body = client.get(f"/properties/{property_id}").get_data(as_text=True)
        assert "SheetRowUniqueTitle" in body
        assert "travel-not-located-notice" not in body
