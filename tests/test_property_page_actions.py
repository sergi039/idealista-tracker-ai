"""What the property page offers the owner (decisions of 2026-08-09).

Two changes, both asked for after looking at the real page:

* The "Profile assignment" and "Classification" editors are gone. They came
  with the original universal-tracker import, nobody asked for them, and both
  duplicate what ingestion and the profile rules already decide.
* Enrichment is **one** button, at the top. It used to be three -- "Enrich
  with Google APIs", "Recalculate travel", "Recalculate scoring" -- sitting
  mid-page, and the last two are steps `enrich_property()` performs anyway,
  so pressing them separately paid Google twice for the same answer.

And one more of the same kind (2026-08-09): that button also runs both AI
providers. "Run Claude" and "Run ChatGPT" were two further buttons, in two
tabs, that the owner had to find and press after the enrichment they depend
on -- Claude and ChatGPT read the scores and travel times the enrichment pass
writes, so running them by hand meant analysing whatever was there before.

Two more, same day, same reason -- the page spent height on things that are
one line and one icon:

* The Location panel is gone. Its coordinate reads in the header metadata
  line with a copy button, and its Google Maps / Idealista / Our Maps buttons
  are icon links next to the title. The panel rendered them twice, once per
  branch of "do we have a coordinate".
* The manual status override is gone -- the dropdown and its "Set status"
  submit, and the POST handler behind them. "Check status" asks Idealista;
  a hand-set status is the one thing that check cannot correct.
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


@pytest.fixture
def listing(app):
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        prop = Property(
            source_email_id="property_page_actions",
            title="ActionsFixtureUniqueTitle",
            search_profile_id=profile.id,
            listing_status="active",
            municipality="Cudillero",
            description="A plot with a view",
            location_lat=43.56,
            location_lon=-6.15,
            location_accuracy="approximate",
            url="https://www.idealista.com/inmueble/112239547/",
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


@pytest.fixture
def listing_without_coordinates(app):
    """The other branch of the old Location panel: no coordinate, only a
    municipality."""
    with app.app_context():
        prop = Property(
            source_email_id="property_page_actions_no_coords",
            title="NoCoordinatesFixtureTitle",
            listing_status="active",
            municipality="Cudillero",
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


class TestTheUnaskedForEditorsAreGone:
    def test_no_profile_assignment_editor(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "Profile assignment" not in body

    def test_no_classification_editor(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "Auto classify" not in body
        assert "Lock (skip bulk reclassify)" not in body

    @pytest.mark.parametrize("path", ["profile", "classification"])
    def test_their_endpoints_are_gone_too(self, client, listing, path):
        """Removing the form but leaving the POST behind would keep a
        state-changing endpoint on an app that has no authentication."""
        resp = client.post(f"/properties/{listing}/{path}", data={})
        assert resp.status_code == 404

    def test_the_page_still_renders(self, client, listing):
        resp = client.get(f"/properties/{listing}")
        assert resp.status_code == 200
        assert "ActionsFixtureUniqueTitle" in resp.get_data(as_text=True)


class TestEnrichIsOneButton:
    def test_one_enrich_button_and_it_is_in_the_header(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert body.count('id="enrich-property-btn"') == 1
        # Above the fold: before the description card, not buried mid-page.
        assert body.index('id="enrich-property-btn"') < body.index(
            "Property Description"
        )

    def test_it_posts_to_the_enrichment_endpoint(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        button = body[body.index('id="enrich-property-btn"') :][:600]
        assert f"/api/property/{listing}/enrich" in button

    def test_the_separate_recalculations_are_gone(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "Recalculate travel" not in body
        assert "Recalculate scoring" not in body

    @pytest.mark.parametrize("path", ["travel/recalculate", "score/recalculate"])
    def test_their_endpoints_are_gone_too(self, client, listing, path):
        resp = client.post(f"/properties/{listing}/{path}", data={})
        assert resp.status_code == 404


class TestTheOneButtonRunsBothAiProviders:
    def test_the_per_provider_run_buttons_are_gone(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert 'id="ai-claude-run-btn"' not in body
        assert 'id="ai-chatgpt-run-btn"' not in body
        assert "Run Claude" not in body
        assert "Run ChatGPT" not in body

    def test_the_enrich_button_drives_the_whole_pass(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        button = body[body.index('id="enrich-property-btn"') :][:600]
        assert f"runEnrichAndAnalyze({listing})" in button

    def test_the_pass_calls_google_then_both_providers(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        pass_body = body[body.index("async function runEnrichAndAnalyze") :]
        pass_body = pass_body[: pass_body.index("async function runClaudeAnalysis")]
        assert "runGoogleEnrichment(propertyId)" in pass_body
        assert "runClaudeAnalysis(propertyId)" in pass_body
        assert "generateChatGPTAnalysis(propertyId)" in pass_body
        # Google first: the AI prompts read the scores and travel times it writes.
        assert pass_body.index("runGoogleEnrichment(propertyId)") < pass_body.index(
            "runClaudeAnalysis(propertyId)"
        )

    def test_chatgpt_is_skipped_rather_than_billed_when_unconfigured(
        self, client, listing
    ):
        """AI_BRIDGE_TOKEN is unset in the test environment, so the page tells
        the pass to run Claude only instead of firing a call that can only
        fail."""
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "window.__OPENAI_CONFIGURED__ = false;" in body
        assert "if (window.__OPENAI_CONFIGURED__) {" in body

    def test_the_empty_tabs_point_at_that_button(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert body.count("Press “Enrich” at the top of the page to generate it.") == 2


class TestTheLocationPanelIsGone:
    """The Location panel is now a coordinate in the header metadata line plus
    three icon links next to the title (owner decision, 2026-08-09). It carried
    a labelled button group and a full-width card of its own for two numbers.
    """

    def test_the_panel_itself_is_gone(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "overview-location" not in body
        assert 'aria-label="Location links"' not in body
        # The labels went with it; the links did not.
        assert "Our Maps</a>" not in body
        assert "Google Maps</a>" not in body

    def test_the_three_links_are_icons_next_to_the_title(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        icons = body[body.index('class="detail-link-icons') :]
        icons = icons[: icons.index("</h1>")]
        assert 'aria-label="Google Maps"' in icons
        assert 'aria-label="Idealista"' in icons
        assert 'aria-label="Our Maps"' in icons
        assert "https://www.google.com/maps/search/?api=1&amp;query=43.56" in icons
        assert "https://www.idealista.com/inmueble/112239547/" in icons
        assert f"/map?focus={listing}" in icons

    def test_each_link_exists_exactly_once_on_the_page(self, client, listing):
        """Two panels used to render the same three links in two branches."""
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert body.count('aria-label="Google Maps"') == 1
        assert body.count('aria-label="Idealista"') == 1
        assert body.count('aria-label="Our Maps"') == 1

    def test_a_property_without_coordinates_still_maps_its_municipality(
        self, client, listing_without_coordinates
    ):
        body = client.get(f"/properties/{listing_without_coordinates}").get_data(
            as_text=True
        )
        assert "https://www.google.com/maps/search/Cudillero,+Spain" in body
        # No coordinate means nothing to copy, not a copy button over an
        # empty string.
        assert "data-copy-text" not in body

    def test_the_coordinate_reads_in_the_header_with_a_copy_button(
        self, client, listing
    ):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert 'data-copy-text="43.560000, -6.150000"' in body
        assert body.count("copy-coords-btn") >= 1
        # Accuracy travels with the coordinate: "approximate" is why a sea-view
        # verdict can come back unknown.
        assert "(approximate)" in body
        # And it is in the header, above the Overview card.
        assert body.index("copy-coords-btn") < body.index("Overview")

    def test_a_failed_copy_is_reported_rather_than_faked(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "Could not copy" in body
        assert "execCommand" in body


def _with_address(address, municipality="Cudillero", key="addr"):
    """A listing whose enrichment pass resolved an address, as 188 real ones did."""
    prop = Property(
        source_email_id=f"property_page_address_{key}",
        title=f"AddressFixture-{key}",
        listing_status="active",
        municipality=municipality,
        location_lat=43.56,
        location_lon=-6.15,
        location_accuracy="approximate",
        enrichment={
            "geocoding": {
                "query": "whatever the email said",
                "accuracy": "approximate",
                "formatted_address": address,
            }
        },
    )
    db.session.add(prop)
    db.session.commit()
    return prop.id


class TestTheGeocodedAddressIsShown:
    """The header knew the coordinate and not the place.

    `enrichment.geocoding.formatted_address` is written by the enrichment pass
    for every listing the geocoder resolves -- 188 of the owner's 356 -- and
    was read by nothing. The page showed `43.529796, -5.665516 (approximate)`
    and a municipality, so the one line naming where the listing actually is
    ("El Llano, Gijón, Asturias, Spain") never reached the screen.
    """

    def test_the_address_reads_in_the_header(self, app, client):
        listing = _with_address("El Llano, Gijón, Asturias, Spain", "Gijón")

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "El Llano, Gijón, Asturias, Spain" in body
        assert body.index("El Llano") < body.index("Overview"), "header, not a panel"

    def test_the_municipality_is_not_repeated_when_the_address_carries_it(
        self, app, client
    ):
        listing = _with_address(
            "El Llano, Gijón, Asturias, Spain", "Gijón", key="dedupe"
        )

        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        header = body[: body.index("Overview")]

        assert header.count("fa-city") == 0, (
            "the address already says Gijón; a second badge is the duplication "
            "the header refactor set out to remove"
        )

    def test_a_differently_named_municipality_still_shows(self, app, client):
        """Google answers in the local language: two names, one place.

        60 of the owner's 188 geocoded listings are like this -- the filters
        and the profile rules use `municipality`, so it cannot silently vanish
        behind a Catalan or Asturian rendering of the same town.
        """
        listing = _with_address(
            "Carrer l'Ordana, 03550 Sant Joan d'Alacant, Alicante, Spain",
            "San Juan de Alicante",
            key="localname",
        )

        body = client.get(f"/properties/{listing}").get_data(as_text=True)

        assert "Sant Joan d&#39;Alacant" in body or "Sant Joan d'Alacant" in body
        assert "San Juan de Alicante" in body

    def test_no_geocoding_means_no_address_line_and_no_invention(self, client, listing):
        """The other 168: a coordinate, and nothing a person would recognise."""
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        header = body[: body.index("Overview")]

        assert "fa-map-pin" not in header, (
            "an unresolved address is absent, never reconstructed from the coordinate"
        )
        assert "Cudillero" in header, "the municipality is all there is, so it stays"


class TestGeocodedAddressAccessor:
    def test_it_reads_the_enrichment_the_pass_writes(self, app):
        prop = Property(source_email_id="addr-accessor-1")
        prop.enrichment = {"geocoding": {"formatted_address": " Bergondo, Spain "}}

        assert prop.geocoded_address == "Bergondo, Spain"

    @pytest.mark.parametrize(
        "enrichment",
        [
            None,
            {},
            {"geocoding": {}},
            {"geocoding": {"formatted_address": None}},
            {"geocoding": {"formatted_address": "   "}},
            {"geocoding": {"formatted_address": {"not": "a string"}}},
            {"geocoding": "not a dict"},
        ],
    )
    def test_anything_but_a_usable_string_is_none(self, app, enrichment):
        prop = Property(source_email_id="addr-accessor-2")
        prop.enrichment = enrichment

        assert prop.geocoded_address is None


class TestTheManualStatusOverrideIsGone:
    """ "Check status" asks Idealista. The dropdown next to it set the status by
    hand, which the scraper then could not correct."""

    def test_the_form_is_gone(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert "Set status" not in body
        assert 'aria-label="Set listing status"' not in body

    def test_the_endpoint_is_gone_too(self, client, listing):
        resp = client.post(f"/properties/{listing}/set-status", data={"status": "sold"})
        assert resp.status_code == 404

    def test_check_status_stayed(self, client, listing):
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        assert 'id="check-listing-status-btn"' in body
        assert f"checkPropertyListingStatus({listing})" in body
        assert "/api/property/${propertyId}/check-status" in body

    def test_the_two_buttons_share_one_row(self, client, listing):
        """AI Analyses and Check status were two stacked rows of one button."""
        body = client.get(f"/properties/{listing}").get_data(as_text=True)
        row = body[: body.index('id="check-listing-status-btn"')]
        row = row[row.rindex('<div class="d-flex') :]
        assert "AI Analyses" in row
