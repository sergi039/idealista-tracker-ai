"""The bridge falls back to the owner's other Claude profile on an ACCOUNT refusal.

Measured 2026-09-16 on the mini: the launcher pins the bridge to profile B
(`CLAUDE_CONFIG_DIR=~/.claude-b`), B answered `429 You've hit your weekly
limit · resets Sep 18`, profile A on the same machine answered OK, and the
only way to reach A was a hand-started second bridge. These tests run the real
`_run` against a fake `claude` on disk so the environment the CLI actually
sees -- the profile variables, and their absence for the default profile --
is what is asserted, not a recorded argv.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from tests.js_harness import load_bridge

# The fake answers per profile: the value of CLAUDE_CONFIG_DIR (or "default")
# maps to (exit code, JSON payload). Every call is appended to a log the test
# reads back, with the profile variables the process saw.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
plan = json.load(open(os.environ["FAKE_CLAUDE_PLAN"]))
profile = os.environ.get("CLAUDE_CONFIG_DIR") or "default"
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({
        "profile": profile,
        "securestorage": os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"),
    }) + "\\n")
code, payload = plan[profile]
sys.stdout.write(json.dumps(payload))
sys.exit(code)
"""

WEEKLY_LIMIT = {
    "is_error": True,
    "api_error_status": 429,
    "result": "You've hit your weekly limit · resets Sep 18 at 6am (Europe/Madrid)",
    "usage": {},
}
NOT_LOGGED_IN = {
    "is_error": True,
    "api_error_status": None,
    "result": "Not logged in · Please run /login",
    "usage": {},
}
BAD_REQUEST = {
    "is_error": True,
    "api_error_status": 400,
    "result": "prompt is too long",
    "usage": {},
}
OK = {"is_error": False, "result": "OK", "usage": {"input_tokens": 2}}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on disk whose answer depends on the profile it was run as."""
    bridge = load_bridge("ai_bridge_profiles_under_test")
    script = tmp_path / "claude"
    # Shebang at byte 0 (#284): a leading newline makes the kernel run it as
    # a shell script and the failure reads as a segfault in the wrong program.
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    plan_path = tmp_path / "plan.json"
    log_path = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CLAUDE_PLAN", str(plan_path))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, raising=False)
    monkeypatch.setattr(bridge, "_which", lambda name: str(script))

    class Harness:
        def __init__(self):
            self.bridge = bridge

        def plan(self, **answers):
            plan_path.write_text(json.dumps(answers))

        def calls(self):
            if not log_path.exists():
                return []
            return [json.loads(line) for line in log_path.read_text().splitlines()]

    return Harness()


def _profile_b(tmp_path: Path) -> str:
    return str(tmp_path / "claude-b")


def test_weekly_limit_on_the_primary_falls_back_to_the_default_profile(
    fake_claude, monkeypatch, tmp_path
):
    """B answers 429; the same request is re-run as A, and A's answer is served."""
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", profile_b)
    monkeypatch.setenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, "default")
    fake_claude.plan(**{profile_b: [1, WEEKLY_LIMIT], "default": [0, OK]})

    result = bridge.complete_claude("prompt", "", "", 60)

    assert result["text"] == "OK"
    assert result["claude_profile"] == "default"
    calls = fake_claude.calls()
    assert [call["profile"] for call in calls] == [profile_b, "default"]
    # The default profile must not inherit B's keychain namespace: the CLI
    # would read A's config against B's stored session.
    assert calls[1]["securestorage"] is None


def test_a_not_logged_in_primary_falls_back_too(fake_claude, monkeypatch, tmp_path):
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    monkeypatch.setenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, "default")
    fake_claude.plan(**{profile_b: [1, NOT_LOGGED_IN], "default": [0, OK]})

    assert bridge.complete_claude("prompt", "", "", 60)["claude_profile"] == "default"


def test_a_fallback_can_name_a_directory_and_sets_both_profile_variables(
    fake_claude, monkeypatch, tmp_path
):
    """Primary is the default profile; the fallback is B, selected explicitly."""
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, profile_b)
    fake_claude.plan(**{"default": [1, WEEKLY_LIMIT], profile_b: [0, OK]})

    result = bridge.complete_claude("prompt", "", "", 60)

    assert result["claude_profile"] == profile_b
    calls = fake_claude.calls()
    assert calls[-1] == {"profile": profile_b, "securestorage": profile_b}


def test_a_refusal_about_the_request_is_never_re_spent_on_the_other_profile(
    fake_claude, monkeypatch, tmp_path
):
    """A 400 is the request's fault; the second subscription must not pay for it."""
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    monkeypatch.setenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, "default")
    fake_claude.plan(**{profile_b: [1, BAD_REQUEST], "default": [0, OK]})

    with pytest.raises(bridge.BridgeError, match="prompt is too long"):
        bridge.complete_claude("prompt", "", "", 60)
    assert [call["profile"] for call in fake_claude.calls()] == [profile_b]


def test_without_a_configured_fallback_the_limit_surfaces_as_it_did_before(
    fake_claude, monkeypatch, tmp_path
):
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    fake_claude.plan(**{profile_b: [1, WEEKLY_LIMIT], "default": [0, OK]})

    with pytest.raises(bridge.BridgeError, match="weekly limit"):
        bridge.complete_claude("prompt", "", "", 60)
    assert [call["profile"] for call in fake_claude.calls()] == [profile_b]


def test_every_profile_refusing_names_each_of_them(fake_claude, monkeypatch, tmp_path):
    """The error says which subscription said what, so the owner knows what to wait for."""
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    monkeypatch.setenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, "default")
    fake_claude.plan(**{profile_b: [1, WEEKLY_LIMIT], "default": [1, NOT_LOGGED_IN]})

    with pytest.raises(bridge.BridgeError) as excinfo:
        bridge.complete_claude("prompt", "", "", 60)
    message = str(excinfo.value)
    assert "every profile" in message
    assert f"{profile_b}: 429" in message
    assert "default: Not logged in" in message


def test_profile_order_is_primary_first_deduplicated_and_expanded(
    fake_claude, monkeypatch
):
    bridge = fake_claude.bridge
    home = os.path.expanduser("~")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{home}/.claude-b")
    monkeypatch.setenv(
        bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR,
        os.pathsep.join(["default", "~/.claude-b", "", " default "]),
    )
    assert bridge._claude_profiles() == [f"{home}/.claude-b", None]


def test_a_successful_primary_never_touches_the_fallback(
    fake_claude, monkeypatch, tmp_path
):
    bridge = fake_claude.bridge
    profile_b = _profile_b(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    monkeypatch.setenv(bridge.CLAUDE_FALLBACK_CONFIG_DIRS_VAR, "default")
    fake_claude.plan(**{profile_b: [0, OK], "default": [0, OK]})

    result = bridge.complete_claude("prompt", "", "", 60)

    assert result["claude_profile"] == profile_b
    assert [call["profile"] for call in fake_claude.calls()] == [profile_b]
