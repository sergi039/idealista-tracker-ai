"""Issue #235: an analysis says which price it was computed from.

The prompt is built from the listing's price, and on 2026-08-11 twenty-two
prices were corrected (#220, the €/m² defect). Those stored analyses had been
computed against the wrong number — both providers said so in their own text —
and the page presented them as current, with no way to tell an analysis
computed at €309 from one computed at €99,000.

Re-running costs money and stays the owner's decision. Saying what was analysed
costs nothing, and that is what is pinned here: the variant records the price it
was given, and the page compares it with the price the listing carries now —
except when nothing was recorded, where it compares nothing at all.
"""

import json
from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, PropertyAiAnalysisVariant
from routes.api_routes import _upsert_property_ai_variant
from tests import setup_test_environment

ANALYSIS = {"price_analysis": {"verdict": "UNDERPRICED", "summary": "Cheap."}}


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
def prop(app):
    prop = Property(
        source_email_id="issue-235",
        title="Detached house in Barrio de Prendonés",
        price=Decimal("99000.00"),
        area=Decimal("320.00"),
        property_category="housing",
    )
    prop.ai_analysis = ANALYSIS
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheWriterRecordsThePrice:
    def test_an_insert_records_it(self, app, prop):
        _upsert_property_ai_variant(
            prop.id,
            "claude",
            model="claude-test",
            analysis=ANALYSIS,
            price_at_analysis=prop.price,
        )
        db.session.commit()

        variant = PropertyAiAnalysisVariant.query.one()
        assert variant.price_at_analysis == Decimal("99000.00")

    def test_an_update_overwrites_it_rather_than_leaving_the_old_one(self, app, prop):
        """A re-run at a new price must not keep the old figure."""
        _upsert_property_ai_variant(
            prop.id, "claude", model="m", analysis=ANALYSIS, price_at_analysis=309
        )
        db.session.commit()

        _upsert_property_ai_variant(
            prop.id, "claude", model="m", analysis=ANALYSIS, price_at_analysis=99000
        )
        db.session.commit()

        variant = PropertyAiAnalysisVariant.query.one()
        assert variant.price_at_analysis == Decimal("99000.00")

    def test_a_caller_that_cannot_say_records_nothing(self, app, prop):
        """None is "not recorded", never a stand-in for the current price."""
        _upsert_property_ai_variant(prop.id, "claude", model="m", analysis=ANALYSIS)
        db.session.commit()

        assert PropertyAiAnalysisVariant.query.one().price_at_analysis is None


class TestTheEndpointReportsIt:
    def _compare(self, client, prop):
        return json.loads(client.get(f"/api/property/{prop.id}/analysis/compare").data)

    def test_it_sends_both_prices(self, app, client, prop):
        db.session.add(
            PropertyAiAnalysisVariant(
                property_id=prop.id,
                provider="claude",
                model="claude-test",
                analysis=ANALYSIS,
                price_at_analysis=Decimal("309.00"),
            )
        )
        db.session.commit()

        data = self._compare(client, prop)

        assert data["current_price"] == 99000.0
        assert data["claude_price_at_analysis"] == 309.0
        assert data["chatgpt_price_at_analysis"] is None

    def test_a_variant_from_before_this_change_reports_none(self, app, client, prop):
        db.session.add(
            PropertyAiAnalysisVariant(
                property_id=prop.id,
                provider="claude",
                model="claude-test",
                analysis=ANALYSIS,
            )
        )
        db.session.commit()

        data = self._compare(client, prop)

        assert data["claude_price_at_analysis"] is None, (
            "every analysis stored before #235 is unknown, not up to date"
        )

    def test_a_listing_with_no_price_reports_none(self, app, client, prop):
        prop.price = None
        db.session.commit()

        assert self._compare(client, prop)["current_price"] is None


def test_the_page_compares_the_two_prices():
    """The note lives in the one status line, not in a second copy of it."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "templates" / "property_detail.html"
    ).read_text(encoding="utf-8")

    assert "claude_price_at_analysis" in source
    assert "chatgpt_price_at_analysis" in source
    assert source.count("Priced before the current") == 1, (
        "one status line, not a second copy of the same note"
    )
    assert "press Enrich to re-run" in source, (
        "the page must not re-run a paid analysis on its own"
    )
