"""A plot's distance to the shore is a verdict, and an unmeasured one says so.

Asturias forbids a dwelling within 500 m of the coast (POLA/PESC, over the top
of the municipal PGOU), so for a *plot* the shoreline distance decides whether
anything can be built at all. The list therefore renders it as a badge.

The assertion that matters is the third one. A row nobody has measured must
carry an explicit "unmeasured" badge -- not nothing. Rendering nothing is what
turns "we never looked" into "far from the sea", which is #98's mistake with a
legal consequence attached: the reader would shortlist a plot that cannot hold
a house.

Houses are deliberately excluded. The ban is on *building*; an existing legal
house inside the band is not what it decides, so painting one red would be a
different lie.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

BAN = "Coastal ban zone"
OUTSIDE = "Outside coastal ban"
UNMEASURED = "Coast distance unmeasured"
NO_COAST_NEAR = "No coast within radius"
# The approximate-origin wording (services/coordinate_quality.py) starts with
# the same text as UNMEASURED, so a plain substring check cannot tell them
# apart -- the exact string is what proves the badge names the reason.
UNMEASURED_APPROXIMATE = "Coast distance unmeasured (approximate location)"


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


@pytest.fixture
def profile(app):
    with app.app_context():
        profile = SearchProfile(
            name="Plots",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        return profile.id


def _add(
    profile_id,
    key,
    *,
    sea=None,
    category="land",
    attributes=None,
    location_accuracy="precise",
):
    """One listing. `sea` None means the measurement never ran.

    `location_accuracy` defaults to `precise` so a plain `_add(..., sea=...)`
    keeps behaving exactly as before #358: the badge reads the raw distance
    at face value only for a coordinate Google matched to an address. Tests
    for the approximate-origin case (services/coordinate_quality.py) pass
    `location_accuracy="approximate"` explicitly.
    """
    enrichment = {}
    if sea is not None:
        enrichment["sea"] = dict(sea)
    db.session.add(
        Property(
            source_email_id=key,
            title=key,
            municipality="Cudillero",
            search_profile_id=profile_id,
            listing_status="active",
            property_category=category,
            property_subtype="plot" if category == "land" else "house",
            price=60000,
            area=1300,
            location_lat=43.5723,
            location_lon=-6.2123,
            location_accuracy=location_accuracy,
            enrichment=enrichment or None,
            attributes=attributes,
        )
    )
    db.session.commit()


def _body(client):
    return client.get("/properties").get_data(as_text=True)


class TestCoastalBadge:
    def test_inside_the_band_is_flagged_as_a_ban(self, client, app, profile):
        with app.app_context():
            _add(profile, "inside", sea={"status": "ok", "distance_m": 108.5})
        body = _body(client)
        assert BAN in body
        assert OUTSIDE not in body

    def test_outside_the_band_is_not_flagged_as_a_ban(self, client, app, profile):
        with app.app_context():
            _add(profile, "outside", sea={"status": "ok", "distance_m": 1892.4})
        body = _body(client)
        assert OUTSIDE in body
        assert BAN not in body

    def test_an_unmeasured_plot_says_so_rather_than_rendering_nothing(
        self, client, app, profile
    ):
        """The one that stops a never-measured plot reading as a safe one."""
        with app.app_context():
            _add(profile, "unmeasured", sea=None)
        body = _body(client)
        assert UNMEASURED in body
        assert OUTSIDE not in body
        assert BAN not in body

    def test_a_refusal_is_not_reported_as_a_distance(self, client, app, profile):
        """`unavailable` is the absence of an answer, not a far shoreline."""
        with app.app_context():
            _add(profile, "refused", sea={"status": "unavailable"})
        body = _body(client)
        assert UNMEASURED in body
        assert OUTSIDE not in body

    def test_a_measured_absence_is_allowed_to_pass(self, client, app, profile):
        """No coastline within the search radius *is* an answer, unlike a refusal."""
        with app.app_context():
            _add(profile, "nocoast", sea={"status": "no_coastline_within_radius"})
        body = _body(client)
        assert "No coast within radius" in body
        assert BAN not in body

    def test_a_house_gets_no_coastal_badge(self, client, app, profile):
        """The ban is on building, so an existing house is not judged by it."""
        with app.app_context():
            _add(
                profile,
                "house",
                sea={"status": "ok", "distance_m": 108.5},
                category="housing",
            )
        body = _body(client)
        assert BAN not in body
        assert OUTSIDE not in body
        assert UNMEASURED not in body


class TestApproximateOriginCoastalBadge:
    """The coordinate is a locality centroid (#358): a raw distance from it is
    not a fact about the parcel, so the badge must not paint the ban colours
    off a number the 5 km slack cannot stand behind.
    """

    def test_a_precise_230m_plot_is_still_flagged_as_a_ban(self, client, app, profile):
        """The precise path is unchanged -- #360's original assertion, restated
        at 230 m so it sits next to its approximate-origin counterpart below.
        """
        with app.app_context():
            _add(
                profile,
                "precise-230",
                sea={"status": "ok", "distance_m": 230},
                location_accuracy="precise",
            )
        body = _body(client)
        assert BAN in body
        assert OUTSIDE not in body
        assert UNMEASURED_APPROXIMATE not in body

    def test_an_approximate_230m_plot_is_not_flagged_as_a_ban(
        self, client, app, profile
    ):
        """230 m measured from a centroid: within the 5 km slack the true
        parcel distance could be anywhere from 0 to 5230 m, so 500 m falls
        inside the bounds on both sides. Neither colour is defensible -- the
        badge must say "unmeasured", not guess red off the raw number.
        """
        with app.app_context():
            _add(
                profile,
                "approx-230",
                sea={"status": "ok", "distance_m": 230},
                location_accuracy="approximate",
            )
        body = _body(client)
        assert UNMEASURED_APPROXIMATE in body
        assert BAN not in body
        assert OUTSIDE not in body

    def test_an_approximate_plot_with_no_coastline_in_17km_stays_green(
        self, client, app, profile
    ):
        """The stored search covered 17 km around the centroid
        (services/sea_distance_service.SEARCH_RADIUS_M); shrunk by the 5 km
        slack that still guarantees 12 km *around the parcel* -- well past
        the 500 m the ban cares about, so "no coastline nearby" survives the
        restatement and stays green rather than falling back to unmeasured.
        """
        with app.app_context():
            _add(
                profile,
                "approx-nocoast",
                sea={"status": "no_coastline_within_radius", "searched_m": 17000},
                location_accuracy="approximate",
            )
        body = _body(client)
        assert NO_COAST_NEAR in body
        assert BAN not in body
        assert UNMEASURED_APPROXIMATE not in body


class TestClassificationFlags:
    def test_listing_conflict_is_surfaced(self, client, app, profile):
        with app.app_context():
            _add(
                profile,
                "conflict",
                sea={"status": "ok", "distance_m": 1892.4},
                attributes={"classification_conflict": "text says finca rural"},
            )
        assert "Listing contradicts itself" in _body(client)

    def test_pgou_zone_warning_is_surfaced(self, client, app, profile):
        with app.app_context():
            _add(
                profile,
                "pgou",
                sea={"status": "ok", "distance_m": 1892.4},
                attributes={"pgou_zone_warning": "Pillarno is not a PGOU urban zone"},
            )
        assert "Outside PGOU urban zones?" in _body(client)

    def test_a_wrong_coordinate_is_surfaced(self, client, app, profile):
        with app.app_context():
            _add(
                profile,
                "badgeo",
                attributes={"geocoding_failure": "geocoded to Barcelona"},
            )
        assert "Coordinate is wrong" in _body(client)

    def test_a_price_outlier_is_surfaced(self, client, app, profile):
        """Price per m2 is the cheapest check on the portal's own class field."""
        with app.app_context():
            _add(
                profile,
                "cheap",
                sea={"status": "ok", "distance_m": 1892.4},
                attributes={"price_per_m2_outlier": "3.2 EUR/m2 — bottom decile"},
            )
        assert "Price per m² far below the norm" in _body(client)

    def test_an_ordinary_price_is_not_flagged(self, client, app, profile):
        with app.app_context():
            _add(profile, "normal", sea={"status": "ok", "distance_m": 1892.4})
        assert "Price per m² far below the norm" not in _body(client)

    def test_a_resolved_coordinate_is_no_longer_flagged(self, client, app, profile):
        """A warning that outlives its problem is one nobody reads."""
        with app.app_context():
            _add(
                profile,
                "fixedgeo",
                attributes={"geocoding_failure_resolved": "was Barcelona, now right"},
            )
        assert "Coordinate is wrong" not in _body(client)
