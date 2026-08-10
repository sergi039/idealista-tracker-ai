"""The legacy land page gets the property page's header treatment (2026-08-09).

`/lands/<id>` still renders the 168 frozen legacy rows, and it carried the same
two shapes the property page just lost: a full-width Location panel holding two
numbers and three labelled links, rendered twice (once per branch of "do we have
a coordinate"), and a manual status override.

What replaced them, exactly as on `/properties/<id>`:

* The coordinate reads in the header with a copy button; Google Maps / Idealista
  / Our Maps are icon links next to the title, once each.
* "Mark Status" / "Mark as Active" are gone, and so is the "Re-check Status"
  button that only appeared inside the removed/sold banner. One "Check status"
  button stands in the actions row instead: always reachable, and it asks
  Idealista rather than overriding it by hand.
* "View on Idealista" is gone from the actions row -- that same link is the
  Idealista icon in the header now, and this repo's rule for the property
  surface is that every control exists exactly once.
"""

from decimal import Decimal

import pytest

from app import create_app, db
from models import Land
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


@pytest.fixture
def land(app):
    with app.app_context():
        row = Land(
            source_email_id="land_page_actions",
            title="LandActionsFixtureTitle",
            municipality="Cudillero",
            land_type="developed",
            listing_status="active",
            description="A legacy plot with a view",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            location_lat=Decimal("43.560000"),
            location_lon=Decimal("-6.150000"),
            url="https://www.idealista.com/inmueble/98765432/",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


@pytest.fixture
def land_without_coordinates(app):
    """The other branch of the old Location panel: municipality only."""
    with app.app_context():
        row = Land(
            source_email_id="land_page_actions_no_coords",
            title="LandNoCoordinatesFixtureTitle",
            municipality="Cudillero",
            land_type="developed",
            listing_status="active",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


@pytest.fixture
def removed_land(app):
    """A removed listing renders the banner that used to carry its own
    re-check button."""
    with app.app_context():
        row = Land(
            source_email_id="land_page_actions_removed",
            title="LandRemovedFixtureTitle",
            municipality="Cudillero",
            land_type="developed",
            listing_status="removed",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


class TestTheLocationPanelIsGone:
    def test_the_panel_itself_is_gone(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert "overview-location" not in body
        assert 'aria-label="Location links"' not in body
        assert "Our Maps</a>" not in body
        assert "Google Maps</a>" not in body

    def test_the_three_links_are_icons_next_to_the_title(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        icons = body[body.index('class="detail-link-icons') :]
        icons = icons[: icons.index("</h1>")]
        assert 'aria-label="Google Maps"' in icons
        assert 'aria-label="Idealista"' in icons
        assert 'aria-label="Our Maps"' in icons
        assert "https://www.google.com/maps/search/?api=1&amp;query=43.56" in icons
        assert "https://www.idealista.com/inmueble/98765432/" in icons
        assert f"/map?focus={land}" in icons

    def test_each_link_exists_exactly_once(self, client, land):
        """Two panel branches rendered the same three links, and a third copy of
        the Idealista one sat in the actions row as "View on Idealista"."""
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert body.count('aria-label="Google Maps"') == 1
        assert body.count('aria-label="Idealista"') == 1
        assert body.count('aria-label="Our Maps"') == 1
        assert "View on Idealista" not in body

    def test_a_land_without_coordinates_still_maps_its_municipality(
        self, client, land_without_coordinates
    ):
        body = client.get(f"/lands/{land_without_coordinates}").get_data(as_text=True)
        assert "https://www.google.com/maps/search/Cudillero,+Spain" in body
        # Nothing to copy, so no copy button over an empty string.
        assert "data-copy-text" not in body
        # The municipality the panel used to show is still on screen twice.
        assert body.count("Cudillero") >= 2

    def test_the_coordinate_reads_in_the_header_with_a_copy_button(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert 'data-copy-text="43.560000, -6.150000"' in body
        # In the header, above the description card.
        assert body.index("data-copy-text") < body.index("Property Description")

    def test_an_unrecorded_accuracy_reads_as_unknown(self, client, land):
        """Same as the property page: an unrecorded accuracy is itself worth
        reading, because it is why a sea-view verdict can come back unknown.
        The fixture leaves the column at its "unknown" default."""
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert "(unknown)" in body

    def test_a_recorded_accuracy_is_printed(self, client, app):
        with app.app_context():
            row = Land(
                source_email_id="land_page_actions_precise",
                title="LandPreciseFixtureTitle",
                municipality="Cudillero",
                land_type="developed",
                listing_status="active",
                location_lat=Decimal("43.560000"),
                location_lon=Decimal("-6.150000"),
                location_accuracy="approximate",
            )
            db.session.add(row)
            db.session.commit()
            land_id = row.id
        body = client.get(f"/lands/{land_id}").get_data(as_text=True)
        assert "(approximate)" in body

    def test_a_failed_copy_is_reported_rather_than_faked(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert "Could not copy" in body
        assert "execCommand" in body


class TestTheManualStatusOverrideIsGone:
    """Removed on 2026-08-09, asked for back on 2026-08-10 once idealista's
    DataDome block made every "Check status" answer `error` -- see the property
    page's copy of this class for the measurement. It comes back in the header,
    marked `manual`, without stamping listing_last_checked.
    """

    def test_the_old_actions_row_controls_stayed_gone(self, client, land):
        """The dropdown that stood among the actions at the bottom, and the
        "Mark as Active" button that replaced it for a removed row."""
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert "></i>Mark Status" not in body
        assert "Mark as Removed" not in body
        assert "Mark as Sold" not in body
        assert "Mark as Active" not in body

    def test_the_control_is_one_dropdown_beside_the_map_icons(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        icons = body[body.index('class="detail-link-icons') :]
        icons = icons[: icons.index("</h1>")]
        assert body.count('id="set-status-btn"') == 1
        assert 'id="set-status-btn"' in icons
        for value in ("active", "removed", "sold"):
            assert f"setListingStatus({land}, '{value}')" in icons

    def test_it_posts_to_the_json_api_and_says_idealista_is_not_asked(
        self, client, land
    ):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        handler = body[body.index("async function setListingStatus") :]
        assert "/api/land/${landId}/set-status" in handler
        assert "Idealista is not consulted" in handler

    def test_a_removed_land_can_be_put_back(self, client, removed_land):
        """The banner still renders, and the row is not stuck: `active` is one
        of the three the dropdown offers."""
        body = client.get(f"/lands/{removed_land}").get_data(as_text=True)
        assert "no longer available on Idealista" in body
        assert f"setListingStatus({removed_land}, 'active')" in body


class TestCheckStatusIsOneButton:
    def test_it_is_an_icon_beside_the_map_links_and_appears_once(self, client, land):
        """It moved out of the actions row and up to the title, next to Google
        Maps / Idealista / Our Maps (owner decision, 2026-08-09)."""
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert body.count('id="check-listing-status-btn"') == 1
        assert "/api/land/${landId}/check-status" in body
        icons = body[body.index('class="detail-link-icons') :]
        icons = icons[: icons.index("</h1>")]
        assert 'id="check-listing-status-btn"' in icons

    def test_the_ai_analyses_shortcut_is_gone(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert "></i>AI Analyses" not in body, "the labelled button"
        assert "function openAiSection" not in body
        assert 'onclick="openAiSection' not in body

    def test_the_banner_no_longer_carries_its_own_copy(self, client, removed_land):
        """It used to be the only way to re-check, and only for removed or sold
        listings; a second call site would also have driven that element by id
        instead of itself."""
        body = client.get(f"/lands/{removed_land}").get_data(as_text=True)
        assert 'id="recheck-btn"' not in body
        assert "Re-check Status" not in body
        assert body.count('id="check-listing-status-btn"') == 1

    def test_the_button_passes_itself_to_the_handler(self, client, land):
        body = client.get(f"/lands/{land}").get_data(as_text=True)
        assert f"checkListingStatus({land}, this)" in body
        assert "async function checkListingStatus(landId, btn)" in body
