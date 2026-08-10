"""A travel target that found nothing must not take the page down with it.

`/properties/266` and `/properties/269` answered 302 to `/properties`. They were
not missing: `property_detail` caught an exception and redirected. The exception
was

    jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'distance_km'

raised from the travel card. `tdata.distance_km` on a dict *without* that key is
Undefined rather than None, and `Undefined is not none` is true, so the guard let
it through to `format()`.

The dicts that have no such key are the refusal records the travel service
writes: `{"status": "not_found", "reason": ...}` with no place, no duration and
no distance. Counted on the live database 2026-08-10: **34 of 356 properties**
carried one, and every one of their detail pages was unreachable.

Two things are pinned here: the page renders, and the refusal is shown rather
than silently skipped -- "not found" (Google answered that there is no such
place) and "not measured" (the lookup never answered) are different facts, and
neither is a travel time of zero.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


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


def make_property(key, targets):
    profile = SearchProfile.query.first()
    if profile is None:
        profile = SearchProfile(
            name="Travel targets",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

    prop = Property(
        source_email_id=f"travel-refusal-{key}",
        title="A property with an awkward travel target",
        municipality="Foz",
        search_profile_id=profile.id,
        location_lat=43.56,
        location_lon=-7.24,
        travel={"targets": targets, "api_status": "ok"},
    )
    db.session.add(prop)
    db.session.commit()
    return prop


REFUSED_AIRPORT = {
    "kind": "preset",
    "enabled": True,
    "mode": "driving",
    "label": "Nearest airport",
    "status": "not_found",
    "reason": "no_candidate_within_radius",
}
MEASURED_STATION = {
    "kind": "preset",
    "enabled": True,
    "mode": "driving",
    "label": "Nearest train station",
    "status": "ok",
    # Deliberately not the municipality: asserting on "Foz" would have passed
    # on the header alone and proved nothing about the place-name path.
    "place": {"name": "Estación de Burela"},
    "duration_min": 21,
    "distance_km": 21.5,
}


class TestTheRefusalDoesNotBreakThePage:
    def test_the_page_renders_instead_of_redirecting(self, app, client):
        """The whole defect in one assertion: this used to be a 302."""
        prop = make_property("not-found", {"airport": REFUSED_AIRPORT})

        resp = client.get(f"/properties/{prop.id}")

        assert resp.status_code == 200

    def test_a_measured_target_beside_it_still_reads(self, app, client):
        prop = make_property(
            "mixed", {"airport": REFUSED_AIRPORT, "train_station": MEASURED_STATION}
        )

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "21min" in body
        assert "21.5km" in body
        assert "Estación de Burela" in body, "the measured place, named"

    def test_a_distance_in_metres_still_converts(self, app, client):
        """The other branch of the guard that was reading Undefined."""
        prop = make_property(
            "metres",
            {
                "hospital": {
                    "kind": "preset",
                    "enabled": True,
                    "label": "Nearest hospital",
                    "status": "ok",
                    "duration_min": 9,
                    "distance_m": 2700,
                }
            },
        )

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "2.7km" in body


class TestTheRefusalIsShown:
    def test_not_found_says_not_found(self, app, client):
        prop = make_property("shown", {"airport": REFUSED_AIRPORT})

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        # The rendered badge, not the raw string: the page also serialises the
        # whole travel blob into a script variable, so a bare search for the
        # status or the reason passes without anything being displayed.
        assert ">not found</span>" in body
        assert 'title="Google found no such place near this listing' in body
        assert "(no_candidate_within_radius)" in body, "the reason, on hover"

    def test_unavailable_is_not_the_same_as_not_found(self, app, client):
        """A lookup that never answered measured nothing; saying "not found"
        would report an absence nobody established (#98)."""
        prop = make_property(
            "unavailable",
            {
                "airport": {
                    "kind": "preset",
                    "enabled": True,
                    "label": "Nearest airport",
                    "status": "unavailable",
                    "reason": "quota_exhausted",
                }
            },
        )

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert ">not measured</span>" in body
        assert 'title="The lookup did not answer, so nothing was measured' in body
        assert "(quota_exhausted)" in body

    def test_a_disabled_target_stays_hidden(self, app, client):
        """The owner switched it off; that is not a refusal to report.

        The record carries `not_found` on purpose: with `status: disabled` the
        macro renders nothing anyway, so the test would stay green even if the
        `enabled == false` guard were deleted. This shape pins the guard.
        """
        prop = make_property(
            "disabled",
            {
                "airport": {
                    "kind": "preset",
                    "enabled": False,
                    "label": "Nearest airport",
                    "status": "not_found",
                    "reason": "no_candidate_within_radius",
                }
            },
        )

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert ">not found</span>" not in body
        assert 'title="Google found no such place near this listing' not in body

    def test_the_barest_refusal_record_is_enough(self, app, client):
        """Nothing but a status and a reason -- no kind, enabled, mode or label.

        The richer fixtures above could have been carrying the row past a guard
        that reads one of those keys; this one cannot.
        """
        prop = make_property(
            "bare", {"airport": {"status": "not_found", "reason": "no_candidate"}}
        )

        resp = client.get(f"/properties/{prop.id}")

        assert resp.status_code == 200
        assert ">not found</span>" in resp.get_data(as_text=True)

    def test_a_target_that_is_not_even_a_dict_cannot_break_the_page(self, app, client):
        """Defensive: the same shape test that guards `travel` above."""
        prop = make_property("junk", {"airport": "not_found"})

        assert client.get(f"/properties/{prop.id}").status_code == 200

    def test_zero_distance_is_a_measurement_not_a_blank(self, app, client):
        prop = make_property(
            "zero",
            {
                "supermarket": {
                    "kind": "preset",
                    "enabled": True,
                    "label": "Nearest supermarket",
                    "status": "ok",
                    "duration_min": 0,
                    "distance_m": 0,
                }
            },
        )

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "0min" in body
        assert "0.0km" in body
