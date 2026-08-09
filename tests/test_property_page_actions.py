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
