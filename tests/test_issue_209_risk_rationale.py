"""Issue #209: the Risk level row stated a verdict and never said why.

`/properties/<id>` showed `Risk level — Claude: Medium, ChatGPT: High` with
nothing behind it:

* every provider is asked for `risks_analysis` (major risks, minor issues,
  advantages, mitigation) and answers it, and the property page rendered none
  of it -- the badge summarised a section the reader could not see;
* the comparison table carried `Key drivers` but no risk counterpart, so the
  disagreement was two bare words;
* nothing said what the scale is. The prompt asks for LOW|MEDIUM|HIGH and
  defines none of them, so it is each provider's own judgement and the two are
  not calibrated against each other.

What must not creep in with the fix: a listing whose analysis has no
`risks_analysis` must read as missing, not as "no risks".
"""

import json

import pytest

from app import create_app, db
from models import Property, PropertyAiAnalysisVariant
from tests import setup_test_environment
from tests.js_harness import read_template, run_node
from utils.analysis_compare import extract_highlights

TEMPLATE = "property_detail.html"


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


def _analysis(risks=None, risk_level="MEDIUM"):
    analysis = {
        "price_analysis": {"verdict": "UNDERPRICED", "summary": "Below the comps."},
        "investment_potential": {
            "rating": "MEDIUM",
            "risk_level": risk_level,
            "key_drivers": ["large plot", "rail"],
        },
        "renovation_ideas": {"best_improvements": ["insulation"]},
        "comparable_analysis": {"market_position": "mid"},
        "similar_objects": {"comparison_summary": "three comparables"},
        "market_price_dynamics": {"price_trend": "STABLE"},
        "rental_market_analysis": {"investment_rating": "AVERAGE"},
    }
    if risks is not None:
        analysis["risks_analysis"] = risks
    return analysis


_RISKS = {
    "major_risks": [
        "Construction obligation with an unfinished envelope",
        "Thin resale market for a 320 m² rural home",
        "Municipal permit timing",
        "A fourth risk that must not reach the one-line summary",
    ],
    "minor_issues": ["Exterior carpentry unfinished"],
    "advantages": ["Large plot"],
    "mitigation": "Budget the completion works before bidding.",
}


class TestHighlightsCarryTheReasons:
    def test_key_risks_is_extracted_from_the_major_risks(self):
        highlights = extract_highlights(_analysis(risks=_RISKS))

        assert highlights["risk_level"] == "MEDIUM"
        assert highlights["key_risks"].startswith(
            "Construction obligation with an unfinished envelope • "
        )
        assert "A fourth risk" not in highlights["key_risks"], (
            "the row takes the first three, like key_drivers"
        )

    def test_an_analysis_without_risks_reports_nothing_rather_than_no_risks(self):
        highlights = extract_highlights(_analysis(risks=None))

        assert highlights["key_risks"] is None, (
            "an absent section must read as missing, never as an empty risk list"
        )

    def test_a_sentence_where_the_schema_asked_for_a_list_is_still_an_answer(self):
        """Reporting nothing would say the provider was silent when it was not."""
        highlights = extract_highlights(
            _analysis(risks={"major_risks": "permits could slip"})
        )

        assert highlights["key_risks"] == "permits could slip"

    def test_a_shape_that_is_neither_a_list_nor_a_sentence_is_not_an_answer(self):
        highlights = extract_highlights(_analysis(risks={"major_risks": {"a": 1}}))

        assert highlights["key_risks"] is None

    def test_an_empty_list_is_not_reported_as_an_answer(self):
        highlights = extract_highlights(_analysis(risks={"major_risks": []}))

        assert highlights["key_risks"] is None

    def test_a_long_risk_line_is_truncated_like_the_other_highlights(self):
        highlights = extract_highlights(
            _analysis(risks={"major_risks": ["x" * 300]}),
        )

        assert len(highlights["key_risks"]) == 160
        assert highlights["key_risks"].endswith("…")


class TestTheReasonsReachTheBrowser:
    def test_the_compare_endpoint_sends_key_risks_for_both_providers(self, app, client):
        prop = Property(
            source_email_id="issue-209-compare",
            title="Detached house in Barrio de Prendonés",
            municipality="El Franco",
            property_category="housing",
        )
        prop.ai_analysis = _analysis(risks=_RISKS, risk_level="MEDIUM")
        db.session.add(prop)
        db.session.commit()
        db.session.add(
            PropertyAiAnalysisVariant(
                property_id=prop.id,
                provider="openai",
                model="gpt-test",
                analysis=_analysis(
                    risks={"major_risks": ["Liquidity of a rural house"]},
                    risk_level="HIGH",
                ),
            )
        )
        db.session.commit()

        data = json.loads(client.get(f"/api/property/{prop.id}/analysis/compare").data)
        claude = data["comparison"]["claude"]["highlights"]
        chatgpt = data["comparison"]["chatgpt"]["highlights"]

        assert (claude["risk_level"], chatgpt["risk_level"]) == ("MEDIUM", "HIGH")
        assert "Construction obligation" in claude["key_risks"]
        assert chatgpt["key_risks"] == "Liquidity of a rural house"

    def test_the_comparison_table_draws_the_risk_row_and_says_what_it_is(
        self, app, client
    ):
        prop = Property(source_email_id="issue-209-page", title="Plot")
        db.session.add(prop)
        db.session.commit()

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)

        assert "'Key risks'" in body
        assert "ch.key_risks" in body and "gh.key_risks" in body
        assert "RISK_LEVEL_NOTE" in body


# ---------------------------------------------------------------------------
# The real template JS, under node
# ---------------------------------------------------------------------------


def _render(analysis: dict) -> str:
    result = run_node(
        TEMPLATE,
        "const html = renderStructuredAIAnalysis("
        + json.dumps(analysis)
        + ");\nconsole.log(JSON.stringify({ html }));\n",
    )
    return result["html"]


class TestThePanelShowsWhatTheBadgeIsBasedOn:
    def test_the_risks_the_provider_returned_are_rendered(self):
        html = _render(_analysis(risks=_RISKS))

        assert "Risk Assessment" in html
        assert "Construction obligation with an unfinished envelope" in html
        assert "Exterior carpentry unfinished" in html
        assert "Budget the completion works before bidding." in html
        assert "Risk: Medium" in html

    def test_the_scale_is_explained_where_the_verdict_is_shown(self):
        html = _render(_analysis(risks=_RISKS))

        assert "not calibrated between providers" in html

    def test_an_analysis_without_risks_says_so_instead_of_inventing_any(self):
        html = _render(_analysis(risks=None))

        assert "Risk Assessment" in html
        assert "listed no specific risks" in html
        assert "Major risks" not in html, "a heading with nothing under it"

    def test_a_sentence_instead_of_a_list_is_shown_rather_than_dropped(self):
        html = _render(_analysis(risks={"major_risks": "permits could slip"}))

        assert "permits could slip" in html
        assert "listed no specific risks" not in html, (
            "the provider answered; reporting silence would be the wrong way round"
        )

    def test_the_comparison_row_carries_the_note_under_its_label(self):
        payload = {
            "success": True,
            "has_claude": True,
            "has_chatgpt": True,
            "openai_configured": True,
            "claude_model": "claude-test",
            "chatgpt_model": "gpt-test",
            "comparison": {
                "claude": {
                    "metrics": {},
                    "highlights": {
                        "risk_level": "MEDIUM",
                        "key_risks": "Construction obligation",
                    },
                    "schema": {"found": 8, "total": 8},
                    "numeric_coverage": {"found": 0, "total": 4},
                },
                "chatgpt": {
                    "metrics": {},
                    "highlights": {"risk_level": "HIGH", "key_risks": None},
                    "schema": {"found": 8, "total": 8},
                    "numeric_coverage": {"found": 0, "total": 4},
                },
                "expected": None,
                "baseline": {"available": False, "reason": "none in this test"},
            },
        }
        # The verdict rows moved to their own always-visible tbody with badge
        # cells (proposal D10, 2026-08-13); a badge cell's text lives in its
        # child span, so the reader falls through to it.
        result = run_node(
            TEMPLATE,
            f"const PAYLOAD = {json.dumps(payload)};\n"
            "const badgeRowsOf = (id) => document.getElementById(id).childNodes.map(\n"
            "  (tr) => tr.childNodes.map(\n"
            "    (td) => td.textContent || (td.childNodes[0] ? td.childNodes[0].textContent : '')\n"
            "  )\n"
            ");\n"
            "refreshAiComparison().then(() => {\n"
            "  console.log(JSON.stringify({\n"
            "    verdictRows: badgeRowsOf('ai-compare-verdicts-tbody'),\n"
            "    rows: rowsOf('ai-compare-tbody'),\n"
            "    notes: notesOf('ai-compare-verdicts-tbody'),\n"
            "  }));\n"
            "});\n"
            "Promise.resolve().then(() => respond(0, PAYLOAD));\n",
        )

        verdict_rows = {row[0]: row for row in result["verdictRows"] if row}
        assert verdict_rows["Risk level"][1:3] == ["Medium", "High"]
        rows = {row[0]: row for row in result["rows"] if row}
        assert rows["Key risks"][1:3] == ["Construction obligation", "—"], (
            "a provider that listed no risks must read as missing, not as none"
        )

        notes = [note for row in result["notes"] for note in row]
        assert any("not calibrated between providers" in note for note in notes)


class TestTheDisagreeChipMeansDisagreement:
    """The ≠ chip fires only on two answers that differ as values: a case
    variant is the same answer, and a missing answer disagrees with nothing
    (Phase-1 diff review, 2026-08-13)."""

    def _notes_for(self, claude_highlights, gpt_highlights):
        payload = {
            "success": True,
            "has_claude": True,
            "has_chatgpt": True,
            "openai_configured": True,
            "claude_model": "claude-test",
            "chatgpt_model": "gpt-test",
            "comparison": {
                "claude": {
                    "metrics": {},
                    "highlights": claude_highlights,
                    "schema": {"found": 8, "total": 8},
                    "numeric_coverage": {"found": 0, "total": 4},
                },
                "chatgpt": {
                    "metrics": {},
                    "highlights": gpt_highlights,
                    "schema": {"found": 8, "total": 8},
                    "numeric_coverage": {"found": 0, "total": 4},
                },
                "expected": None,
                "baseline": {"available": False, "reason": "none in this test"},
            },
        }
        result = run_node(
            TEMPLATE,
            f"const PAYLOAD = {json.dumps(payload)};\n"
            "refreshAiComparison().then(() => {\n"
            "  console.log(JSON.stringify({\n"
            "    notes: notesOf('ai-compare-verdicts-tbody'),\n"
            "  }));\n"
            "});\n"
            "Promise.resolve().then(() => respond(0, PAYLOAD));\n",
        )
        return [note for row in result["notes"] for note in row]

    def test_case_variants_are_the_same_answer(self):
        notes = self._notes_for({"risk_level": "MEDIUM"}, {"risk_level": "Medium"})
        assert "≠" not in notes

    def test_a_missing_answer_is_not_a_disagreement(self):
        notes = self._notes_for({"market_trend": "GROWING"}, {"market_trend": None})
        assert "≠" not in notes

    def test_two_different_answers_still_flag(self):
        notes = self._notes_for(
            {"price_verdict": "UNDERPRICED"}, {"price_verdict": "OVERPRICED"}
        )
        assert "≠" in notes


def test_the_note_is_written_once_and_used_in_both_places():
    """Two wordings drift apart; the panel and the table share the constant."""
    source = read_template(TEMPLATE)

    assert source.count("const RISK_LEVEL_NOTE =") == 1
    # The verdict-first panel (D9) states it twice — the strip badge's tooltip
    # and the Risk Assessment body — and the comparison's verdict row takes it
    # as an argument. Still one definition, shared by every surface.
    assert source.count("${RISK_LEVEL_NOTE}") == 2  # strip tooltip + panel body
    assert source.count(", RISK_LEVEL_NOTE)") == 1  # the comparison verdict row
