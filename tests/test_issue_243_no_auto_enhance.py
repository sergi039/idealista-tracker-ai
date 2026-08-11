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

import re
from pathlib import Path

from tests.js_harness import object_member_source, run_node_script

MAIN_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "main.js"


def _run(driver: str, tmp_path) -> dict:
    """Drive the real `initializeDescriptionUI` under node."""
    member = object_member_source(
        MAIN_JS.read_text(encoding="utf-8"), "initializeDescriptionUI"
    )
    return run_node_script(
        f"const initializeDescriptionUI = {member};\n" + driver, tmp_path
    )


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
