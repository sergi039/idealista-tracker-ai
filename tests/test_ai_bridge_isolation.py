"""The subscription bridge runs the CLIs cold, and cleans up after itself (#201).

The bridge is a host-side script outside the Flask app, so it is loaded here by
path rather than imported as a package.
"""

import ast
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.js_harness import load_bridge

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_bridge.py"


@pytest.fixture
def bridge():
    return load_bridge("ai_bridge_under_test")


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
def test_a_normal_exit_is_never_signalled(bridge, tmp_path, monkeypatch):
    """Cancellation belongs to the timeout path, never to a finished run.

    Asserting on the output alone would pass against a `finally: killpg`, so
    watch the signal itself.
    """
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    signalled = []
    real_killpg = os.killpg

    def spy(pgid, sig):
        signalled.append((pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(bridge.os, "killpg", spy)
    out = bridge._run([sys.executable, "-c", "print('done')"], "", 30)

    assert out.strip() == "done"
    assert signalled == []


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
    # This test is about serialisation, not the floor: at the production
    # MIN_RUN_SECONDS (45.0, #206 item 4) a 30 s timeout leaves almost no
    # room to queue at all (`_queue_floor(30) == 29.5`), so the second thread
    # below would spuriously see BridgeBusy instead of the delay it is meant
    # to observe. Lowered here the same way test_the_budget_covers_queueing_
    # and_running_together does.
    monkeypatch.setattr(bridge, "MIN_RUN_SECONDS", 0.5)

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


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        # A short call granted a slot at once keeps its whole budget: these
        # three stay below MIN_RUN_SECONDS regardless of where that floor is
        # tuned (production is 45.0, see MIN_RUN_SECONDS's own comment), so
        # they exercise the `timeout - grace` cap rather than the floor.
        (2, 1.5),
        (1, 0.5),
        (0.4, 0.0),
        # A long call that queued its way down under MIN_RUN_SECONDS is
        # refused, at the production floor (#206 item 4: raised from 5.0).
        (8, 7.5),
        (600, 45.0),
    ],
)
def test_the_queue_floor_never_rejects_a_call_that_did_not_queue(
    bridge, timeout, expected
):
    """Only time spent queueing may push a run under the floor."""
    assert bridge.MIN_RUN_SECONDS == pytest.approx(45.0), (
        "this test's expectations are pinned to the production default; "
        "update both together if MIN_RUN_SECONDS is retuned"
    )
    assert bridge._queue_floor(timeout) == pytest.approx(expected)


def test_a_queued_call_below_the_minimum_is_refused_not_started(bridge, monkeypatch):
    """A budget that queues below the production MIN_RUN_SECONDS is refused.

    Uses a timeout comfortably larger than MIN_RUN_SECONDS so the floor here
    is bound by the production minimum itself, not by the `timeout - grace`
    cap that binds for short-lived calls (see the parametrized
    `_queue_floor` tests above) -- this test is not run at a lowered floor.
    """
    monkeypatch.setattr(bridge, "MAX_CONCURRENT_RUNS", 1)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(bridge, "_RUN_SLOTS", slots)

    timeout = bridge.MIN_RUN_SECONDS + 1
    hold_seconds = 2.0  # leaves timeout - hold_seconds < MIN_RUN_SECONDS

    held = threading.Event()

    def hold_the_slot():
        slots.acquire()
        held.set()
        time.sleep(hold_seconds)
        slots.release()

    holder = threading.Thread(target=hold_the_slot)
    holder.start()
    try:
        assert held.wait(5)
        with pytest.raises(bridge.BridgeBusy):
            # Never reaches a CLI: this argv would fail loudly if it did.
            bridge._run(["/nonexistent/cli"], "", timeout)
    finally:
        holder.join()


def test_the_budget_covers_queueing_and_running_together(bridge, tmp_path, monkeypatch):
    """One budget, not one per stage.

    Waiting a full timeout for a slot and then granting a full timeout to the
    run lets a call live for two budgets, while the HTTP client gives up after
    one — leaving a paid CLI running with nobody to receive its answer. The
    floor is lowered here deliberately: this test is about the deadline, and
    the floor is covered at its production value by the test above.
    """
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    monkeypatch.setattr(bridge, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(bridge, "MIN_RUN_SECONDS", 0.5)
    monkeypatch.setattr(bridge, "KILL_GRACE_SECONDS", 1.0)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(bridge, "_RUN_SLOTS", slots)

    held = threading.Event()

    def hold_the_slot():
        slots.acquire()
        held.set()
        time.sleep(3)
        slots.release()

    holder = threading.Thread(target=hold_the_slot)
    holder.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        with pytest.raises(bridge.BridgeTimeout):
            bridge._run([sys.executable, "-c", "import time; time.sleep(30)"], "", 4)
        elapsed = time.monotonic() - started
    finally:
        holder.join()

    # ~3 s queued plus the rest of the 4 s budget. Two budgets would be ~7 s.
    assert elapsed < 5.5, f"the call took {elapsed:.1f}s, more than its 4s budget"


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


def test_codex_answer_survives_a_trailing_closing_remark(bridge, recorded_cmd):
    """The mirror case of the test above (#206 item 2).

    codex sometimes emits the JSON answer and then a closing remark ("Let me
    know if you need anything else."). The old "always take the last
    message" rule let that remark win and silently discarded the analysis;
    the bridge answered 200 with prose the app's json.loads then failed on.
    """
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
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}),
        ]
    )
    result = bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)
    assert result["text"] == '{"verdict":"fair"}'


def test_codex_answer_tolerates_a_fenced_json_body(bridge, recorded_cmd):
    """A ```-fenced JSON answer must still be recognised as JSON.

    `services/property_ai_service.py`'s `_clean_json_text` strips a fence
    before parsing, so the bridge's own "does this look like JSON" check has
    to tolerate the same fence or it would judge a perfectly good fenced
    answer as prose and let a trailing remark win instead.
    """
    fenced = '```json\n{"verdict": "fair"}\n```'
    recorded_cmd["stdout"] = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": fenced},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Done."},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    result = bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)
    assert result["text"] == fenced


def test_codex_answer_falls_back_to_the_last_message_when_nothing_parses(
    bridge, recorded_cmd
):
    """No message parsing as JSON is a real failure and must surface as one.

    Falling back to the last non-empty message (the old behaviour) rather
    than an empty string keeps that failure visible to the app's own
    json.loads instead of masking it as an empty success.
    """
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
    result = bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)
    assert result["text"] == "I could not complete this analysis."


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

    def failing(prompt, system, model, timeout, schema=None):
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


# Every one of these is read by an installed CLI and would move the call off
# the subscription onto a billed path. The launcher sources the whole .env, so
# any of them can arrive here without anyone meaning it.
BILLED_PATH_VARS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
]


@pytest.mark.parametrize("variable", BILLED_PATH_VARS)
def test_the_subprocess_never_sees_a_billed_auth_path(
    bridge, monkeypatch, tmp_path, variable
):
    """A key or gateway override silently outranks the subscription session."""
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    # Deliberately not shaped like a real credential: what is asserted is that
    # the variable is absent, and a realistic literal only trips secret scanners.
    monkeypatch.setenv(variable, "must-not-reach-the-cli")
    printed = bridge._run(
        [sys.executable, "-c", f"import os; print(os.environ.get({variable!r}))"],
        "",
        30,
    )
    assert printed.strip() == "None"


# The subscription's own credentials, which share a prefix with the billed
# variables above. CLAUDE_CODE_OAUTH_TOKEN is what `claude setup-token` issues
# and it requires a subscription; CODEX_ACCESS_TOKEN is its codex counterpart.
# Sweeping them away would leave a CLI with no auth at all on a host with no
# stored session — a subscription outage dressed as isolation.
SUBSCRIPTION_VARS = {
    "CODEX_HOME": "/tmp/codex-home",
    "CLAUDE_CONFIG_DIR": "/tmp/claude-config",
    "CLAUDE_CODE_OAUTH_TOKEN": "subscription-oauth-value",
    "CODEX_ACCESS_TOKEN": "subscription-access-value",
}


@pytest.mark.parametrize("variable", sorted(SUBSCRIPTION_VARS))
def test_the_subscription_credentials_still_reach_the_cli(
    bridge, monkeypatch, tmp_path, variable
):
    """Stripping billed paths must not strip the subscription's own auth."""
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    monkeypatch.setenv(variable, SUBSCRIPTION_VARS[variable])
    printed = bridge._run(
        [sys.executable, "-c", f"import os; print(os.environ.get({variable!r}))"],
        "",
        30,
    )
    assert printed.strip() == SUBSCRIPTION_VARS[variable]


def test_an_unknown_provider_variable_is_stripped_by_default(
    bridge, monkeypatch, tmp_path
):
    """A provider variable a future CLI adds must not quietly take effect."""
    monkeypatch.setenv("AI_BRIDGE_WORKDIR", str(tmp_path / "cold"))
    monkeypatch.setenv("ANTHROPIC_SOMETHING_INVENTED_LATER", "value")
    printed = bridge._run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('ANTHROPIC_SOMETHING_INVENTED_LATER'))",
        ],
        "",
        30,
    )
    assert printed.strip() == "None"


def test_bridge_has_no_shell_invocation():
    """argv is built as a list; a shell would make the prompt injectable.

    The previous version of this test also asserted
    `"subprocess.run(" not in source or "start_new_session" in source`: since
    the bridge stopped using `subprocess.run(` outright, the left side of
    that `or` was always true and the right side -- the actual isolation
    property -- was never evaluated. Verified by mutation (#206 item 6):
    deleting `start_new_session=True` from the `Popen` call left that
    assertion green. `test_the_bridge_isolates_every_run_in_its_own_process_group`
    below replaces it with a real, AST-based check of the `Popen` call
    itself, which mutation can't fool the same way.
    """
    source = _BRIDGE_PATH.read_text()
    assert "shell=True" not in source


def test_the_bridge_isolates_every_run_in_its_own_process_group():
    """Every `subprocess.Popen` call must pass `start_new_session=True`.

    `_kill_process_group` signals the whole process group via `proc.pid`
    (see its docstring), which only works because `start_new_session=True`
    made the child its own group leader. Without it, a timeout's SIGTERM/
    SIGKILL would hit only the direct child -- `codex`'s node wrapper --
    and leave the grandchild that does the real work running and billing
    the subscription (the #201 leak this test exists to prevent).

    Parses the source with `ast` rather than grepping for the substring so a
    mutation that removes the keyword, or a second `Popen` call added later
    without it, cannot pass silently.
    """
    tree = ast.parse(_BRIDGE_PATH.read_text())
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert popen_calls, "no subprocess.Popen call found in the bridge"

    for call in popen_calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert "start_new_session" in kwargs, (
            "a Popen call is missing start_new_session=True -- a timeout "
            "would then only kill the direct child, not its process group"
        )
        value = kwargs["start_new_session"]
        assert isinstance(value, ast.Constant) and value.value is True, (
            "start_new_session must be literally True, not merely present"
        )


# --- the bridge's own shutdown (#206) --------------------------------------
#
# Everything above drives `_run()` in-process, which cannot prove this: the
# leak #206 item 1 closes is the bridge process's *own* lifecycle (a redeploy
# via `launchctl kickstart -k` sends SIGTERM while a run is in flight), not
# the per-request timeout path the cancellation tests above already cover.
# Proving it needs a real bridge process, signalled from outside.


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _BridgeOutput:
    """Reads a spawned bridge's output continuously, and keeps it.

    Two defects in one object, both found while failing to reproduce
    BRIDGE-TEST-001 (#265).

    **The pipe has to be read.** `stdout=PIPE` with nobody reading it gives the
    child a 64 KB buffer and then blocks it mid-write, forever. Startup logs
    three lines today, so it does not fire -- but the mechanism produces
    exactly the symptom that ticket is about, a bridge that never answers, and
    it should not be sitting in the harness that is supposed to diagnose it.

    **And the output is the evidence.** Before this, the bridge's own account
    of why it did not start was captured into a pipe and discarded unread by
    the `finally` that killed it, so the assertion could say `bridge never
    became healthy` and nothing else -- it could not even distinguish "exited
    instantly with a traceback" from "alive and not answering". That is why the
    failure survived an afternoon of investigation with no cause named.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._lines: list[str] = []
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            return
        for line in stream:
            self._lines.append(line)

    def text(self) -> str:
        # A short join: the reader ends when the child's stdout closes, and a
        # caller asking for the text has usually just killed it. Never block
        # the test on it.
        self._thread.join(timeout=2)
        return "".join(self._lines)


def _wait_for_health(
    port: int, deadline: float, proc: subprocess.Popen, output: _BridgeOutput
) -> None:
    """Poll until the bridge answers, and say what happened if it never does.

    `proc` and `output` are required rather than optional: a caller that has
    them and forgets to pass them gets the old blind assertion back, which is
    the whole defect. There is one caller.
    """
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            # It is already over; waiting out the deadline only delays the
            # report and tells nobody anything.
            raise AssertionError(_unhealthy(port, proc, output))
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                answered = response.status == 200
        except Exception:  # noqa: BLE001 - polling until the server accepts
            time.sleep(0.1)
            continue
        if answered:
            # Answering once is not being up. A process that served this reply
            # and then exited would otherwise be reported healthy, and the next
            # assertion in the caller would blame the fake CLI for never
            # starting -- a second wrong cause for the same failure (round 2 of
            # the independent review, 2026-09-01).
            if proc.poll() is not None:
                raise AssertionError(_unhealthy(port, proc, output))
            return
    # Polled again rather than reusing what the loop last saw. A process alive
    # at the final poll that dies in the ~1.1 s a failed health request plus its
    # backoff can take would otherwise be reported as "still running" with no
    # exit code -- losing exactly the distinction this function exists to draw
    # (independent review, 2026-09-01).
    raise AssertionError(_unhealthy(port, proc, output))


def _unhealthy(port: int, proc: subprocess.Popen, output: _BridgeOutput) -> str:
    """Say which of the two failures this is, from one fresh poll."""
    exit_code = proc.poll()
    if exit_code is not None:
        return (
            f"the bridge exited with code {exit_code} before it became "
            f"healthy on port {port}. Its own output:\n{output.text()}"
        )
    return (
        f"bridge never became healthy on port {port} within the deadline; it "
        f"is still running (pid {proc.pid}). Its own output so far:\n"
        f"{output.text()}"
    )


def _fake_claude_script(pid_file: Path) -> str:
    """A stand-in `claude` binary: spawns a grandchild that ignores SIGTERM,
    the same shape codex's node-wrapper/rust-child leaves behind (see
    `_grandchild_script` above), then blocks like a real valuation would.
    """
    return (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', "
        '"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "\n'
        '"time.sleep(300)"])\n'
        f"open({str(pid_file)!r}, 'w').write(f'{{os.getpid()}} {{grandchild.pid}}')\n"
        "time.sleep(300)\n"
    )


class TestTheHarnessKeepsTheEvidence:
    """BRIDGE-TEST-001 (#265): the failure that could not be diagnosed.

    `test_the_bridges_own_shutdown_kills_an_in_flight_run` failed on this Mac
    for about forty-five minutes on 2026-09-01 -- two full suites and three
    standalone runs across three worktrees, one of them at an untouched
    `main` -- and then stopped. Roughly 190 executions since, unloaded, under
    six-way load, and with two copies racing each other, have not reproduced
    it. **These tests do not fix that failure and are not evidence that it is
    gone.** They fix the reason it cost an afternoon and named nothing: the
    harness spawned the bridge with `stdout=PIPE`, never read it, and threw it
    away in `finally`, so the only thing the assertion could say was that the
    bridge never became healthy.
    """

    @staticmethod
    def _spawn(script: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def test_a_bridge_that_dies_names_its_cause(self):
        """Exit code and the process's own words, in the assertion."""
        proc = self._spawn(
            "import sys; print('AI_BRIDGE_TOKEN is not set'); sys.exit(3)"
        )
        output = _BridgeOutput(proc)
        try:
            with pytest.raises(AssertionError) as raised:
                _wait_for_health(_free_port(), time.monotonic() + 5, proc, output)
        finally:
            if proc.poll() is None:
                proc.kill()
        message = str(raised.value)
        assert "exited with code 3" in message
        assert "AI_BRIDGE_TOKEN is not set" in message

    def test_a_dead_bridge_is_reported_at_once(self):
        """...and without waiting out the deadline, which only delays the
        report by ten seconds and tells nobody anything."""
        proc = self._spawn("import sys; sys.exit(1)")
        output = _BridgeOutput(proc)
        started = time.monotonic()
        try:
            with pytest.raises(AssertionError):
                _wait_for_health(_free_port(), time.monotonic() + 30, proc, output)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert time.monotonic() - started < 10

    def test_a_bridge_that_dies_on_the_deadline_is_not_called_alive(self):
        """The reviewed defect: the deadline path reported "still running"
        without polling again, so a bridge that died between the loop's last
        poll and the deadline lost its exit code -- the one distinction this
        function exists to draw.

        Reproduced deterministically with a deadline already in the past, so
        the loop body never runs and the post-loop report is the only thing
        under test.
        """
        proc = self._spawn("import sys; print('late crash'); sys.exit(7)")
        output = _BridgeOutput(proc)
        assert proc.wait(timeout=10) == 7
        with pytest.raises(AssertionError) as raised:
            _wait_for_health(_free_port(), time.monotonic() - 1, proc, output)
        message = str(raised.value)
        assert "exited with code 7" in message
        assert "still running" not in message
        assert "late crash" in message

    def test_answering_once_and_dying_is_not_healthy(self, monkeypatch):
        """Round 2 of the review: the success path returned on HTTP 200 without
        asking whether the process was still there.

        Pinned at the branch rather than with a real one-shot server, and the
        reason is worth stating: a child that answers and exits does so
        concurrently with the parent reading the reply, so whether `poll()` has
        reaped it by the next statement is a race, and a test built on that
        race would be the flaky thing this file exists to remove. The fake
        below is deterministic and exercises the same branch.
        """

        class _Answered:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _DiesAfterAnswering:
            """Alive when the loop checks, gone when the reply is in.

            A stub that is dead from the start would be caught by the
            top-of-loop poll and never reach the branch under test -- which is
            what the first version of this test did, and a mutation removing
            the re-poll left all 51 tests green.
            """

            pid = 4242
            stdout = None

            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls == 1 else 7

        proc = _DiesAfterAnswering()
        output = _BridgeOutput(proc)
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Answered())
        with pytest.raises(AssertionError) as raised:
            _wait_for_health(_free_port(), time.monotonic() + 5, proc, output)
        assert "exited with code 7" in str(raised.value)
        assert "still running" not in str(raised.value)
        # It really did answer first: the loop's own poll said alive.
        assert proc.polls >= 2

    def test_a_live_but_silent_bridge_says_so(self):
        """The other half: still running, still not answering. The two used to
        be indistinguishable."""
        # `flush=True` because a child's stdout is block-buffered when it is a
        # pipe: without it the line sits in the *child's* buffer until it
        # exits, and this test is about the harness rather than about C-level
        # buffering. The real bridge logs through `logging` to stderr, which is
        # merged here by `stderr=STDOUT` and is not block-buffered, so its
        # startup lines really do arrive while it is still alive.
        proc = self._spawn(
            "import time; print('bound and idle', flush=True); time.sleep(30)"
        )
        output = _BridgeOutput(proc)
        try:
            with pytest.raises(AssertionError) as raised:
                _wait_for_health(_free_port(), time.monotonic() + 2, proc, output)
        finally:
            proc.kill()
            proc.wait(timeout=5)
        message = str(raised.value)
        assert "still running" in message
        assert str(proc.pid) in message
        assert "bound and idle" in message

    def test_the_reader_does_not_wedge_a_noisy_bridge(self):
        """An unread `PIPE` blocks the child mid-write once the 64 KB buffer
        fills, forever -- which produces exactly the symptom BRIDGE-TEST-001
        describes, a bridge that never answers. Today's bridge logs three lines
        at startup so it does not fire; the mechanism has no business sitting
        in the harness meant to diagnose it.
        """
        payload = 300_000
        proc = self._spawn(
            f"import sys; sys.stdout.write('x' * {payload}); sys.stdout.flush()"
        )
        output = _BridgeOutput(proc)
        try:
            # Without the reader thread this wait never returns.
            assert proc.wait(timeout=15) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
        assert len(output.text()) == payload


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_the_bridges_own_shutdown_kills_an_in_flight_run(tmp_path):
    """A restart must not orphan the CLI it was serving (#206 item 1).

    Points the bridge at a fake `claude` through the same `_which()` PATH
    lookup it already uses — the least invasive seam, no test-only production
    code needed. The real `claude` on this host resolves via `$HOME/.local/
    bin`, so overriding `HOME` for the spawned bridge is what lets the fake
    one win; `codex` sits at a fixed `/opt/homebrew/bin` this test cannot
    shadow without touching real installed state, so the request uses
    `claude`.
    """
    port = _free_port()
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    pid_file = tmp_path / "claude.pids"

    fake_claude = fake_bin / "claude"
    fake_claude.write_text(f"#!{sys.executable}\n" + _fake_claude_script(pid_file))
    fake_claude.chmod(0o755)

    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "fakehome")
    env["PATH"] = os.pathsep.join([str(fake_bin), env.get("PATH", "")])
    env["AI_BRIDGE_TOKEN"] = "test-token"
    env["AI_BRIDGE_HOST"] = "127.0.0.1"
    env["AI_BRIDGE_PORT"] = str(port)
    # Keeps the test fast without skipping the SIGTERM-then-SIGKILL path.
    env["AI_BRIDGE_KILL_GRACE"] = "1"
    # Every other test in this file sets this; the one that spawns a real
    # bridge subprocess did not, so it resolved `tempfile.gettempdir()/
    # ai-bridge-workdir` -- the very directory the live `com.idealista.ai-bridge`
    # LaunchAgent runs its CLIs in. The test then started its fake `claude`
    # with production's cwd. It cannot break startup, so it is not the
    # BRIDGE-TEST-001 failure; it is the harness reaching into production
    # state, and this file's own convention already says not to.
    env["AI_BRIDGE_WORKDIR"] = str(tmp_path / "bridge-workdir")

    bridge_proc = subprocess.Popen(
        [sys.executable, str(_BRIDGE_PATH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    bridge_output = _BridgeOutput(bridge_proc)
    request_thread = None
    child_pid = None
    grandchild_pid = None
    try:
        _wait_for_health(port, time.monotonic() + 10, bridge_proc, bridge_output)

        # The bridge announces its workdir on the line above the first request.
        # Asserted here rather than left to the environment variable, because
        # the variable is what a future edit would drop: without it this test
        # ran its fake `claude` in `tempfile.gettempdir()/ai-bridge-workdir`,
        # which is the live `com.idealista.ai-bridge` LaunchAgent's own cwd.
        assert f"workdir={tmp_path / 'bridge-workdir'}" in bridge_output.text()

        def _fire_request():
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/complete",
                data=json.dumps(
                    {"provider": "claude", "prompt": "hi", "timeout": 60}
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer test-token",
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=30)
            except Exception:  # noqa: BLE001 - liveness is asserted below,
                pass  # not this reply, which never arrives once the bridge dies

        request_thread = threading.Thread(target=_fire_request, daemon=True)
        request_thread.start()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.1)
        assert pid_file.exists(), "the fake claude CLI never started"
        child_pid, grandchild_pid = (int(part) for part in pid_file.read_text().split())

        # The redeploy step this closes: SIGTERM to the bridge itself while a
        # run is in flight.
        bridge_proc.send_signal(signal.SIGTERM)
        bridge_proc.wait(timeout=20)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (
            _pid_alive(child_pid) or _pid_alive(grandchild_pid)
        ):
            time.sleep(0.1)
        assert not _pid_alive(child_pid), (
            f"the fake CLI child {child_pid} survived the bridge's own "
            "shutdown - this is the #206 leak"
        )
        assert not _pid_alive(grandchild_pid), (
            f"the grandchild {grandchild_pid} survived the bridge's own "
            "shutdown - this is the #206 leak"
        )
    finally:
        if bridge_proc.poll() is None:
            bridge_proc.kill()
            bridge_proc.wait(timeout=5)
        if request_thread is not None:
            request_thread.join(timeout=5)
        # Best-effort: never leave the grandchild running even if an
        # assertion above already failed.
        for pid in (child_pid, grandchild_pid):
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
