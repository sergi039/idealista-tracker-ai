"""The three structured-analysis JSON Schemas, pinned against their readers (#218).

`services/property_ai_service.py` used to hand each CLI a *string template* --
sample JSON with bare-word placeholders (`"price_per_m2":
estimated_market_price_per_m2`) pasted into the prompt as an illustration, not
a contract. These are real JSON Schema documents now, passed to `codex exec
--output-schema` / `claude --json-schema` (see tests/test_ai_bridge_schema.py
for the bridge side).

The regression to fear, per the issue, is drift in either direction: "a
schema that permits a field the template never reads, or omits one it does".
So this file does not compare the schema against itself -- it parses the two
actual readers,

* `renderStructuredAIAnalysis` / `renderSimilarProperties` in
  `templates/property_detail.html` (the property page's own render), and
* `extract_metrics` / `extract_highlights` / `_best_use` in
  `utils/analysis_compare.py` (what `/api/property/<id>/analysis/compare`
  reads for the provider-comparison table),

into a read-set per section, and asserts each schema's fields against it. If
either file stops reading a field, or starts reading a new one, the parsed
read-set moves and these tests fail without anyone having to remember to
update them by hand.

`comparable_analysis`, `construction_value_estimation`, and the "ideas"
sections' fields other than `best_use`/`best_improvements` are deliberately
out of this pinning: their top-level key is required (schema_completeness in
utils/analysis_compare.py counts it), but nothing reads their contents today.
Pinning a schema to zero permitted fields there would not be a meaningful
test, so those sections are checked for presence only.
"""

import re
from pathlib import Path
from typing import Dict, Set

import pytest

from services.property_ai_service import (
    GENERIC_STRUCTURED_JSON_SCHEMA,
    HOUSING_STRUCTURED_JSON_SCHEMA,
    LAND_STRUCTURED_JSON_SCHEMA,
)
from utils.analysis_compare import GENERIC_TOP_KEYS, HOUSING_TOP_KEYS, LAND_TOP_KEYS

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "property_detail.html"
ANALYSIS_COMPARE = Path(__file__).resolve().parents[1] / "utils" / "analysis_compare.py"

SCHEMAS_BY_CATEGORY = {
    "land": (LAND_STRUCTURED_JSON_SCHEMA, LAND_TOP_KEYS, "development_ideas"),
    "housing": (HOUSING_STRUCTURED_JSON_SCHEMA, HOUSING_TOP_KEYS, "renovation_ideas"),
    "generic": (GENERIC_STRUCTURED_JSON_SCHEMA, GENERIC_TOP_KEYS, "usage_ideas"),
}

# JS Array/String prototype members that show up as `var.member` in the
# similar-objects array-rendering branch (dead code for our own schema, which
# always answers with the {comparison_summary, recommended_alternatives}
# object shape) -- not real field candidates.
_JS_NOISE = {
    "length",
    "map",
    "filter",
    "some",
    "every",
    "forEach",
    "join",
    "slice",
    "indexOf",
    "includes",
    "push",
    "toString",
    "hasOwnProperty",
    "constructor",
}

# Fallback-only aliases: read literally by the JS (so the parser below finds
# them), but each sits behind `||` after a primary field the schema does
# supply, so a missing schema field here never produces an empty card.
#   - investment.summary || investment.forecast
#   - rental.summary || humanizeEnumLabel(rental.investment_rating)
#   - similarProperties.comparison_summary || similarProperties.summary
#   - similarProperties.recommended_alternatives || similarProperties.alternatives
_DEAD_FALLBACK_ALIASES = {
    "investment_potential": {"summary"},
    "rental_market_analysis": {"summary"},
    "similar_objects": {"summary", "alternatives"},
}

# The fields with no such fallback: if a schema does not produce these, the
# corresponding UI element (or comparison-table row) renders as if the
# provider said nothing.
REQUIRED_READ_FIELDS: Dict[str, Set[str]] = {
    "price_analysis": {"verdict", "summary"},
    "investment_potential": {"rating", "risk_level", "forecast", "key_drivers"},
    "risks_analysis": {"major_risks", "minor_issues", "advantages", "mitigation"},
    "market_price_dynamics": {"price_trend", "trend_analysis"},
    "rental_market_analysis": {
        "investment_rating",
        "rental_yield",
        "cap_rate",
        "price_to_rent_ratio",
        "payback_period_years",
    },
    "similar_objects": {"comparison_summary", "recommended_alternatives"},
}

PINNED_SECTIONS = tuple(REQUIRED_READ_FIELDS)


def _extract_function_body(source: str, func_name: str) -> str:
    """The text between a top-level `function NAME(...) {` and its matching `}`."""
    match = re.search(rf"function\s+{func_name}\s*\([^)]*\)\s*\{{", source)
    assert match, f"{func_name} not found in {TEMPLATE.name}"
    start = match.end()
    depth = 1
    i = start
    while depth > 0:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start : i - 1]


def _read_fields_from_template() -> Dict[str, Set[str]]:
    js_source = TEMPLATE.read_text()
    render_body = _extract_function_body(js_source, "renderStructuredAIAnalysis")
    similar_body = _extract_function_body(js_source, "renderSimilarProperties")

    # `const priceAnalysis = analysis.price_analysis || {};` and friends.
    var_to_section = dict(
        re.findall(r"const\s+(\w+)\s*=\s*analysis\.(\w+)\s*(?:\|\|)", render_body)
    )

    fields: Dict[str, Set[str]] = {}
    for var, section in var_to_section.items():
        accessed = set(re.findall(rf"\b{re.escape(var)}\.(\w+)", render_body))
        fields.setdefault(section, set()).update(accessed - _JS_NOISE)

    # `['major_risks', 'minor_issues', 'advantages'].some((key) => riskItems(risks[key])...)`
    # is a bracket access the generic `risks.\w+` regex above cannot see.
    bracket_match = re.search(
        r"\[\s*((?:'[^']*'\s*,?\s*)+)\]\s*\.some\(\s*\(key\)\s*=>\s*riskItems\(risks\[key\]\)",
        render_body,
    )
    assert bracket_match, "the risks[key] bracket idiom moved; update this parser"
    fields.setdefault("risks_analysis", set()).update(
        re.findall(r"'([^']*)'", bracket_match.group(1))
    )

    # renderSimilarProperties(similar) is called with no `similar.x` access in
    # renderStructuredAIAnalysis itself; its object-branch fields live here.
    similar_fields = set(re.findall(r"\bsimilarProperties\.(\w+)", similar_body))
    fields.setdefault("similar_objects", set()).update(similar_fields - _JS_NOISE)

    return fields


def _read_fields_from_analysis_compare() -> Dict[str, Set[str]]:
    py_source = ANALYSIS_COMPARE.read_text()
    fields: Dict[str, Set[str]] = {}

    # Literal two-string _pick(a, "section", "field") calls.
    for section, field in re.findall(r'_pick\(a,\s*"(\w+)",\s*"(\w+)"\)', py_source):
        fields.setdefault(section, set()).add(field)

    # _best_use()'s `for section in (...): value = _pick(a, section, "best_use")`
    # loop uses a variable, not a literal, so the regex above cannot see it.
    loop_match = re.search(
        r'for\s+section\s+in\s+\(([^)]*)\):\s*\n\s*value\s*=\s*_pick\(a,\s*section,\s*"(\w+)"\)',
        py_source,
    )
    assert loop_match, "_best_use's ideas-section loop moved; update this parser"
    loop_field = loop_match.group(2)
    for section in re.findall(r'"(\w+)"', loop_match.group(1)):
        fields.setdefault(section, set()).add(loop_field)

    # `rental = a.get("rental_market_analysis")` then `rental.get("field")`.
    for var, section in re.findall(r'(\w+)\s*=\s*a\.get\("(\w+)"\)', py_source):
        accessed = set(re.findall(rf'\b{re.escape(var)}\.get\("(\w+)"\)', py_source))
        fields.setdefault(section, set()).update(accessed)

    return fields


def _merge(*field_maps: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    merged: Dict[str, Set[str]] = {}
    for field_map in field_maps:
        for section, names in field_map.items():
            merged.setdefault(section, set()).update(names)
    return merged


@pytest.fixture(scope="module")
def read_fields() -> Dict[str, Set[str]]:
    return _merge(_read_fields_from_template(), _read_fields_from_analysis_compare())


# --- top-level keys ---------------------------------------------------------


@pytest.mark.parametrize("category", sorted(SCHEMAS_BY_CATEGORY))
def test_top_level_keys_match_analysis_compare(category):
    """One source of truth: `utils.analysis_compare`'s own *_TOP_KEYS lists.

    `schema_completeness()` there already scores a stored analysis against
    these lists, so importing them (rather than repeating the key names)
    means the schema and the completeness scorer cannot silently diverge.
    """
    schema, top_keys, _ideas_key = SCHEMAS_BY_CATEGORY[category]
    assert set(schema["properties"].keys()) == set(top_keys)
    assert set(schema["required"]) == set(top_keys)


# --- strict-mode shape -------------------------------------------------------


def _walk_objects(schema, path=""):
    if isinstance(schema, dict) and schema.get("type") == "object":
        yield path, schema
        for name, sub in schema.get("properties", {}).items():
            yield from _walk_objects(sub, f"{path}.{name}" if path else name)


@pytest.mark.parametrize("category", sorted(SCHEMAS_BY_CATEGORY))
def test_every_object_is_strict(category):
    """Every object sets `additionalProperties: false` and requires every
    property it declares -- optional data is nullable, not omittable. This is
    the shape both CLIs' structured-output modes enforce; see
    tests/test_ai_bridge_schema.py for the flags that request it."""
    schema, _top_keys, _ideas_key = SCHEMAS_BY_CATEGORY[category]
    for path, obj in _walk_objects(schema):
        assert obj.get("additionalProperties") is False, (
            f"{category}.{path or '<root>'} permits additional properties"
        )
        assert set(obj.get("required", [])) == set(obj.get("properties", {})), (
            f"{category}.{path or '<root>'} required != properties"
        )


# --- pinned sections: no unread fields, no missing ones ---------------------


@pytest.mark.parametrize("category", sorted(SCHEMAS_BY_CATEGORY))
@pytest.mark.parametrize("section", PINNED_SECTIONS)
def test_pinned_section_matches_the_readers(category, section, read_fields):
    schema, _top_keys, _ideas_key = SCHEMAS_BY_CATEGORY[category]
    schema_fields = set(schema["properties"][section]["properties"].keys())

    permitted = read_fields.get(section, set()) | _DEAD_FALLBACK_ALIASES.get(
        section, set()
    )
    leaked = schema_fields - permitted
    assert not leaked, (
        f"{category}.{section} permits {leaked}, which neither "
        "renderStructuredAIAnalysis/renderSimilarProperties nor "
        "utils/analysis_compare.py ever reads"
    )

    missing = REQUIRED_READ_FIELDS[section] - schema_fields
    assert not missing, (
        f"{category}.{section} omits {missing}, which a reader accesses "
        "with no fallback -- this would render as an empty card"
    )


# --- the one field of each "ideas" section that is actually read -----------


@pytest.mark.parametrize("category", sorted(SCHEMAS_BY_CATEGORY))
def test_ideas_section_best_use_field_is_pinned(category, read_fields):
    """`_best_use()` in utils/analysis_compare.py is what feeds the "Best use"
    row of the provider-comparison table; every other field of these three
    sections is informational only (see the module docstring)."""
    schema, _top_keys, ideas_key = SCHEMAS_BY_CATEGORY[category]
    expected_field = (
        "best_improvements" if ideas_key == "renovation_ideas" else "best_use"
    )

    assert expected_field in schema["properties"][ideas_key]["properties"]
    assert expected_field in read_fields.get(ideas_key, set()), (
        f"utils/analysis_compare.py no longer reads {ideas_key}.{expected_field}"
    )


# --- shape of the one section whose *type* is asserted, not just its fields -


@pytest.mark.parametrize("category", sorted(SCHEMAS_BY_CATEGORY))
def test_similar_objects_is_an_object_not_an_array(category):
    """renderSimilarProperties() branches on this: an array is read as full
    property records (a different, historical data shape); our own schema
    always answers with {comparison_summary, recommended_alternatives}."""
    schema, _top_keys, _ideas_key = SCHEMAS_BY_CATEGORY[category]
    assert schema["properties"]["similar_objects"]["type"] == "object"


# --- enum values: a typo here silently produces the "Unknown" badge --------


@pytest.mark.parametrize("category", sorted(SCHEMAS_BY_CATEGORY))
@pytest.mark.parametrize(
    ("section", "field", "expected_values"),
    [
        ("price_analysis", "verdict", {"FAIR_PRICE", "OVERPRICED", "UNDERPRICED"}),
        ("investment_potential", "rating", {"HIGH", "MEDIUM", "LOW"}),
        ("investment_potential", "risk_level", {"LOW", "MEDIUM", "HIGH"}),
        ("market_price_dynamics", "price_trend", {"RISING", "STABLE", "DECLINING"}),
        (
            "rental_market_analysis",
            "investment_rating",
            {"EXCELLENT", "GOOD", "MODERATE", "BELOW_AVERAGE"},
        ),
    ],
)
def test_enum_values_match_the_renderer_css_classes(
    category, section, field, expected_values
):
    """getPriceVerdictClass()/getPriceTrendClass() in property_detail.html
    switch on these exact strings; anything else falls through to the
    'bg-secondary'/'UNKNOWN' default, which is a silent miscolour, not a
    crash -- so it needs its own pin rather than relying on a KeyError."""
    schema, _top_keys, _ideas_key = SCHEMAS_BY_CATEGORY[category]
    prop = schema["properties"][section]["properties"][field]
    assert set(prop["enum"]) == expected_values
