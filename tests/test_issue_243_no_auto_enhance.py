"""Issue #243: opening a page must not start an AI run.

`initializeDescriptionUI()` runs on every page load. On a page carrying
`[data-land-id]` it fetched the stored description variants and, when the
answer was `not_processed`, POSTed `/api/enhance/description/<id>` on its own —
no press, no confirmation, nothing to stop a reload doing it again. The call
goes through the owner's Claude subscription, so it is not a metered API key,
but it is still work started by looking at a listing.

The real function runs under node here, against a server that says the
description was never processed, and the only request it may make is the read.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MAIN_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "main.js"


def _extract_method(source: str, name: str) -> str:
    """Return `function () {...}` for one `name: function (...) {...}` member."""
    match = re.search(rf"\n    {name}: (function\s*\()", source)
    assert match, f"{name} is not a member of the object literal any more"
    depth, start = 0, source.index("{", match.end())
    for pos in range(start, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start(1) : pos + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _run(driver: str, tmp_path) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    source = MAIN_JS.read_text(encoding="utf-8")
    script = tmp_path / "desc.js"
    script.write_text(
        f"const initializeDescriptionUI = {_extract_method(source, 'initializeDescriptionUI')};\n"
        + driver,
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_HARNESS = """
const requests = [];
const methodsCalled = [];
globalThis.console = { log: () => {}, error: () => {} };
globalThis.document = {
  querySelector: () => ({ getAttribute: () => '42' }),
  getElementById: () => null,
};
globalThis.fetch = async (url, options) => {
  requests.push({ url, method: (options && options.method) || 'GET' });
  return { json: async () => (%PAYLOAD%) };
};
const owner = new Proxy({}, {
  get: (_t, name) => (...args) => { methodsCalled.push(String(name)); },
});
initializeDescriptionUI.call(owner);
setTimeout(() => {
  console.log = (...a) => process.stdout.write(a.join(' ') + '\\n');
  process.stdout.write(JSON.stringify({ requests, methodsCalled }) + '\\n');
}, 60);
"""


class TestOpeningAPageStartsNoAiRun:
    def test_an_unprocessed_description_is_left_alone(self, tmp_path):
        result = _run(
            _HARNESS.replace("%PAYLOAD%", "{ success: true, status: 'not_processed' }"),
            tmp_path,
        )

        posts = [r for r in result["requests"] if r["method"] == "POST"]
        assert not posts, f"a page load started an AI run: {posts}"
        assert "autoEnhanceDescription" not in result["methodsCalled"]
        assert [r["url"] for r in result["requests"]] == [
            "/api/description/variants/42"
        ], "the read is all a page load may do"

    def test_a_failed_lookup_is_not_a_reason_to_spend_a_run(self, tmp_path):
        driver = _HARNESS.replace("%PAYLOAD%", "{}").replace(
            "return { json: async () => ({}) };",
            "throw new Error('network down');",
        )

        result = _run(driver, tmp_path)

        assert not [r for r in result["requests"] if r["method"] == "POST"]
        assert "autoEnhanceDescription" not in result["methodsCalled"]

    def test_a_stored_description_is_still_displayed(self, tmp_path):
        result = _run(
            _HARNESS.replace(
                "%PAYLOAD%", "{ success: true, status: 'processed', enhanced_en: 'x' }"
            ),
            tmp_path,
        )

        assert "displayEnhancedDescription" in result["methodsCalled"]
        assert not [r for r in result["requests"] if r["method"] == "POST"]


def test_the_auto_enhancer_is_gone_from_the_source():
    """A dormant helper is an invitation to call it again."""
    source = MAIN_JS.read_text(encoding="utf-8")

    assert "autoEnhanceDescription: function" not in source
    assert not re.search(r"fetch\(`/api/enhance/description/", source), (
        "nothing in the page's JavaScript may POST an enhancement"
    )
