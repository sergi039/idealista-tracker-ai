"""
Regression tests for GitHub issue #23 (HIGH, security): stored XSS via
AI analysis / listing text -> innerHTML in the land/property detail pages.

Two independent layers are covered:

1. Render-side (the actual sink): templates/land_detail.html and
   templates/property_detail.html build HTML strings from listing/AI data
   and assign them to `.innerHTML`. The fix escapes every string leaf
   before interpolation. These tests execute the *real* JS shipped in the
   templates (extracted verbatim, with only the two/three Jinja-templated
   `window.X = {{ ... }}` bootstrap assignments neutralized) inside Node,
   feed it attacker-controlled payloads, and assert the produced HTML never
   contains a live `<script`/`<img`/`<svg` tag. Skipped (not failed, not
   passed) when Node isn't available, since the repo's CI only provisions
   Python.

2. Prompt-side (defense in depth against the AI being coaxed into echoing
   injected HTML): untrusted listing/description text is capped in length
   and wrapped in explicit "treat as data" delimiters before being embedded
   into any LLM prompt. External API clients are mocked; no network calls.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from services.anthropic_service import AnthropicService
from services.description_service import (
    MAX_PROMPT_DESCRIPTION_CHARS,
    UNTRUSTED_TEXT_INSTRUCTION,
    DescriptionService,
)
from services.openai_service import OpenAIService

NODE = shutil.which("node")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

# Payloads chosen so a passing (unescaped) render would produce a tag a
# browser actually executes on page load, not just "a < character somewhere".
IMG_XSS = "<img src=x onerror=alert(1)>"
SCRIPT_XSS = "<script>alert(document.cookie)</script>"
JS_URL = "javascript:alert(1)"


# ---------------------------------------------------------------------------
# Layer 1: render-side (innerHTML sink) — executes the real template JS
# ---------------------------------------------------------------------------


def _extract_inline_script(template_name: str) -> str:
    """Pull the single <script>...</script> block out of a detail template
    and neutralize the Jinja-templated bootstrap assignments (the only
    lines inside it that aren't valid standalone JS) so it can be evaluated
    by Node without a Jinja render pass."""
    path = os.path.join(TEMPLATES_DIR, template_name)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    script = html[start:end]

    fixed_lines = []
    for line in script.splitlines():
        if ("{{" in line or "{%" in line) and re.match(r"^\s*window\.\w+\s*=", line):
            name = re.match(r"^\s*(window\.\w+)\s*=", line).group(1)
            fixed_lines.append(f"{name} = null;")
        else:
            fixed_lines.append(line)
    fixed = "\n".join(fixed_lines)

    # Only the top-level `window.X = {{ ... }}` bootstrap assignments are
    # invalid standalone JS; `{{ land.id }}` inside a JS template-literal
    # *string* (e.g. `` `/api/x/{{ land.id }}` ``) is legitimate JS and
    # must survive untouched. Assert specifically that no window.*
    # assignment still carries Jinja syntax after the fix-up.
    for line in fixed_lines:
        if re.match(r"^\s*window\.\w+\s*=", line):
            assert "{{" not in line and "{%" not in line, (
                f"{template_name}: window.* bootstrap assignment still has "
                f"Jinja syntax after neutralization: {line!r}"
            )
    return fixed


def _run_node(script_js: str, driver_js: str) -> dict:
    """Evaluate `script_js` (the extracted template JS) followed by
    `driver_js` (which must print exactly one JSON line to stdout) in a
    fresh Node process, with minimal DOM stubs."""
    if NODE is None:
        pytest.skip(
            "node executable not found; JS-level XSS-escaping regression "
            "skipped (this repo's CI only provisions Python)"
        )

    harness = (
        "'use strict';\n"
        "global.window = {};\n"
        "global.document = {\n"
        "  addEventListener: function () {},\n"
        "  getElementById: function () { return null; },\n"
        "};\n"
        f"{script_js}\n"
        f"{driver_js}\n"
    )

    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run(
            [NODE, path], capture_output=True, text=True, timeout=15
        )
    finally:
        os.unlink(path)

    assert proc.returncode == 0, (
        f"node harness crashed (stderr below) - either the extraction is "
        f"stale or a real bug:\n{proc.stderr}"
    )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    assert lines, f"node harness produced no output; stderr:\n{proc.stderr}"
    return json.loads(lines[-1])


def _malicious_similar_property(idx: int) -> dict:
    return {
        "id": idx,
        "title": IMG_XSS,
        "price": 100000,
        "area": 500,
        "municipality": SCRIPT_XSS,
        "land_type": "developed",
        "property_category": IMG_XSS,
        "category": IMG_XSS,
        "subtype": IMG_XSS,
        "score_total": 42.0,
        "url": JS_URL,
    }


def _malicious_analysis() -> dict:
    """A structured-analysis payload with the attacker payload in every
    string leaf that either template's renderStructuredAIAnalysis touches,
    covering both the land_detail and property_detail schema shapes."""
    return {
        "price_analysis": {
            "verdict": IMG_XSS,
            "summary": SCRIPT_XSS,
            "recommendation": IMG_XSS,
        },
        "investment_potential": {
            "rating": IMG_XSS,
            "forecast": SCRIPT_XSS,
            "risk_level": IMG_XSS,
            "summary": SCRIPT_XSS,
            "key_drivers": [IMG_XSS, SCRIPT_XSS],
        },
        "risks_analysis": {
            "major_risks": [IMG_XSS],
            "advantages": [SCRIPT_XSS],
            "mitigation": IMG_XSS,
        },
        "comparable_analysis": {
            "market_position": IMG_XSS,
            "price_comparison": SCRIPT_XSS,
            "advantages_vs_similar": [IMG_XSS],
            "disadvantages_vs_similar": [SCRIPT_XSS],
        },
        "development_ideas": {
            "best_use": IMG_XSS,
            "building_size": SCRIPT_XSS,
            "estimated_cost": IMG_XSS,
            "special_features": SCRIPT_XSS,
        },
        "construction_value_estimation": {
            "construction_type": IMG_XSS,
            "minimum_value": 1000,
            "maximum_value": 2000,
            "total_investment": SCRIPT_XSS,
        },
        "market_price_dynamics": {
            "price_trend": IMG_XSS,
            "current_trend": IMG_XSS,
            "annual_growth_rate": 3.5,
            "trend_analysis": SCRIPT_XSS,
            "market_factors": [IMG_XSS],
            "future_outlook": SCRIPT_XSS,
        },
        "rental_market_analysis": {
            "monthly_rent_min": 100,
            "monthly_rent_avg": 200,
            "monthly_rent_max": 300,
            "annual_rent_avg": 2400,
            "rental_yield": 5.5,
            "cap_rate": 4.2,
            "price_to_rent_ratio": 12.0,
            "payback_period_years": 10,
            "rental_strategy": IMG_XSS,
            "investment_rating": SCRIPT_XSS,
            "demand_factors": [IMG_XSS],
            "summary": SCRIPT_XSS,
        },
        "similar_objects": [
            _malicious_similar_property(1),
            _malicious_similar_property(2),
        ],
        "similar_properties_data": [
            _malicious_similar_property(3),
            _malicious_similar_property(4),
        ],
    }


DANGEROUS_TAG_RE = re.compile(r"<(script|img|svg|iframe)\b", re.IGNORECASE)


@pytest.mark.parametrize("template_name", ["land_detail.html", "property_detail.html"])
class TestDetailTemplateEscapesUntrustedHtml:
    """Issue #23: renderStructuredAIAnalysis()/renderSimilarProperties() must
    never hand a live <script>/<img onerror>/<svg onload> tag to innerHTML,
    however the underlying listing text or persisted AI analysis reads."""

    def test_render_structured_ai_analysis_escapes_payload(self, template_name):
        script = _extract_inline_script(template_name)
        driver = (
            "const analysis = "
            + json.dumps(_malicious_analysis())
            + ";\n"
            "const html = renderStructuredAIAnalysis(analysis);\n"
            "console.log(JSON.stringify({ html }));\n"
        )
        result = _run_node(script, driver)
        html = result["html"]

        assert not DANGEROUS_TAG_RE.search(html), (
            f"{template_name}: renderStructuredAIAnalysis() emitted a live "
            f"dangerous tag from attacker-controlled analysis data:\n{html}"
        )
        # The payload's angle brackets must survive *escaped*, proving the
        # content made it through deepEscapeStrings rather than being
        # silently dropped (which would also hide the vulnerability).
        assert "&lt;img" in html or "&lt;script" in html, (
            f"{template_name}: expected the escaped form of the injected "
            "payload to be present in the rendered HTML"
        )
        # javascript: URLs must never reach an href attribute.
        assert "javascript:alert" not in html, (
            f"{template_name}: a javascript: URL leaked into an href"
        )

    def test_raw_text_fallback_escapes_payload(self, template_name):
        script = _extract_inline_script(template_name)
        driver = (
            "const target = { innerHTML: '' };\n"
            "const html = `<div class=\"analysis-text\">${escapeHtml(String("
            + json.dumps(SCRIPT_XSS + "\nsecond line")
            + ")).replace(/\\n/g, '<br>')}</div>`;\n"
            "console.log(JSON.stringify({ html }));\n"
        )
        result = _run_node(script, driver)
        html = result["html"]
        assert not DANGEROUS_TAG_RE.search(html), (
            f"{template_name}: the _raw_text fallback path did not escape "
            f"a raw <script> payload:\n{html}"
        )
        assert "<br>" in html, "newline-to-<br> conversion should still work"

    def test_is_safe_http_url_rejects_javascript_scheme(self, template_name):
        script = _extract_inline_script(template_name)
        driver = (
            "console.log(JSON.stringify({\n"
            "  js: isSafeHttpUrl('javascript:alert(1)'),\n"
            "  data: isSafeHttpUrl('data:text/html,<script>alert(1)</script>'),\n"
            "  http: isSafeHttpUrl('http://idealista.com/x'),\n"
            "  https: isSafeHttpUrl('https://idealista.com/x'),\n"
            "  empty: isSafeHttpUrl(''),\n"
            "  none: isSafeHttpUrl(null),\n"
            "}));\n"
        )
        result = _run_node(script, driver)
        assert result == {
            "js": False,
            "data": False,
            "http": True,
            "https": True,
            "empty": False,
            "none": False,
        }

    def test_escape_html_neutralizes_all_html_metacharacters(self, template_name):
        script = _extract_inline_script(template_name)
        driver = (
            "console.log(JSON.stringify({ out: escapeHtml(" + json.dumps(
                "<script>&\"'</script>"
            ) + ") }));\n"
        )
        result = _run_node(script, driver)
        assert result["out"] == "&lt;script&gt;&amp;&quot;&#39;&lt;/script&gt;"


# ---------------------------------------------------------------------------
# Layer 2: prompt-side (defense in depth against prompt injection)
# ---------------------------------------------------------------------------


class TestDescriptionServiceCapsAndDelimitsUntrustedText:
    """services/description_service.py:enhance_description had no length
    cap at all on raw_description (unlike every other AI-prompt builder in
    the repo) and no instruction telling the model to treat it as data."""

    def _service_with_mocked_client(self):
        fake_message = MagicMock()
        fake_message.content = [MagicMock(text='{"enhanced_description": "ok"}')]

        fake_anthropic_service = MagicMock()
        fake_anthropic_service.model = "test-model"
        fake_anthropic_service.client.messages.create.return_value = fake_message

        with patch(
            "services.description_service.get_anthropic_service",
            return_value=fake_anthropic_service,
        ):
            service = DescriptionService()
        return service, fake_anthropic_service

    def test_long_description_is_capped_before_reaching_the_prompt(self):
        service, fake = self._service_with_mocked_client()
        raw_description = "A" * 5000 + "MARKER_BEYOND_CAP" + "B" * 5000

        service.enhance_description(raw_description)

        prompt = fake.client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "A" * MAX_PROMPT_DESCRIPTION_CHARS in prompt
        assert "A" * (MAX_PROMPT_DESCRIPTION_CHARS + 1) not in prompt
        assert "MARKER_BEYOND_CAP" not in prompt, (
            "text beyond the cap must not reach the LLM prompt"
        )

    def test_description_is_wrapped_in_untrusted_data_delimiters(self):
        service, fake = self._service_with_mocked_client()
        service.enhance_description(SCRIPT_XSS)

        prompt = fake.client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "<<<LISTING_TEXT_START>>>" in prompt
        assert "<<<LISTING_TEXT_END>>>" in prompt
        assert UNTRUSTED_TEXT_INSTRUCTION in prompt
        start = prompt.index("<<<LISTING_TEXT_START>>>")
        end = prompt.index("<<<LISTING_TEXT_END>>>")
        assert SCRIPT_XSS in prompt[start:end], (
            "the listing text itself must still be inside the delimiters"
        )

    def test_extraction_still_runs_on_the_full_original_text(self):
        """Capping the *prompt* must not regress price/area regex
        extraction, which is expected to scan the full original text."""
        service, _ = self._service_with_mocked_client()
        padding = "x" * 2000
        raw_description = f"{padding} 150,000€ and 500m²"

        extracted = service.extract_key_data(raw_description)
        assert extracted.get("current_price") == 150000
        assert extracted.get("area") == 500


class TestAnthropicServiceDelimitsDescription:
    def test_format_comprehensive_data_delimits_and_caps_description(self):
        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = AnthropicService()
            long_description = "A" * 2000 + "MARKER_BEYOND_CAP"
            text = service._format_comprehensive_data({"description": long_description})

        assert "<<<LISTING_TEXT_START>>>" in text
        assert "<<<LISTING_TEXT_END>>>" in text
        assert "MARKER_BEYOND_CAP" not in text


class TestOpenAIServiceDelimitsDescription:
    def test_build_prompt_delimits_description(self):
        from types import SimpleNamespace

        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = OpenAIService()

        land = SimpleNamespace(
            id=1,
            title="Test land",
            price=Decimal("100000"),
            area=Decimal("500"),
            municipality="Valencia",
            land_type="developed",
            score_total=Decimal("50"),
            travel_time_nearest_beach=None,
            nearest_beach_name=None,
            travel_time_oviedo=None,
            travel_time_gijon=None,
            travel_time_airport=None,
            description=SCRIPT_XSS,
        )

        prompt = service._build_prompt(land, enriched_data={}, similar_properties=[])

        assert "<<<LISTING_TEXT_START>>>" in prompt
        assert "<<<LISTING_TEXT_END>>>" in prompt
        start = prompt.index("<<<LISTING_TEXT_START>>>")
        end = prompt.index("<<<LISTING_TEXT_END>>>")
        assert SCRIPT_XSS in prompt[start:end]


class TestPropertyAIServiceDelimitsDescription:
    @pytest.fixture
    def app(self):
        from tests import setup_test_environment

        setup_test_environment()
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        from app import create_app, db

        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()

    def test_build_prompt_delimits_description(self, app):
        from app import db
        from models import Property
        from services.property_ai_service import PropertyAIService

        with app.app_context():
            prop = Property(
                source_email_id="issue23_test_1",
                title="Test property",
                municipality="Barcelona",
                property_category="housing",
                property_subtype="apartment",
                price=Decimal("250000.00"),
                area=Decimal("90.00"),
                description="A" * 2000 + "MARKER_BEYOND_CAP",
            )
            db.session.add(prop)
            db.session.commit()

            with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
                service = PropertyAIService()
                prompt, _system = service._build_prompt(prop)

            assert "<<<LISTING_TEXT_START>>>" in prompt
            assert "<<<LISTING_TEXT_END>>>" in prompt
            assert "MARKER_BEYOND_CAP" not in prompt
