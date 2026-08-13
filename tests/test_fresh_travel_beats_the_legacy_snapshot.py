"""One page, one answer: the Transport card must not contradict Travel Times.

Found by reading `/properties/79` after the full travel recalculation:

    Travel Times & Distances    Nearest Airport   Asturias Airport  41min  44.1km
    Transport                   Airport Distance                    35min  31km

Two cards, one listing, two answers. Travel Times reads `travel["targets"]`,
which the recalculation rewrote; Transport reads
`Property.travel_time_airport`, which preferred
`enrichment.legacy_land.travel_time_airport` -- a frozen snapshot from the old
`Land` model that no recalculation touches, measured to a destination nobody
recorded. All **168** mirrored rows disagreed, by 4.6 minutes on average and
6 at worst.

The precedence is now the other way round: a target present in `travel` wins,
including when its value is `None`. "We looked and found nothing" is a result;
a number nobody can reproduce is not.
"""

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402


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


# Verbatim from property 79 in the owner's database.
LEGACY_SNAPSHOT = {
    "legacy_land": {
        "travel_time_airport": 35,
        "distance_airport": 31,
        "travel_time_police": 32,
        "distance_police": 7.5,
    }
}
FRESH_TARGETS = {
    "targets": {
        "airport": {
            "kind": "preset",
            "status": "ok",
            "duration_min": 41,
            "distance_km": 44.095,
            "place": {"name": "Asturias Airport"},
        }
    }
}


def _listing(key, enrichment=None, travel=None):
    prop = Property(
        source_email_id=f"fresh-vs-legacy-{key}",
        title=f"FreshVsLegacy {key}",
        municipality="Gozón",
        location_lat=43.636917,
        location_lon=-5.851910,
    )
    if enrichment is not None:
        prop.enrichment = enrichment
    if travel is not None:
        prop.travel = travel
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheRecalculatedValueWins:
    def test_a_fresh_measurement_outranks_the_legacy_snapshot(self, app):
        prop = _listing("both", LEGACY_SNAPSHOT, FRESH_TARGETS)

        assert prop.travel_time_airport == 41
        assert prop.distance_airport == 44.1

    def test_the_legacy_snapshot_still_answers_where_nothing_was_measured(self, app):
        """Police was never in this run's targets, so the old value is all there is."""
        prop = _listing("police", LEGACY_SNAPSHOT, FRESH_TARGETS)

        assert prop.travel_time_police == 32
        assert prop.distance_police == 7.5

    def test_a_target_that_found_nothing_still_wins(self, app):
        """34 listings have no airport within reach. That is an answer."""
        prop = _listing(
            "notfound",
            LEGACY_SNAPSHOT,
            {"targets": {"airport": {"kind": "preset", "status": "not_found"}}},
        )

        assert prop.travel_time_airport is None, (
            "a recorded 'nothing qualifies' must not fall back to an "
            "unreproducible legacy number"
        )
        assert prop.distance_airport is None

    def test_a_row_with_no_legacy_at_all_is_unaffected(self, app):
        prop = _listing("fresh-only", None, FRESH_TARGETS)

        assert prop.travel_time_airport == 41
        assert prop.distance_airport == 44.1

    def test_a_row_with_neither_reports_nothing(self, app):
        prop = _listing("empty")

        assert prop.travel_time_airport is None
        assert prop.distance_airport is None


class TestThePageAgreesWithItself:
    def test_the_page_quotes_fresh_travel_never_the_snapshot(self, app, client):
        """The Transport card is gone (proposal D12, 2026-08-13), so the page
        can no longer disagree with itself — but the frozen legacy snapshot
        must still never resurface anywhere on it."""
        listing = _listing("page", LEGACY_SNAPSHOT, FRESH_TARGETS)

        body = client.get(f"/properties/{listing.id}").get_data(as_text=True)

        assert "Airport Distance" not in body, "the Transport card stays gone"
        assert "41min" in body, "the page must quote the recalculated value"
        assert "35min" not in body, "the frozen snapshot must not resurface"
        assert "Asturias Airport" in body, "Travel Times still names the place"
