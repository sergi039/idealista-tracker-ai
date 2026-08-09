"""The "Claude vs ChatGPT" block on `/properties/<id>`, which read half-empty.

Four separate things emptied it, and each one is pinned below.

* The property endpoint scored both providers against a **fabricated**
  baseline of zeroes. Every listing therefore showed "Numeric fidelity 0/100"
  and "Overall 60/100" for both models -- a made-up number presented as a
  measurement, and the #98 mistake again: an absent baseline is not a score of
  zero.
* `numeric_fidelity_score` returned `0.0` when there was nothing to compare,
  so "unmeasured" and "every figure was wrong" were the same value.
* Schema completeness counted every analysis against the **land** schema.
  `PropertyAIService` prompts a different schema per category, so a house was
  marked 7/9 for two sections its prompt never asked for.
* The qualitative fields (`highlights`) were computed, sent to the browser and
  then never rendered on the property page -- `/lands` showed them, the
  surface that holds the listings did not. `best_use` was read only from the
  land schema's `development_ideas`, so it stayed blank for houses anyway.
"""

import json

import pytest

from app import create_app, db
from models import Property, PropertyAiAnalysisVariant
from tests import setup_test_environment
from utils.analysis_compare import (
    build_evaluation,
    extract_highlights,
    numeric_coverage,
    numeric_fidelity_score,
    overall_score,
    schema_completeness,
)


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


def _land_analysis(**rental):
    return {
        "price_analysis": {"verdict": "OVERPRICED", "summary": "Above the comps."},
        "investment_potential": {
            "rating": "MEDIUM",
            "risk_level": "MEDIUM",
            "key_drivers": ["supermarket", "rail", "schools"],
        },
        "risks_analysis": {"major_risks": ["permits"]},
        "development_ideas": {"best_use": "Single-family home"},
        "comparable_analysis": {"market_position": "mid"},
        "similar_objects": {"comparison_summary": "three comparables"},
        "construction_value_estimation": {"minimum_value": 1},
        "market_price_dynamics": {"price_trend": "STABLE"},
        "rental_market_analysis": {"investment_rating": "BELOW_AVERAGE", **rental},
    }


def _housing_analysis(**rental):
    return {
        "price_analysis": {"verdict": "FAIR_PRICE", "summary": "In line with comps."},
        "investment_potential": {
            "rating": "HIGH",
            "risk_level": "LOW",
            "key_drivers": ["beach", "transport"],
        },
        "risks_analysis": {"major_risks": ["community fees"]},
        "renovation_ideas": {"best_improvements": ["kitchen", "windows", "terrace"]},
        "comparable_analysis": {"market_position": "top"},
        "similar_objects": {"comparison_summary": "two comparables"},
        "market_price_dynamics": {"price_trend": "RISING"},
        "rental_market_analysis": {"investment_rating": "GOOD", **rental},
    }


FULL_FIGURES = {
    "rental_yield": 1.9,
    "cap_rate": 1.9,
    "price_to_rent_ratio": 53,
    "payback_period_years": 53,
}
# What GPT-5.6-Terra actually stored for property 356: it declined to price the
# rent on a building plot and said so in `rental_strategy`.
NO_FIGURES = {
    "rental_yield": None,
    "cap_rate": None,
    "price_to_rent_ratio": None,
    "payback_period_years": None,
}


class TestUnmeasuredIsNotZero:
    def test_fidelity_is_none_when_there_is_no_baseline(self):
        metrics = dict(FULL_FIGURES)

        assert numeric_fidelity_score(metrics, None) is None
        assert numeric_fidelity_score(metrics, {}) is None, "an empty baseline"
        assert numeric_fidelity_score(metrics, dict(NO_FIGURES)) is None

    def test_a_baseline_of_zeroes_is_still_no_baseline(self):
        """The exact placeholder the property endpoint used to send."""
        placeholder = {
            "rental_yield": 0,
            "cap_rate": 0,
            "price_to_rent_ratio": 0,
            "payback_period_years": 0,
        }

        assert numeric_fidelity_score(dict(FULL_FIGURES), placeholder) is None

    def test_a_real_baseline_still_scores(self):
        score = numeric_fidelity_score(
            {"rental_yield": 4.0}, {"rental_yield": 4.0, "cap_rate": 3.0}
        )

        assert score == 100.0

    def test_overall_falls_back_to_what_the_provider_filled_in(self):
        """Without a baseline the two providers must still be distinguishable."""
        complete = (9, 9)

        with_figures = overall_score(complete, None, numeric_coverage(FULL_FIGURES))
        without_figures = overall_score(complete, None, numeric_coverage(NO_FIGURES))

        assert with_figures == 100.0
        assert without_figures == 60.0
        assert with_figures > without_figures

    def test_coverage_counts_the_figures_that_carry_a_number(self):
        assert numeric_coverage(FULL_FIGURES) == (4, 4)
        assert numeric_coverage(NO_FIGURES) == (0, 4)
        assert numeric_coverage({"rental_yield": 4.0, "cap_rate": None}) == (1, 4)


class TestSchemaIsCountedPerCategory:
    def test_a_house_is_complete_against_the_housing_schema(self):
        found, total = schema_completeness(_housing_analysis(), "housing")

        assert (found, total) == (8, 8), (
            "the housing prompt never asks for development_ideas "
            "or construction_value_estimation"
        )

    def test_land_keeps_its_nine_sections(self):
        assert schema_completeness(_land_analysis(), "land") == (9, 9)

    def test_the_stored_analysis_outranks_a_changed_category(self):
        """A recategorised listing must not lose sections it was never asked for."""
        assert schema_completeness(_housing_analysis(), "land") == (8, 8)
        assert schema_completeness(_land_analysis(), "housing") == (9, 9)

    def test_an_empty_analysis_scores_nothing(self):
        assert schema_completeness(None, "housing") == (0, 8)
        assert schema_completeness({}, "land") == (0, 9)


class TestHighlightsSurviveEverySchema:
    def test_land_best_use(self):
        assert extract_highlights(_land_analysis())["best_use"] == "Single-family home"

    def test_housing_falls_back_to_its_improvements_list(self):
        highlights = extract_highlights(_housing_analysis())

        assert highlights["best_use"] == "kitchen • windows • terrace", (
            "the housing schema has no best_use; its improvements are the answer"
        )

    def test_generic_usage_ideas(self):
        analysis = {"usage_ideas": {"best_use": "Short-term letting"}}

        assert extract_highlights(analysis)["best_use"] == "Short-term letting"

    def test_the_verdicts_the_property_page_now_renders(self):
        highlights = extract_highlights(_land_analysis())

        assert highlights["price_verdict"] == "OVERPRICED"
        assert highlights["investment_potential_rating"] == "MEDIUM"
        assert highlights["risk_level"] == "MEDIUM"
        assert highlights["market_trend"] == "STABLE"
        assert highlights["key_drivers"] == "supermarket • rail • schools"


class TestEvaluationContract:
    def test_no_baseline_reports_itself_as_unmeasured(self):
        result = build_evaluation(_land_analysis(**FULL_FIGURES), expected=None)

        assert result["fidelity_score"] is None
        assert result["overall_basis"] == "schema+coverage"
        assert result["numeric_coverage"] == {"found": 4, "total": 4}

    def test_a_baseline_switches_the_basis_back(self):
        result = build_evaluation(
            _land_analysis(**FULL_FIGURES),
            expected={"rental_yield": 1.9, "cap_rate": 1.9},
        )

        assert result["fidelity_score"] == 100.0
        assert result["overall_basis"] == "schema+baseline"


def _make_property(**overrides):
    fields = {
        "source_email_id": "ai-compare-1",
        "title": "Buildable plot in Porceyo",
        "municipality": "Gijón",
        "property_category": "land",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestCompareEndpoint:
    def test_it_reports_the_missing_baseline_instead_of_inventing_one(
        self, app, client
    ):
        prop = _make_property()
        prop.ai_analysis = _land_analysis(**FULL_FIGURES)
        db.session.commit()

        data = json.loads(client.get(f"/api/property/{prop.id}/analysis/compare").data)

        assert data["success"] is True
        baseline = data["comparison"]["baseline"]
        assert baseline["available"] is False
        assert baseline["reason"]
        assert data["comparison"]["expected"] is None, (
            "a row of zeroes is a fabricated measurement, not a baseline"
        )
        assert data["comparison"]["claude"]["fidelity_score"] is None

    def test_it_separates_the_provider_that_answered_from_the_one_that_did_not(
        self, app, client
    ):
        prop = _make_property(source_email_id="ai-compare-2")
        prop.ai_analysis = _land_analysis(**FULL_FIGURES)
        db.session.add(
            PropertyAiAnalysisVariant(
                property_id=prop.id,
                provider="openai",
                model="gpt-test",
                analysis=_land_analysis(**NO_FIGURES),
            )
        )
        db.session.commit()

        data = json.loads(client.get(f"/api/property/{prop.id}/analysis/compare").data)
        claude = data["comparison"]["claude"]
        chatgpt = data["comparison"]["chatgpt"]

        assert data["has_claude"] is True
        assert data["has_chatgpt"] is True
        assert claude["numeric_coverage"] == {"found": 4, "total": 4}
        assert chatgpt["numeric_coverage"] == {"found": 0, "total": 4}
        assert claude["overall_score"] > chatgpt["overall_score"], (
            "both used to score 60/100 whatever they answered"
        )

    def test_a_house_is_not_docked_for_the_land_sections(self, app, client):
        prop = _make_property(
            source_email_id="ai-compare-3", property_category="housing"
        )
        prop.ai_analysis = _housing_analysis(**FULL_FIGURES)
        db.session.commit()

        data = json.loads(client.get(f"/api/property/{prop.id}/analysis/compare").data)

        assert data["property_category"] == "housing"
        assert data["comparison"]["claude"]["schema"] == {"found": 8, "total": 8}

    def test_highlights_reach_the_browser(self, app, client):
        prop = _make_property(source_email_id="ai-compare-4")
        prop.ai_analysis = _land_analysis(**FULL_FIGURES)
        db.session.commit()

        data = json.loads(client.get(f"/api/property/{prop.id}/analysis/compare").data)
        highlights = data["comparison"]["claude"]["highlights"]

        assert highlights["price_verdict"] == "OVERPRICED"
        assert highlights["best_use"] == "Single-family home"


class TestPropertyPageRendersTheComparison:
    """The data was always in the response; the page threw most of it away."""

    def test_the_page_draws_the_qualitative_rows(self, app, client):
        prop = _make_property(source_email_id="ai-compare-page")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        for label in (
            "'Price verdict'",
            "'Investment potential'",
            "'Risk level'",
            "'Market trend'",
            "'Price summary'",
            "'Key drivers'",
            "'Best use'",
        ):
            assert label in body, f"{label} row missing from the comparison table"

    def test_the_page_shows_how_many_figures_each_provider_gave(self, app, client):
        prop = _make_property(source_email_id="ai-compare-page-2")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "'Rental figures given'" in body
        assert "numeric_coverage" in body

    def test_the_baseline_column_is_conditional(self, app, client):
        prop = _make_property(source_email_id="ai-compare-page-3")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "ai-compare-baseline-head" in body
        assert "baselineAvailable" in body, (
            "an absent baseline must drop the column, not draw it full of dashes"
        )
