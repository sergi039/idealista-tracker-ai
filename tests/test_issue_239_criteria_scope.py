"""Issue #239: /criteria said it rescored properties; it rescores lands.

The page is titled "Dual Scoring System Configuration" and its Update button
reported "all properties rescored successfully". It writes `ScoringCriteria`
rows — `development_potential`, `infrastructure_basic`, `legal_status` and the
rest — which only the legacy `ScoringService` reads, and it rescores `Land`.

`PropertyScoringService`, which scores everything on `/properties`, never looks
at those rows: its criteria are a different set entirely (value, size, travel,
sea), configured per subscription in `scoring_config`. So the owner dragged the
sliders, was told every property had been rescored, and every listing kept the
score it had.

These tests pin what the page says. **They do not pin that it stays that way**:
whether global criteria should reach property scoring, and how the two
vocabularies would map, is a design decision for the owner — see the issue.
"""

import pytest

from app import create_app, db
from models import Land, Property, SearchProfile
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
def rows(app):
    for index in range(3):
        db.session.add(Land(source_email_id=f"criteria-land-{index}", title="A plot"))
    profile = SearchProfile(
        name="Houses in Asturias",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()
    db.session.add(
        Property(
            source_email_id="criteria-prop",
            title="Land plot in Siero",
            property_category="land",
            search_profile_id=profile.id,
        )
    )
    db.session.commit()


class TestThePageSaysWhatItCovers:
    def test_it_names_the_legacy_rows_and_points_at_the_real_one(self, client, rows):
        body = client.get("/criteria").get_data(as_text=True)

        assert "criteria-scope-note" in body
        assert "3 legacy land listings" in body
        assert "/properties" in body
        assert "Scoring Config" in body, (
            "the owner needs to be told where the live scores are configured"
        )

    def test_the_update_message_does_not_claim_the_properties(self, client, rows):
        body = client.post(
            "/criteria/update_profile/investment",
            data={"weight_location_quality": "1.0"},
            follow_redirects=True,
        ).get_data(as_text=True)

        assert "all properties rescored" not in body, (
            "this is the sentence that sent the owner looking for a change "
            "that was never going to be there"
        )
        assert "legacy land listings rescored" in body

    def test_the_page_still_renders_with_no_rows_at_all(self, client, app):
        body = client.get("/criteria").get_data(as_text=True)

        assert "criteria-scope-note" in body
        assert "0 legacy land listings" in body
