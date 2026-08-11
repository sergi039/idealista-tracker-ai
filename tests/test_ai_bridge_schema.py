"""Structured output at the bridge (#218): the two CLI flags, cleanup, and
backward compatibility.

`services/property_ai_service.py` now builds a real JSON Schema per property
category (see tests/test_ai_structured_schemas.py) and hands it to
`services/subscription_transport.py`'s `complete()`. This file covers the
next hop, `tools/ai_bridge.py`:

* `claude --json-schema <schema>` takes the schema itself, inline.
* `codex exec --output-schema <file>` takes a *file*, so the bridge writes
  one to a private temp file and must remove it again -- on the happy path,
  when the run times out, and when codex reports `turn.failed`.
* A request that does not carry a schema must behave exactly as it did
  before this field existed: no flag added, same argv otherwise.
* codex's existing last-JSON-message heuristic (#206 item 2 / #214) is the
  fallback for a CLI that ignores or rejects the schema, and this file
  proves it still fires with a schema in play, not just without one.

The bridge is a host-side script outside the Flask app (see
tests/test_ai_bridge_isolation.py's own note), so it is loaded here the same
way: by path, not as a package import.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.js_harness import load_bridge

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_bridge.py"

_SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": ["string", "null"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}


@pytest.fixture
def bridge():
    return load_bridge("ai_bridge_schema_under_test")


@pytest.fixture
def recorded_cmd(bridge, monkeypatch):
    """Capture the argv the bridge builds, and what the schema file (if any)
    held *at the moment the CLI would have run* -- `complete_codex` deletes
    it again before returning, so a check made afterwards would always find
    it gone regardless of whether cleanup code exists at all."""
    captured = {}

    def fake_which(name):
        return f"/fake/bin/{name}"

    def fake_run(cmd, stdin_text, timeout):
        captured["cmd"] = cmd
        captured["stdin"] = stdin_text
        if "--output-schema" in cmd:
            schema_path = cmd[cmd.index("--output-schema") + 1]
            captured["schema_path"] = schema_path
            captured["schema_file_existed_during_run"] = os.path.isfile(schema_path)
            if captured["schema_file_existed_during_run"]:
                captured["schema_file_contents_during_run"] = json.loads(
                    Path(schema_path).read_text()
                )
        return captured.get("stdout", "")

    monkeypatch.setattr(bridge, "_which", fake_which)
    monkeypatch.setattr(bridge, "_run", fake_run)
    return captured


# --- codex: --output-schema is a file, written and cleaned up --------------


def test_codex_writes_the_schema_to_a_file_and_passes_its_path(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps({"type": "turn.completed", "usage": {}})
    bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60, schema=_SAMPLE_SCHEMA)

    assert "--output-schema" in recorded_cmd["cmd"]
    assert recorded_cmd["schema_file_existed_during_run"] is True
    assert recorded_cmd["schema_file_contents_during_run"] == _SAMPLE_SCHEMA


def test_codex_schema_file_is_removed_after_a_normal_run(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps({"type": "turn.completed", "usage": {}})
    bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60, schema=_SAMPLE_SCHEMA)

    assert not os.path.exists(recorded_cmd["schema_path"]), (
        "the schema temp file survived a normal run"
    )


def test_codex_schema_file_is_removed_when_the_run_times_out(bridge, monkeypatch):
    """Cleanup has to survive `_kill_process_group`'s path too, not just a
    clean `_run` return -- see `complete_codex`'s `finally`."""
    seen_path = {}

    def timing_out_run(cmd, stdin_text, timeout):
        seen_path["path"] = cmd[cmd.index("--output-schema") + 1]
        assert os.path.isfile(seen_path["path"])
        raise bridge.BridgeTimeout("boom")

    monkeypatch.setattr(bridge, "_which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(bridge, "_run", timing_out_run)

    with pytest.raises(bridge.BridgeTimeout):
        bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60, schema=_SAMPLE_SCHEMA)

    assert not os.path.exists(seen_path["path"]), (
        "the schema temp file survived a timed-out run"
    )


def test_codex_schema_file_is_removed_when_the_turn_fails(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps({"type": "turn.failed", "error": "boom"})

    with pytest.raises(bridge.BridgeError, match="turn failed"):
        bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60, schema=_SAMPLE_SCHEMA)

    assert not os.path.exists(recorded_cmd["schema_path"]), (
        "the schema temp file survived a turn.failed run"
    )


def test_codex_omits_output_schema_without_a_schema(bridge, recorded_cmd):
    """Backward compatibility: a call with no schema builds the same argv as
    before this field existed -- no flag, no temp file."""
    recorded_cmd["stdout"] = json.dumps({"type": "turn.completed", "usage": {}})
    bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)

    assert "--output-schema" not in recorded_cmd["cmd"]
    assert "schema_path" not in recorded_cmd


# --- claude: --json-schema carries the schema itself ------------------------


def test_claude_passes_the_schema_inline(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps({"result": "{}", "usage": {}})
    bridge.complete_claude(
        "prompt", "sys", "claude-sonnet-5", 60, schema=_SAMPLE_SCHEMA
    )

    cmd = recorded_cmd["cmd"]
    assert "--json-schema" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == _SAMPLE_SCHEMA


def test_claude_omits_json_schema_without_a_schema(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps({"result": "{}", "usage": {}})
    bridge.complete_claude("prompt", "sys", "claude-sonnet-5", 60)

    assert "--json-schema" not in recorded_cmd["cmd"]


def test_claude_result_string_is_unchanged_without_a_schema(bridge, recorded_cmd):
    """Pins the pre-#218 behaviour: `result` as a plain string passes through
    verbatim, not re-encoded."""
    recorded_cmd["stdout"] = json.dumps({"result": "hello", "usage": {}})
    result = bridge.complete_claude("prompt", "sys", "claude-sonnet-5", 60)

    assert result["text"] == "hello"


def test_claude_a_structured_result_object_is_re_serialised_to_json(
    bridge, recorded_cmd
):
    """Under --json-schema, `claude -p --output-format json` may hand back
    `result` already decoded to an object rather than a JSON string. Naive
    `str(dict)` would emit Python repr (single-quoted) here, which breaks the
    caller's `json.loads` -- this is the defensive fix, not an assumption
    about what the live CLI actually does (see the live check in the PR
    report)."""
    recorded_cmd["stdout"] = json.dumps(
        {"result": {"verdict": "FAIR_PRICE"}, "usage": {}}
    )
    result = bridge.complete_claude(
        "prompt", "sys", "claude-sonnet-5", 60, schema=_SAMPLE_SCHEMA
    )

    assert json.loads(result["text"]) == {"verdict": "FAIR_PRICE"}


# --- the existing heuristic is still the fallback, schema or not -----------


def test_codex_last_json_message_heuristic_still_applies_with_a_schema(
    bridge, recorded_cmd
):
    """Same scenario as test_ai_bridge_isolation.py's
    test_codex_answer_survives_a_trailing_closing_remark, with a schema now
    also in play: codex emits the JSON answer and then a closing remark, and
    the remark must not win just because a schema was requested too."""
    recorded_cmd["stdout"] = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"verdict":"fair"}'},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Let me know if you need anything else.",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    result = bridge.complete_codex(
        "prompt", "", "gpt-5.6-terra", 60, schema=_SAMPLE_SCHEMA
    )
    assert result["text"] == '{"verdict":"fair"}'


def test_codex_falls_back_to_the_last_message_when_the_schema_was_ignored(
    bridge, recorded_cmd
):
    """A CLI that ignores or rejects the schema and answers with prose must
    still surface that prose upstream (#214), not fail outright."""
    recorded_cmd["stdout"] = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "I could not complete this analysis.",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    result = bridge.complete_codex(
        "prompt", "", "gpt-5.6-terra", 60, schema=_SAMPLE_SCHEMA
    )
    assert result["text"] == "I could not complete this analysis."


# --- the wire contract: do_POST's own handling of the schema field ---------


def _serve(bridge):
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post(server, payload, token="test-token"):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/v1/complete",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=10)


def test_do_post_forwards_a_present_schema_to_the_handler(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    captured = {}

    def fake_handler(prompt, system, model, timeout, schema=None):
        captured["schema"] = schema
        return {"text": "{}", "usage": {}, "provider": "claude"}

    monkeypatch.setitem(bridge.PROVIDERS, "claude", fake_handler)
    server, _ = _serve(bridge)
    try:
        response = _post(
            server, {"provider": "claude", "prompt": "hi", "schema": _SAMPLE_SCHEMA}
        )
        assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()

    assert captured["schema"] == _SAMPLE_SCHEMA


def test_do_post_without_a_schema_field_forwards_none(bridge, monkeypatch):
    """The absent-key case, at the actual wire boundary: this is what
    services/subscription_transport.py sent before #218 and what any other
    caller of the bridge still sends today."""
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    captured = {"called": False}

    def fake_handler(prompt, system, model, timeout, schema=None):
        captured["called"] = True
        captured["schema"] = schema
        return {"text": "{}", "usage": {}, "provider": "claude"}

    monkeypatch.setitem(bridge.PROVIDERS, "claude", fake_handler)
    server, _ = _serve(bridge)
    try:
        response = _post(server, {"provider": "claude", "prompt": "hi"})
        assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()

    assert captured["called"] is True
    assert captured["schema"] is None


def test_do_post_rejects_a_schema_that_is_not_an_object(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    server, _ = _serve(bridge)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(
                server,
                {"provider": "claude", "prompt": "hi", "schema": "not-an-object"},
            )
        assert excinfo.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
