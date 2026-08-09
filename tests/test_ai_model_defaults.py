"""The configured AI models must reach the CLIs that actually answer.

`tools/ai_bridge.py` shells out to the Claude Code and Codex CLIs, and the
codex branch *drops* any model id outside that CLI's own catalogue: the call
then runs on the CLI default while the app keeps labelling the result with the
id from `config.py`. A stale default therefore fails silently, which is exactly
the class of bug that is invisible in the UI. Pin both defaults at the boundary
that decides which flag is passed.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from config import Config

BRIDGE_PATH = Path(__file__).resolve().parent.parent / "tools" / "ai_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("ai_bridge_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge(monkeypatch):
    module = _load_bridge()
    monkeypatch.setattr(module, "_which", lambda name: f"/usr/local/bin/{name}")
    return module


def _capture(module, monkeypatch, stdout):
    """Replace the subprocess call and record the argv it was handed."""
    recorded = {}

    def fake_run(cmd, stdin_text, timeout):
        recorded["cmd"] = cmd
        recorded["stdin"] = stdin_text
        return stdout

    monkeypatch.setattr(module, "_run", fake_run)
    return recorded


def test_default_openai_model_is_passed_to_the_codex_cli(bridge, monkeypatch):
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ok"},
        }
    )
    recorded = _capture(bridge, monkeypatch, stdout)

    result = bridge.complete_codex("prompt", "", Config.OPENAI_MODEL, 30)

    assert result["text"] == "ok"
    cmd = recorded["cmd"]
    assert "-m" in cmd, (
        f"codex would ignore {Config.OPENAI_MODEL!r} and answer on its own default"
    )
    assert cmd[cmd.index("-m") + 1] == Config.OPENAI_MODEL


def test_unknown_openai_model_would_be_dropped(bridge, monkeypatch):
    """The guard the test above relies on is real, not a no-op."""
    recorded = _capture(bridge, monkeypatch, "")

    bridge.complete_codex("prompt", "", "gpt-5-mini", 30)

    assert "-m" not in recorded["cmd"]


def test_default_anthropic_model_is_passed_to_the_claude_cli(bridge, monkeypatch):
    recorded = _capture(bridge, monkeypatch, json.dumps({"result": "ok"}))

    result = bridge.complete_claude("prompt", "", Config.ANTHROPIC_MODEL, 30)

    assert result["text"] == "ok"
    cmd = recorded["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == Config.ANTHROPIC_MODEL


@pytest.mark.skipif(
    bool(os.environ.get("ANTHROPIC_MODEL") or os.environ.get("OPENAI_MODEL")),
    reason="the environment overrides the defaults this test is about",
)
def test_configured_defaults_are_the_current_models():
    """A model id is a deployment fact, so a change to it is a deliberate one.

    Skipped rather than reloaded when the environment overrides them: other
    modules hold `from config import Config`, so reimporting the module hands
    them a second Config object and quietly breaks tests that patch the first.
    """
    assert Config.ANTHROPIC_MODEL == "claude-sonnet-5"
    assert Config.OPENAI_MODEL == "gpt-5.6-terra"
