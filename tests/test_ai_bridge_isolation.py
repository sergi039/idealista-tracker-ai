"""The subscription bridge runs the CLIs cold, and cleans up after itself (#201).

The bridge is a host-side script outside the Flask app, so it is loaded here by
path rather than imported as a package.
"""

import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("ai_bridge_under_test", _BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge():
    return _load_bridge()


@pytest.fixture
def recorded_cmd(bridge, monkeypatch):
    """Capture the argv the bridge builds, without running a CLI."""
    captured = {}

    def fake_which(name):
        return f"/fake/bin/{name}"

    def fake_run(cmd, stdin_text, timeout):
        captured["cmd"] = cmd
        captured["stdin"] = stdin_text
        return captured.get("stdout", "")

    monkeypatch.setattr(bridge, "_which", fake_which)
    monkeypatch.setattr(bridge, "_run", fake_run)
    return captured


# --- cold-start isolation -------------------------------------------------


def test_codex_ignores_the_owners_personal_config(bridge, recorded_cmd):
    """A profile would still layer on the base config; only ignoring it isolates.

    This is also what drops the fast service tier, which bills 2.5x the
    standard credit rate for 1.5x the speed.
    """
    recorded_cmd["stdout"] = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 10}}
    )
    bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)

    cmd = recorded_cmd["cmd"]
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert "--ephemeral" in cmd
    assert "-p" not in cmd and "--profile" not in cmd


def test_codex_runs_without_sub_agents_or_interactive_features(bridge, recorded_cmd):
    """multi_agent is stable and on by default; it spawned the researchers."""
    recorded_cmd["stdout"] = json.dumps({"type": "turn.completed", "usage": {}})
    bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)

    cmd = recorded_cmd["cmd"]
    disabled = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--disable"}
    assert "multi_agent" in disabled
    assert {"shell_tool", "plugins", "apps", "hooks", "goals"} <= disabled


def test_codex_asks_for_low_reasoning_effort(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps({"type": "turn.completed", "usage": {}})
    bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)

    cmd = recorded_cmd["cmd"]
    assert f"model_reasoning_effort={bridge.CODEX_EFFORT}" in cmd
    assert bridge.CODEX_EFFORT == "low"


def test_claude_runs_with_customizations_off(bridge, recorded_cmd):
    """--safe-mode drops CLAUDE.md, skills, plugins, hooks and MCP.

    Auth survives it, which is what keeps the call on the subscription. --bare
    would not: it never reads OAuth or the keychain.
    """
    recorded_cmd["stdout"] = json.dumps({"result": "{}", "usage": {}})
    bridge.complete_claude("prompt", "sys", "claude-sonnet-5", 60)

    cmd = recorded_cmd["cmd"]
    assert "--safe-mode" in cmd
    assert "--bare" not in cmd
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--effort") + 1] == bridge.CLAUDE_EFFORT
    # An empty string, as its own argv entry, is what disables every tool.
    assert cmd[cmd.index("--tools") + 1] == ""


def test_workdir_is_empty_and_outside_any_repository(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    path = Path(bridge.workdir())

    assert path.is_dir()
    assert list(path.iterdir()) == []
    assert not (path / ".git").exists()
    assert not (path / "CLAUDE.md").exists()
    assert not (path / "AGENTS.md").exists()


def test_the_cli_runs_in_that_workdir_not_the_repository(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    printed = bridge._run(
        [sys.executable, "-c", "import os; print(os.getcwd())"], "", 30
    )
    assert printed.strip() == os.path.realpath(tmp_path / "cold")


# --- cancellation ---------------------------------------------------------


def _grandchild_script(pid_file: Path) -> str:
    """A process that ignores SIGTERM and leaves a child that does the same.

    This is the shape that leaked: `codex` is a node wrapper whose grandchild
    does the work, so killing the direct child alone left the real process
    running for minutes on the owner's quota.
    """
    return (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        '"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "\n'
        '"time.sleep(300)"])\n'
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(300)\n"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_timeout_kills_the_whole_process_tree(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(bridge.BridgeTimeout):
        bridge._run([sys.executable, "-c", _grandchild_script(pid_file)], "", 2)

    grandchild = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_alive(grandchild):
        time.sleep(0.1)
    assert not _pid_alive(grandchild), (
        f"grandchild {grandchild} survived the timeout — this is the leak of #201"
    )


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_normal_exit_is_not_killed(bridge, tmp_path, monkeypatch):
    """Cancellation belongs to the timeout path, never to a finished run."""
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    out = bridge._run([sys.executable, "-c", "print('done')"], "", 30)
    assert out.strip() == "done"


def test_a_failing_cli_is_reported_as_an_error_not_a_timeout(
    bridge, tmp_path, monkeypatch
):
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    with pytest.raises(bridge.BridgeError) as excinfo:
        bridge._run([sys.executable, "-c", "import sys; sys.exit(3)"], "", 30)
    assert not isinstance(excinfo.value, bridge.BridgeTimeout)


# --- bounded concurrency --------------------------------------------------


def test_runs_are_bounded(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    monkeypatch.setattr(bridge, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(bridge, "_RUN_SLOTS", threading.BoundedSemaphore(1))

    sleeper = [sys.executable, "-c", "import time; time.sleep(0.6)"]
    started = time.monotonic()
    threads = [
        threading.Thread(target=lambda: bridge._run(sleeper, "", 30)) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Serialised: two 0.6 s runs cannot both finish inside one of them.
    assert time.monotonic() - started >= 1.0


def test_a_full_bridge_reports_busy_instead_of_queueing_forever(
    bridge, tmp_path, monkeypatch
):
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    monkeypatch.setattr(bridge, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(bridge, "_RUN_SLOTS", threading.BoundedSemaphore(1))

    holder = threading.Thread(
        target=lambda: bridge._run(
            [sys.executable, "-c", "import time; time.sleep(3)"], "", 30
        )
    )
    holder.start()
    try:
        time.sleep(0.3)
        with pytest.raises(bridge.BridgeBusy):
            bridge._run([sys.executable, "-c", "pass"], "", 1)
    finally:
        holder.join()


# --- stream contract ------------------------------------------------------


def test_codex_answer_is_the_last_message_not_every_message(bridge, recorded_cmd):
    """Concatenating messages produced text that parses as neither prose nor JSON."""
    recorded_cmd["stdout"] = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Let me think."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"verdict":"fair"}'},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}),
        ]
    )
    result = bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)
    assert result["text"] == '{"verdict":"fair"}'


def test_a_truncated_codex_stream_is_a_failure(bridge, recorded_cmd):
    """Without turn.completed the run was cut off; a partial answer is not an answer."""
    recorded_cmd["stdout"] = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"verdict":"fai'},
        }
    )
    with pytest.raises(bridge.BridgeError, match="turn.completed"):
        bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)


def test_codex_usage_carries_the_counts_that_expose_a_regression(bridge, recorded_cmd):
    recorded_cmd["stdout"] = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 16867,
                "cached_input_tokens": 11008,
                "output_tokens": 306,
                "reasoning_output_tokens": 76,
            },
        }
    )
    usage = bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)["usage"]
    assert usage["cached_input_tokens"] == 11008
    assert usage["reasoning_output_tokens"] == 76


# --- HTTP status codes ----------------------------------------------------


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


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        ("BridgeTimeout", 504),
        ("BridgeBusy", 503),
        ("BridgeError", 502),
    ],
)
def test_failures_map_to_distinct_statuses(bridge, monkeypatch, error, expected_status):
    """A caller must tell "we gave up waiting" from "the CLI answered badly"."""
    monkeypatch.setattr(bridge, "TOKEN", "test-token")

    def failing(prompt, system, model, timeout):
        raise getattr(bridge, error)("boom")

    monkeypatch.setitem(bridge.PROVIDERS, "claude", failing)
    server, _ = _serve(bridge)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, {"provider": "claude", "prompt": "hi"})
        assert excinfo.value.code == expected_status
    finally:
        server.shutdown()
        server.server_close()


def test_a_successful_call_still_answers_200(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    monkeypatch.setitem(
        bridge.PROVIDERS,
        "claude",
        lambda *a: {"text": "{}", "usage": {}, "provider": "claude"},
    )
    server, _ = _serve(bridge)
    try:
        response = _post(server, {"provider": "claude", "prompt": "hi"})
        assert response.status == 200
        assert json.loads(response.read())["text"] == "{}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_subprocess_never_sees_an_api_key(bridge, monkeypatch, tmp_path):
    """A key in the environment would take precedence over the subscription."""
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    # Deliberately not shaped like a real key: what is asserted is that the
    # variable is absent, and a realistic-looking literal only trips secret
    # scanners.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-the-cli")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-cli-either")
    printed = bridge._run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('ANTHROPIC_API_KEY'), "
            "os.environ.get('OPENAI_API_KEY'))",
        ],
        "",
        30,
    )
    assert printed.strip() == "None None"


def test_bridge_has_no_shell_invocation():
    """argv is built as a list; a shell would make the prompt injectable."""
    source = _BRIDGE_PATH.read_text()
    assert "shell=True" not in source
    assert "subprocess.run(" not in source or "start_new_session" in source
