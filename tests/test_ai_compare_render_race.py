"""Issue #208: the "Claude vs ChatGPT" table drew every section twice.

`refreshAiComparison()` emptied the table **before** awaiting the comparison
request, and one Enrich starts three of them -- after Claude, after ChatGPT and
again after `Promise.all` -- while the Refresh button can start another at any
moment. Every overlapping run therefore cleared a table that was still empty
and appended to one that had just been filled, so the owner saw `Verdicts`,
`Rental figures`, `Qualitative highlights` and `Schema + coverage` twice over.

Two layers are pinned, on both detail pages:

* Structural: the clear may not happen before the response arrives, and a run
  token has to drop a run that is no longer the newest. This half needs no
  node, so CI (Python only) really runs it.
* Behavioural: the real template JS runs under node against a stub DOM, with
  two overlapping refreshes resolved out of order. Skipped -- never silently
  passed -- when node is absent.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

NODE = shutil.which("node")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
TEMPLATES = ["property_detail.html", "land_detail.html"]


def _read_template(template_name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, template_name), "r", encoding="utf-8") as f:
        return f.read()


def _extract_inline_script(template_name: str) -> str:
    """The template's single <script> block, with the Jinja-templated
    `window.X = {{ ... }}` bootstrap assignments neutralized -- the only lines
    inside it that are not valid standalone JS. Same trick as
    tests/test_issue_23_xss_and_prompt_injection.py."""
    html = _read_template(template_name)
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
    return "\n".join(fixed_lines)


def _function_source(source: str, name: str) -> str:
    """Return one top-level `async function name(...) {...}` by brace matching."""
    start = source.index(f"function {name}(")
    depth = 0
    for pos in range(source.index("{", start), len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# ---------------------------------------------------------------------------
# Structural: the shape that produced the duplicate cannot come back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", TEMPLATES)
class TestTheTableIsNotClearedBeforeTheAnswerArrives:
    def test_the_clear_happens_after_the_request(self, template_name):
        body = _function_source(_read_template(template_name), "refreshAiComparison")
        fetch_at = body.index("await fetch(")

        for clear in ("tbody.innerHTML = ''", "tbody.textContent = ''"):
            at = body.find(clear)
            assert at == -1 or at > fetch_at, (
                f"{template_name}: `{clear}` runs before the await, so a second "
                "refresh clears an empty table and then appends to a full one"
            )

    def test_a_run_token_drops_a_run_that_is_no_longer_the_newest(self, template_name):
        source = _read_template(template_name)
        body = _function_source(source, "refreshAiComparison")

        assert "let _aiCompareRun = 0;" in source
        assert "const run = ++_aiCompareRun;" in body
        guard_at = body.index("if (run !== _aiCompareRun) return;")
        assert guard_at > body.index("await fetch("), (
            f"{template_name}: the staleness guard must be read after the "
            "response, not before it"
        )


# ---------------------------------------------------------------------------
# Behavioural: two overlapping refreshes, resolved out of order, under node
# ---------------------------------------------------------------------------


def _payload(investment_rating: str, risk_level: str) -> dict:
    """A comparison response both detail pages can render.

    `has_claude`/`has_chatgpt` is what the property page reads; the land page
    reads `schema.found`, so both are populated.
    """
    side = {
        "metrics": {
            "investment_rating": investment_rating,
            "rental_yield": 5.5,
            "cap_rate": 4.2,
            "price_to_rent_ratio": 12.0,
            "payback_period_years": 12.0,
        },
        "highlights": {
            "price_verdict": "UNDERPRICED",
            "price_summary": "Below the comps.",
            "investment_potential_rating": "MEDIUM",
            "risk_level": risk_level,
            "key_drivers": "plot • rail • schools",
            "best_use": "Single-family home",
            "market_trend": "STABLE",
        },
        "schema": {"found": 8, "total": 8},
        "numeric_coverage": {"found": 4, "total": 4},
        "fidelity_score": None,
        "overall_score": 100.0,
    }
    return {
        "success": True,
        "has_claude": True,
        "has_chatgpt": True,
        "openai_configured": True,
        "claude_model": "claude-test",
        "chatgpt_model": "gpt-test",
        "comparison": {
            "claude": side,
            "chatgpt": json.loads(json.dumps(side)),
            "expected": None,
            "baseline": {"available": False, "reason": "no baseline in the test"},
        },
    }


# The stub DOM: enough of it that the real render code runs unchanged, and a
# document fragment that behaves like one (appending it moves its children).
_DOM_STUB = """
'use strict';
class FakeNode {
    constructor(tag) {
        this.tagName = tag;
        this.childNodes = [];
        this.style = {};
        this.className = '';
        this.__text = '';
        this.__isFragment = false;
    }
    appendChild(child) {
        if (child && child.__isFragment) {
            for (const c of child.childNodes) this.childNodes.push(c);
            child.childNodes = [];
        } else {
            this.childNodes.push(child);
        }
        return child;
    }
    set textContent(value) {
        if (value === '') this.childNodes = [];
        this.__text = String(value);
    }
    get textContent() { return this.__text; }
    set innerHTML(value) {
        if (value === '') this.childNodes = [];
        this.__html = String(value);
    }
    get innerHTML() { return this.__html || ''; }
}

const ELEMENTS = {};
global.window = {};
global.document = {
    addEventListener: function () {},
    getElementById: function (id) {
        if (!ELEMENTS[id]) ELEMENTS[id] = new FakeNode(id);
        return ELEMENTS[id];
    },
    createElement: function (tag) { return new FakeNode(tag); },
    createDocumentFragment: function () {
        const frag = new FakeNode('#fragment');
        frag.__isFragment = true;
        return frag;
    },
};

// Every fetch parks until the driver resolves it, so the two refreshes really
// do overlap instead of running one after the other.
const PENDING = [];
global.fetch = function () {
    return new Promise(function (resolve) { PENDING.push(resolve); });
};
const respond = (index, payload) => PENDING[index]({
    json: () => Promise.resolve(payload),
});
const rowsOf = (id) => document.getElementById(id).childNodes.map(
    (tr) => tr.childNodes.map((td) => td.textContent)
);
"""


def _run_node(template_name: str, driver_js: str) -> dict:
    if NODE is None:
        pytest.skip(
            "node executable not found; the JS-level render-race regression is "
            "skipped (this repo's CI only provisions Python)"
        )

    harness = f"{_DOM_STUB}\n{_extract_inline_script(template_name)}\n{driver_js}\n"
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run([NODE, path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)

    assert proc.returncode == 0, f"{template_name}: node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


_OLD = _payload("BELOW_AVERAGE", "LOW")
_NEW = _payload("EXCELLENT", "HIGH")


@pytest.mark.parametrize("template_name", TEMPLATES)
class TestOverlappingRefreshesUnderNode:
    def _overlap(self, template_name: str) -> dict:
        """Start two refreshes, answer the newer one first, the older last."""
        driver = (
            f"const OLD = {json.dumps(_OLD)};\n"
            f"const NEW = {json.dumps(_NEW)};\n"
            "const first = refreshAiComparison();\n"
            "const second = refreshAiComparison();\n"
            "Promise.resolve().then(() => {\n"
            "  respond(1, NEW);\n"
            "  respond(0, OLD);\n"
            "});\n"
            "Promise.all([first, second]).then(() => {\n"
            "  console.log(JSON.stringify({ rows: rowsOf('ai-compare-tbody') }));\n"
            "});\n"
        )
        return _run_node(template_name, driver)

    def test_two_overlapping_refreshes_leave_one_copy(self, template_name):
        rows = self._overlap(template_name)["rows"]
        labels = [row[0] for row in rows if row]

        assert labels, "the comparison table rendered nothing at all"
        duplicated = sorted({label for label in labels if labels.count(label) > 1})
        assert not duplicated, (
            f"{template_name}: rendered twice: {duplicated} (rows: {len(labels)})"
        )

    def test_the_older_answer_does_not_overwrite_the_newer_one(self, template_name):
        rows = self._overlap(template_name)["rows"]
        rating = next(row for row in rows if row and row[0] == "Investment rating")

        assert rating[1] == "Excellent", (
            f"{template_name}: the slower first run overwrote the newest answer "
            f"({rating[1]!r} came from the stale response)"
        )

    def test_a_single_refresh_still_renders_the_table(self, template_name):
        """The guard must not make the ordinary one-refresh path draw nothing."""
        driver = (
            f"const NEW = {json.dumps(_NEW)};\n"
            "const only = refreshAiComparison();\n"
            "Promise.resolve().then(() => respond(0, NEW));\n"
            "only.then(() => {\n"
            "  console.log(JSON.stringify({\n"
            "    rows: rowsOf('ai-compare-tbody'),\n"
            "    display: document.getElementById('ai-compare-table').style.display,\n"
            "  }));\n"
            "});\n"
        )
        result = _run_node(template_name, driver)

        labels = [row[0] for row in result["rows"] if row]
        assert "Investment rating" in labels
        assert result["display"] == "", "the table stayed hidden after a refresh"
