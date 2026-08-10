"""Extended Infrastructure must not answer the question Travel Times answers.

Found on `/properties/79` after the travel recalculation landed:

    Travel Times              Supermarket   Comercial Alonso        11min  6.9km
                              School        Escuela rural de Viodo   3min
    Extended Infrastructure   Supermarket Distance                   7min  4.7km
                              School Distance                        1min  0.4km

Different shop, different school, different numbers, one page. Travel Times
reads `travel["targets"]`, rewritten by every enrichment run; the
infrastructure block was rendering `enrichment.legacy_land
.infrastructure_extended`, a frozen Places snapshot from the old `Land`
import that no recalculation touches. **157 of the 168** mirrored rows
disagreed on the supermarket, by up to 21 minutes, and 146 on the school.

The distances and times are gone from that block. What stays is what Travel
Times does not say: whether the amenity exists, and the OSM counts.
`/lands/<id>` keeps its copy -- `Land` has no travel targets, so there the
block is the only source.
"""

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Land, Property  # noqa: E402


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


# Verbatim from property 79.
LEGACY_INFRASTRUCTURE = {
    "cafe_distance": 4377.717596331556,
    "cafe_available": True,
    "cafe_travel_time": 7,
    "school_distance": 426.42362764176,
    "school_available": True,
    "school_travel_time": 1,
    "hospital_available": False,
    "supermarket_distance": 4699.839543335818,
    "supermarket_available": True,
    "supermarket_travel_time": 7,
    "osm_amenities": {"restaurant": 2},
}
FRESH_TARGETS = {
    "targets": {
        "supermarket": {
            "kind": "preset",
            "status": "ok",
            "duration_min": 11,
            "distance_km": 6.903,
            "place": {"name": "Comercial Alonso"},
        }
    }
}


def _listing(key, infrastructure, travel=None):
    prop = Property(
        source_email_id=f"infra-{key}",
        title=f"InfraFixture {key}",
        municipality="Gozón",
        location_lat=43.636917,
        location_lon=-5.851910,
    )
    prop.enrichment = {"legacy_land": {"infrastructure_extended": infrastructure}}
    if travel is not None:
        prop.travel = travel
    db.session.add(prop)
    db.session.commit()
    return prop.id


def _infrastructure_card(body):
    """The block's markup, up to whichever card follows it on this fixture."""
    start = body.index("Extended Infrastructure")
    ends = [
        body.index(marker, start)
        for marker in ("Transport", "Environment", "Services Quality")
        if marker in body[start:]
    ]
    return body[start : min(ends)] if ends else body[start:]


class TestTheBlockStopsRestatingTravel:
    def test_the_frozen_distances_are_gone(self, app, client):
        listing = _listing("frozen", LEGACY_INFRASTRUCTURE, FRESH_TARGETS)

        card = _infrastructure_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "Supermarket Distance" not in card
        assert "Supermarket Travel Time" not in card
        assert "School Distance" not in card
        assert "School Travel Time" not in card
        assert "4.7km" not in card, "the stale figure must not survive anywhere"

    def test_availability_and_osm_counts_stay(self, app, client):
        """That is the part Travel Times never answers."""
        listing = _listing("stays", LEGACY_INFRASTRUCTURE, FRESH_TARGETS)

        card = _infrastructure_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "Supermarket Available" in card
        assert "Hospital Available" in card
        assert "Restaurants" in card, "the OSM amenity counts are the point of #152"

    def test_travel_times_still_names_the_measured_place(self, app, client):
        listing = _listing("travel", LEGACY_INFRASTRUCTURE, FRESH_TARGETS)

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "Comercial Alonso" in body
        assert "11min" in body

    def test_a_property_with_only_counts_is_unaffected(self, app, client):
        """The 188 non-legacy rows never had the frozen keys to begin with."""
        listing = _listing("counts", {"osm_amenities": {"restaurant": 2, "school": 7}})

        card = _infrastructure_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "Restaurants" in card
        assert "Schools" in card


class TestTheLandPageKeepsItsOwnCopy:
    def test_the_legacy_page_still_shows_its_distances(self, app, client):
        """`Land` has no travel targets: this block is its only source."""
        land = Land(
            source_email_id="infra-land",
            title="InfraLandFixture",
            municipality="Gozón",
            infrastructure_extended=LEGACY_INFRASTRUCTURE,
        )
        db.session.add(land)
        db.session.commit()

        body = client.get(f"/lands/{land.id}").get_data(as_text=True)

        assert "Supermarket Distance" in body
