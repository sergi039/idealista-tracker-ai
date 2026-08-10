"""The word "none" under "Sea view unknown" on the property page.

`read_verdict()` answers the literal string `"none"` when no source ever
computed a verdict, and the template printed that value straight out. On 193
of the owner's 356 listings the Environment card therefore read:

    Sea view            [Sea view unknown]
    none

which is an internal code leaking into the page -- and one a reader takes for
a verdict, when it means the opposite: nobody looked. The four-state contract
in `services/sea_view_service.py` is explicit that `unknown` is "could not be
computed" and must never read as `no`.
"""

import re

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.sea_view_service import read_verdict  # noqa: E402


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


def _listing(key, enrichment=None):
    prop = Property(
        source_email_id=f"sea-source-{key}",
        title=f"SeaSourceFixture {key}",
        municipality="Gijón",
        location_lat=43.529796,
        location_lon=-5.665516,
    )
    if enrichment is not None:
        prop.enrichment = enrichment
    db.session.add(prop)
    db.session.commit()
    return prop.id


def _environment_card(body):
    start = body.index("environment-display")
    return body[start : start + 1200]


class TestAnUncomputedVerdictSaysSo:
    def test_the_page_does_not_print_the_word_none(self, app, client):
        listing = _listing("uncomputed")

        card = _environment_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "Not computed yet" in card
        # The old template rendered the source as a bare text node between the
        # badge and the closing div, which is exactly `>none<` once the
        # whitespace is collapsed.
        assert ">none<" not in re.sub(r"\s+", "", card), (
            "the literal source code must not reach the page"
        )

    def test_the_state_itself_is_still_unknown(self, app, client):
        """`unknown` is the verdict; "not computed" is why. Both are shown."""
        listing = _listing("state")

        card = _environment_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "Sea view unknown" in card

    def test_read_verdict_still_reports_none_to_its_callers(self, app):
        """The API contract is unchanged; only the page stopped printing it."""
        prop = db.session.get(Property, _listing("contract"))

        assert read_verdict(prop)["source"] == "none"


class TestARealSourceIsStillShown:
    def test_geometry_keeps_its_provenance_and_reason(self, app, client):
        listing = _listing(
            "geometry",
            {
                "environment": {
                    "sea_view": "no",
                    "sea_view_detail": {
                        "source": "geometry",
                        "reason": "terrain_blocks_line_of_sight",
                        "geometry": {
                            "distance_m": 523.0,
                            "observer_elevation_m": 91.0,
                        },
                    },
                }
            },
        )

        card = _environment_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "geometry" in card
        assert "terrain_blocks_line_of_sight" in card
        assert "0.5 km to the coastline" in card
        assert "91 m above sea level" in card
        assert "Not computed yet" not in card

    def test_a_hand_set_verdict_names_itself(self, app, client):
        listing = _listing(
            "manual",
            {
                "environment": {
                    "sea_view": "yes",
                    "sea_view_detail": {"source": "manual", "reason": "owner checked"},
                }
            },
        )

        card = _environment_card(
            client.get(f"/properties/{listing}").get_data(as_text=True)
        )

        assert "manual" in card
        assert "owner checked" in card
        assert "Not computed yet" not in card
