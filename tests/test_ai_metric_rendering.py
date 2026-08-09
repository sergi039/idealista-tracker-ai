"""A metric the model could not compute has to read as missing.

ChatGPT answers `null` for every figure it has no evidence for -- the market
context tells it to state uncertainty rather than invent numbers, and the
prompt carries no rental or construction data at all. The panels rendered that
with `${value || 'N/A'}` plus a literal unit, so the page showed **"N/Ay"** and
"N/A%", which reads as a broken page rather than as an honest refusal. The same
expression also swallowed a real `0` and printed it as N/A.

Both halves are pinned here: the markup must route every figure through
`formatMetricValue`, and that function must actually behave -- the behavioural
half runs the real JavaScript under node and skips (never silently passes) when
node is absent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "property_detail.html"
SOURCE = TEMPLATE.read_text(encoding="utf-8")

# The four rental figures, and the unit each one carries.
RENTAL_METRICS = (
    ("rental_yield", "'%'"),
    ("cap_rate", "'%'"),
    ("price_to_rent_ratio", None),
    ("payback_period_years", "'y'"),
)


def _extract_function(source: str, name: str) -> str:
    """Return one top-level `function name(...) {...}` by brace matching."""
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", start)
    for pos in range(i, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unbalanced braces in {name}")


@pytest.mark.parametrize("field,unit", RENTAL_METRICS)
def test_every_rental_figure_goes_through_the_formatter(field, unit):
    expected = (
        f"formatMetricValue(rental.{field}, {unit})"
        if unit
        else f"formatMetricValue(rental.{field})"
    )
    assert expected in SOURCE, f"{field} is not rendered through formatMetricValue"


def test_no_metric_concatenates_a_unit_onto_a_fallback():
    """`${x || 'N/A'}y` is what produced "N/Ay" -- it must not come back."""
    offenders = re.findall(r"\$\{[^}]*\|\|\s*'N/A'\s*\}\s*[%y]", SOURCE)
    assert not offenders, f"unit glued to an N/A fallback: {offenders}"


def test_the_two_investment_badges_say_what_they_are():
    """Bare "Low" next to "High" read as one contradictory verdict."""
    assert "Potential: ${humanizeEnumLabel(investmentRating)}" in SOURCE
    assert "Risk: ${humanizeEnumLabel(riskLevel)}" in SOURCE


def test_the_rental_rating_is_not_printed_as_a_raw_enum():
    assert "humanizeEnumLabel(rental.investment_rating)" in SOURCE
    assert "|| rental.investment_rating ||" not in SOURCE


def test_the_formatter_is_defined_once():
    assert SOURCE.count("function formatMetricValue(") == 1
    assert "const _formatMetricValue = formatMetricValue;" in SOURCE


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_formatter_behaviour_under_node(tmp_path):
    script = tmp_path / "check.js"
    script.write_text(
        "\n".join(
            [
                _extract_function(SOURCE, "humanizeEnumLabel"),
                _extract_function(SOURCE, "formatMetricValue"),
                "console.log(JSON.stringify({",
                "  missing: formatMetricValue(null, 'y'),",
                "  undef: formatMetricValue(undefined, '%'),",
                "  empty: formatMetricValue('', '%'),",
                "  zero: formatMetricValue(0, '%'),",
                "  number: formatMetricValue(37, 'y'),",
                "  ratio: formatMetricValue(2.7),",
                "  enum: formatMetricValue('BELOW_AVERAGE'),",
                "}));",
            ]
        ),
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)

    # The defect itself: a missing figure never grows a unit.
    assert result["missing"] == "—"
    assert result["undef"] == "—"
    assert result["empty"] == "—"
    # A measured zero is a value, not a gap.
    assert result["zero"] == "0%"
    assert result["number"] == "37y"
    assert result["ratio"] == "2.7"
    assert result["enum"] == "Below Average"
