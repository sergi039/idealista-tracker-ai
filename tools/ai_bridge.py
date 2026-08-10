#!/usr/bin/env python3
"""Subscription AI bridge.

The app runs in a Linux container; the Claude Code and Codex CLIs are macOS
binaries authenticated with the owner's *subscriptions* and cannot be mounted
into it. This process runs on the host, exposes a tiny HTTP API, and shells
out to those CLIs — so the app never needs an ANTHROPIC_API_KEY or
OPENAI_API_KEY.

Run it on the host:

    AI_BRIDGE_TOKEN=<token> python3 tools/ai_bridge.py

Endpoints:
    GET  /health          -> {"ok": true, "claude": bool, "codex": bool}
    POST /v1/complete     -> {"text": str, "usage": {...}, "provider": str}

Every request must carry `Authorization: Bearer $AI_BRIDGE_TOKEN`; the socket
listens on all interfaces because Docker Desktop reaches the host through its
own gateway, so the token is the actual access control.

Both CLIs are *agents*, and this bridge wants a single completion out of them
(issue #201). Left alone they behave accordingly: codex read the owner's
`~/.codex/config.toml` (`model_reasoning_effort = "ultra"`, a fast service tier
billing 2.5x the standard credit rate), spawned research sub-agents, and turned
a 1100-token prompt into 57k input tokens — 4m50s per listing, and a 600 s
timeout that a real analysis actually hit. claude ran from the app repository
and carried 21 KB of project CLAUDE.md into every valuation. So each invocation
here is deliberately *cold*: personal configuration ignored, tools and
sub-agents off, an empty working directory, low reasoning effort.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("ai_bridge")

HOST = os.environ.get("AI_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_BRIDGE_PORT", "5061"))
TOKEN = os.environ.get("AI_BRIDGE_TOKEN", "")
DEFAULT_TIMEOUT = int(os.environ.get("AI_BRIDGE_TIMEOUT", "300"))
MAX_BODY_BYTES = 512 * 1024

# Reasoning effort for both CLIs. These are single-shot valuations against a
# prompt the app has already assembled, not open-ended engineering: measured on
# codex, "low" answered in 9.8 s where the inherited "ultra" took 35.7 s and
# spent 982 reasoning tokens against 76.
CODEX_EFFORT = os.environ.get("AI_BRIDGE_CODEX_EFFORT", "low")
CLAUDE_EFFORT = os.environ.get("AI_BRIDGE_CLAUDE_EFFORT", "low")

# codex features that only make sense for an interactive coding session. The
# first one is the expensive default: `multi_agent` is stable and on, and it is
# what spawned the /root/rental_benchmarks and /root/build_costs researchers.
CODEX_DISABLED_FEATURES = (
    "multi_agent",
    "shell_tool",
    "plugins",
    "apps",
    "hooks",
    "goals",
)

# One heavy CLI run per slot. The bridge is a ThreadingHTTPServer and the app
# fires Claude and codex together on one Enrich click, so without a bound the
# host can end up with several reasoning agents competing — the "~7 minutes
# under parallel load" of #195.
MAX_CONCURRENT_RUNS = max(1, int(os.environ.get("AI_BRIDGE_MAX_CONCURRENT", "2")))
_RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)

# How long a timed-out process group gets between SIGTERM and SIGKILL.
KILL_GRACE_SECONDS = float(os.environ.get("AI_BRIDGE_KILL_GRACE", "5"))

# Below this much of the budget left after queueing, starting a CLI only buys a
# timeout: report a busy bridge instead of a run that cannot finish.
#
# Measured (#206 item 4): the fastest synthetic codex run (above) was 9.8 s,
# but that is a warm CLI answering a trivial stub prompt. Through production,
# the first two real analyses took 41.1 s (codex) and 19.4 s (claude), and a
# later run measured 26.0 s. 5 s was below all of them, so a call that queued
# down to "5 s left" was granted a slot anyway and started a run that could
# not possibly finish -- holding one of only two slots for the rest of its
# budget plus up to 3 * KILL_GRACE_SECONDS of kill and drain on top. The floor
# is set just above the slowest real run observed (41.1 s) so a slot is only
# granted when there is a realistic chance the run finishes in it.
MIN_RUN_SECONDS = float(os.environ.get("AI_BRIDGE_MIN_RUN_SECONDS", "45"))
# Slack that keeps an immediately-granted slot from looking like a queue wait.
QUEUE_FLOOR_GRACE_SECONDS = 0.5

# Provider credentials and endpoint overrides never reach a CLI. See _cli_env.
# The prefixes are the net; these names are the ones whose presence is worth
# saying out loud, because each of them moves a call off the subscription and
# onto a billed path, and each is read by an installed CLI today.
AUTH_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CODEX_", "CLAUDE_CODE_")
CREDENTIAL_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
    }
)
# The subscription's own credentials. Sharing a prefix with the billed
# variables above is exactly why they need naming: `CLAUDE_CODE_OAUTH_TOKEN`
# (what `claude setup-token` issues, and it *requires* a subscription) and
# `CODEX_ACCESS_TOKEN` are how a headless host authenticates without a key, so
# sweeping them away would leave a CLI with no auth at all wherever no stored
# session exists. Both are read by the installed binaries.
AUTH_ENV_KEEP = frozenset(
    {
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_ACCESS_TOKEN",
    }
)

# The CLIs live outside the login shell's PATH when launched from a LaunchAgent.
EXTRA_PATH = os.pathsep.join(
    [
        os.path.expanduser("~/.local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
)


def _cli_env() -> dict:
    """The environment a CLI is allowed to see.

    Anything that can point a CLI at a billed API path instead of the owner's
    subscription session is removed. Two names were not enough: `codex` also
    reads `CODEX_API_KEY`, and `claude` reads `ANTHROPIC_AUTH_TOKEN`,
    `ANTHROPIC_BASE_URL` and the `CLAUDE_CODE_USE_BEDROCK` / `_USE_VERTEX`
    switches (all four verified present in the installed binaries). The
    launcher sources the whole `.env`, so any of them could arrive here without
    anyone meaning it — and a key silently wins over the subscription, which is
    the one outcome this bridge exists to prevent.

    Stripping by prefix rather than by list is deliberate: a provider variable
    added by a future CLI release is removed by default instead of quietly
    taking effect. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` are the exceptions —
    they locate the OAuth session itself.
    """
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([EXTRA_PATH, env.get("PATH", "")])

    removed = [
        name
        for name in list(env)
        if name not in AUTH_ENV_KEEP
        and (name.startswith(AUTH_ENV_PREFIXES) or name in CREDENTIAL_ENV_NAMES)
    ]
    for name in removed:
        env.pop(name, None)

    # Names only: the values are exactly what must not be logged. A credential
    # is worth a warning — it means the environment really did hold a billed
    # path — while the rest of the prefix sweep is routine hygiene.
    credentials = sorted(set(removed) & CREDENTIAL_ENV_NAMES)
    if credentials:
        LOG.warning("kept a billed auth path away from the CLI: %s", credentials)
    routine = sorted(set(removed) - CREDENTIAL_ENV_NAMES)
    if routine:
        LOG.debug("stripped inherited CLI variables: %s", routine)
    return env


def _which(name: str) -> str | None:
    return shutil.which(name, path=_cli_env()["PATH"])


def workdir() -> str:
    """An empty directory outside any repository, used as the CLI's cwd.

    Both CLIs read their surroundings: codex picks up AGENTS.md, project config
    and execpolicy rules, claude discovers CLAUDE.md upward from the cwd. The
    LaunchAgent starts this bridge inside the app repository, so every listing
    valuation used to carry the project's engineering rules. Nothing about a
    house in Asturias depends on them.
    """
    path = os.environ.get("AI_BRIDGE_WORKDIR") or os.path.join(
        tempfile.gettempdir(), "ai-bridge-workdir"
    )
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _log_usage(provider: str, usage: dict) -> None:
    """Record what the run actually cost.

    The context bloat behind #201 was invisible because only input/output made
    it out of here: the cached and reasoning counts are where an inherited
    "ultra" effort or a re-enabled sub-agent shows up first.
    """
    LOG.info(
        "%s usage: input=%s cached=%s output=%s reasoning=%s",
        provider,
        usage.get("input_tokens"),
        usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens"),
        usage.get("output_tokens"),
        usage.get("reasoning_output_tokens"),
    )


class BridgeError(RuntimeError):
    """A CLI call could not be completed."""


class BridgeTimeout(BridgeError):
    """The CLI outlived its budget and was killed."""


class BridgeBusy(BridgeError):
    """Every run slot was taken for the whole wait."""


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just is not ours to signal any more.
        return True
    return True


def _kill_process_group(proc: subprocess.Popen, grace: float) -> None:
    """SIGTERM the run's whole process group, then SIGKILL what survives.

    Killing `proc` alone is what leaked processes before #201: `codex` is a
    node wrapper around a rust binary, so the process that does the work is a
    *grandchild*. One measured timeout kept that grandchild running five more
    minutes, finishing an answer nobody read and billing the subscription for
    it. `start_new_session=True` puts the whole run in its own process group
    whose id is the direct child's pid, so read it from `proc.pid` rather than
    `os.getpgid()`, which fails once the wrapper exits.
    """
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        LOG.warning("cannot signal process group %s", pgid)
        return

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        # Reap the direct child first: a zombie still counts as a group member,
        # so an un-reaped wrapper would make the group look alive forever.
        try:
            proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            continue
        if not _group_alive(pgid):
            return
        break

    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        LOG.error("process group %s did not die after SIGKILL", pgid)


# Every run in flight, so the bridge's own shutdown can find and kill them
# (#206): `start_new_session=True` detaches each CLI into its own process
# group precisely so a per-request timeout can signal it without touching the
# bridge — but that detachment also means the bridge's own death (a redeploy
# via `launchctl kickstart -k`) never reaches the child on its own. Popen
# objects are tracked rather than bare pgids because `_kill_process_group`
# reaps through `proc.wait()`, which only the object that owns the OS-level
# child relationship can do safely; `proc.pid` doubles as the pgid since the
# child was started with `start_new_session=True`. `subprocess.Popen.wait()`
# is internally lock-guarded, so calling it from the signal handler's thread
# while a worker thread is concurrently blocked inside `communicate()` on the
# same object is safe.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_PROCS: set[subprocess.Popen] = set()


def _track_run(proc: subprocess.Popen) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT_PROCS.add(proc)


def _untrack_run(proc: subprocess.Popen) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT_PROCS.discard(proc)


def _kill_inflight_runs() -> None:
    """Kill every run's process group the bridge still remembers.

    Called from the SIGTERM/SIGINT handler on the main thread. A worker
    thread only ever holds `_INFLIGHT_LOCK` for the instant it takes to add
    or discard one entry, so the lock is held just long enough to snapshot
    the set and released before any killing starts: `_kill_process_group` can
    block for up to `2 * KILL_GRACE_SECONDS` per run, and holding the lock for
    that long would stall a worker thread that is simply trying to record its
    own run finishing — not a deadlock, but exactly the kind of avoidable
    stall this function exists to not cause.
    """
    with _INFLIGHT_LOCK:
        procs = list(_INFLIGHT_PROCS)
    if procs:
        LOG.info("killing %d in-flight run(s) before shutdown", len(procs))
    for proc in procs:
        _kill_process_group(proc, KILL_GRACE_SECONDS)


def _queue_floor(timeout: float) -> float:
    """The least budget worth starting a run with, once queueing has eaten in.

    Capped just under the request's own budget, because only time spent
    queueing can push the remainder below it: a caller that asked for a short
    timeout and got a slot at once must still get its run, while a call that
    waited its way down to a remainder it cannot answer in is refused instead
    of started.
    """
    return max(0.0, min(MIN_RUN_SECONDS, timeout - QUEUE_FLOOR_GRACE_SECONDS))


def _run(cmd: list[str], stdin_text: str, timeout: int) -> str:
    """Run one CLI to completion, or kill its whole process tree trying.

    `timeout` is the budget for the whole call, queueing included. Spending it
    twice — once waiting for a slot, once running — would let a request live for
    two budgets while the HTTP client gave up after one, leaving an expensive
    CLI running with nobody left to receive its answer.
    """
    deadline = time.monotonic() + timeout
    if not _RUN_SLOTS.acquire(timeout=timeout):
        raise BridgeBusy(
            f"all {MAX_CONCURRENT_RUNS} run slots were busy for {timeout}s"
        )
    try:
        budget = deadline - time.monotonic()
        if budget < _queue_floor(timeout):
            raise BridgeBusy(
                f"a slot came free with {budget:.1f}s of the {timeout}s budget "
                "left, too little to answer in"
            )
        proc = subprocess.Popen(  # noqa: S603 - argv is built here, never shell
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_cli_env(),
            cwd=workdir(),
            start_new_session=True,
        )
        _track_run(proc)
        try:
            # Re-read the clock: starting a process is not free, and that cost
            # belongs inside the deadline rather than on top of it.
            try:
                stdout, stderr = proc.communicate(
                    stdin_text, timeout=max(0.0, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                _kill_process_group(proc, KILL_GRACE_SECONDS)
                # Drain the pipes the killed process left behind.
                try:
                    proc.communicate(timeout=KILL_GRACE_SECONDS)
                except (subprocess.TimeoutExpired, ValueError):
                    pass
                raise BridgeTimeout(
                    f"{cmd[0]} timed out after {budget:.0f}s of a {timeout}s budget"
                ) from None
        finally:
            # Every exit from this run — a normal finish, the timeout path
            # above, or any other exception — must stop counting it as
            # in-flight, or the bridge's own shutdown handler would try to
            # kill a process group that no longer needs it (harmless, since
            # _kill_process_group tolerates a dead target) while leaking
            # entries for runs that finished cleanly.
            _untrack_run(proc)
    finally:
        _RUN_SLOTS.release()

    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()[:2000]
        raise BridgeError(f"{cmd[0]} exited {proc.returncode}: {detail}")
    return stdout


def complete_claude(
    prompt: str, system: str, model: str, timeout: int, schema: dict | None = None
) -> dict:
    if _which("claude") is None:
        raise BridgeError("claude CLI not found on host")

    cmd = [
        _which("claude"),
        "-p",
        "--output-format",
        "json",
        # An empty --tools disables every built-in tool: this is a valuation,
        # not a session that should be reading or running anything.
        "--tools",
        "",
        # Customizations off: CLAUDE.md, skills, plugins, hooks, MCP servers,
        # custom agents. Auth and model selection deliberately survive it, so
        # the owner's subscription still signs the request. Measured: 542
        # context tokens instead of 17368, the rest being the app repository's
        # engineering rules. (--bare would go further but never reads OAuth or
        # the keychain, so it would demand an API key — the one thing this
        # bridge exists to avoid.)
        "--safe-mode",
        "--effort",
        CLAUDE_EFFORT,
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    if system:
        cmd += ["--system-prompt", system]
    if schema:
        # claude takes the schema itself, not a file (issue #218). A request
        # without one omits the flag entirely, which is exactly today's
        # invocation -- see complete_codex for the file-based codex side.
        cmd += ["--json-schema", json.dumps(schema)]

    payload = json.loads(_run(cmd, prompt, timeout) or "{}")
    if payload.get("is_error"):
        raise BridgeError(f"claude returned an error: {str(payload)[:500]}")

    usage = payload.get("usage") or {}
    _log_usage("claude", usage)
    # `result` is normally a string; under --json-schema it may already be
    # decoded to an object. str() on a dict would emit Python repr (single
    # quotes) instead of JSON, breaking the caller's json.loads, so a
    # structured result is re-serialised instead of stringified.
    raw_result = payload.get("result")
    if isinstance(raw_result, (dict, list)):
        text = json.dumps(raw_result)
    else:
        text = str(raw_result or "")
    return {
        "text": text,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        },
        "provider": "claude",
    }


def _strip_json_fence(text: str) -> str:
    """Drop a leading/trailing ``` fence, mirroring `_clean_json_text` in
    `services/property_ai_service.py` (also duplicated in
    `services/openai_service.py`).

    The bridge is a standalone host-side script (see the module docstring)
    that cannot import the Flask app's services, so this is a deliberate,
    minimal duplicate rather than a shared import -- kept tiny so the two
    stay easy to compare by eye. It exists only to decide whether a message
    *looks like* JSON; the app still does its own fence-stripping before the
    real `json.loads`.
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _parses_as_json(text: str) -> bool:
    try:
        json.loads(_strip_json_fence(text))
    except ValueError:
        return False
    return True


def _write_schema_file(schema: dict) -> str:
    """Write a JSON Schema to a private temp file for `codex exec --output-schema`.

    Unlike claude, codex takes a *path*, not the schema itself (issue #218).
    `mkstemp` gives an owner-only (0600), uniquely-named file with no
    collision risk between the up-to-`MAX_CONCURRENT_RUNS` runs that can be
    in flight together, and it lives outside `workdir()` on purpose: that
    directory is documented and tested (`test_workdir_is_empty_and_outside_
    any_repository`) to be empty, and codex's own read-only sandbox has no
    reason to see a stray schema file while it works.
    """
    fd, path = tempfile.mkstemp(prefix="ai-bridge-schema-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(schema, fh)
    except Exception as exc:
        os.unlink(path)
        raise BridgeError(f"failed to write output schema: {exc}") from exc
    return path


def complete_codex(
    prompt: str, system: str, model: str, timeout: int, schema: dict | None = None
) -> dict:
    if _which("codex") is None:
        raise BridgeError("codex CLI not found on host")

    cmd = [
        _which("codex"),
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        # Do not inherit the owner's interactive setup. A profile would not do:
        # profiles layer *on top of* the base user config, so anything added to
        # it later silently reaches this service. Auth still comes from
        # CODEX_HOME, which is what keeps this on the subscription.
        "--ignore-user-config",
        "--ignore-rules",
        # No session rollout on disk; each run used to leave 300-500 KB behind.
        "--ephemeral",
    ]
    for feature in CODEX_DISABLED_FEATURES:
        cmd += ["--disable", feature]
    cmd += ["-c", f"model_reasoning_effort={CODEX_EFFORT}"]
    # The CLI only knows its own model ids. An API-era name such as
    # "gpt-5-mini" makes it exit 1, so fall back to the CLI default instead of
    # failing the whole analysis.
    if model and (model.startswith("gpt-5.") or model.startswith("codex")):
        cmd += ["-m", model]
    elif model:
        LOG.warning("ignoring model %r: not a codex CLI model id", model)

    # Codex has no system-prompt flag; prepend it to the user turn instead.
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    schema_path: str | None = None
    if schema:
        schema_path = _write_schema_file(schema)
        cmd += ["--output-schema", schema_path]

    try:
        answer = ""
        last_message = ""
        usage: dict = {}
        completed = False
        for line in _run(cmd, full_prompt, timeout).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            if (
                event.get("type") == "item.completed"
                and item.get("type") == "agent_message"
            ):
                # The answer is the *last* agent message whose text parses as
                # JSON (#206 item 2), fenced or not. Concatenating every message,
                # as this did before, turned a preamble plus a JSON object into
                # something that parsed as neither. Keeping unconditionally the
                # last message fixed that but opened the mirror case: codex
                # emitting the JSON answer and then a closing remark ("Done.",
                # "Let me know if you need anything else.") made the remark win
                # and silently discarded the analysis. `last_message` is the
                # fallback for a stream where nothing parsed -- that failure
                # still needs to surface upstream (the app's own json.loads
                # fails on it) rather than being masked by refusing to answer.
                # This heuristic is deliberately unchanged by --output-schema
                # (issue #218): a CLI that ignores or rejects the schema must
                # still produce today's behaviour, not a hard failure.
                text = str(item.get("text") or "").strip()
                if text:
                    last_message = text
                    if _parses_as_json(text):
                        answer = text
            elif event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                completed = True
            elif event.get("type") == "turn.failed":
                raise BridgeError(f"codex turn failed: {str(event)[:500]}")

        if not completed:
            # A stream that stopped without turn.completed was cut off.
            # Returning whatever text arrived would report a partial answer
            # as a whole one.
            raise BridgeError("codex stream ended without turn.completed")

        if not answer:
            answer = last_message
    finally:
        # Must survive every exit from the run above: the happy path, a
        # BridgeTimeout raised out of `_run`, a turn.failed BridgeError, and
        # the truncated-stream BridgeError -- all of them unwind through this
        # `finally` before leaving complete_codex.
        if schema_path is not None:
            try:
                os.unlink(schema_path)
            except OSError:
                pass

    _log_usage("codex", usage)
    return {
        "text": answer,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
        },
        "provider": "codex",
    }


PROVIDERS = {
    "claude": complete_claude,
    "codex": complete_codex,
    "openai": complete_codex,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-bridge/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        # Constant-time compare so the token cannot be probed byte by byte.
        import hmac

        return bool(TOKEN) and hmac.compare_digest(header, expected)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if self.path.rstrip("/") != "/health":
            self._reply(404, {"error": "not found"})
            return
        self._reply(
            200,
            {
                "ok": True,
                "claude": _which("claude") is not None,
                "codex": _which("codex") is not None,
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        if self.path.rstrip("/") != "/v1/complete":
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"error": "body missing or too large"})
            return

        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            self._reply(400, {"error": "invalid json"})
            return

        provider = str(request.get("provider") or "claude").lower()
        handler = PROVIDERS.get(provider)
        if handler is None:
            self._reply(400, {"error": f"unknown provider: {provider}"})
            return

        prompt = str(request.get("prompt") or "")
        if not prompt.strip():
            self._reply(400, {"error": "prompt is required"})
            return

        # Optional and additive (issue #218): absent or null behaves exactly
        # as before this field existed. A present-but-malformed value is
        # rejected rather than silently ignored -- this endpoint has exactly
        # one caller (services/subscription_transport.py), so a shape other
        # than "object" or "absent" means that caller has a bug worth seeing.
        schema = request.get("schema")
        if schema is not None and not isinstance(schema, dict):
            self._reply(400, {"error": "schema must be a JSON object or null"})
            return

        timeout = min(int(request.get("timeout") or DEFAULT_TIMEOUT), 900)
        started = time.monotonic()
        try:
            result = handler(
                prompt,
                str(request.get("system") or ""),
                str(request.get("model") or ""),
                timeout,
                schema,
            )
        except BridgeError as exc:
            # A timeout and a busy bridge are not the same failure as a CLI
            # that answered with an error, and the caller should be able to
            # tell them apart without reading the message.
            status = 502
            if isinstance(exc, BridgeTimeout):
                status = 504
            elif isinstance(exc, BridgeBusy):
                status = 503
            LOG.error(
                "%s call failed after %.1fs: %s",
                provider,
                time.monotonic() - started,
                exc,
            )
            self._reply(status, {"error": str(exc)})
            return

        LOG.info("%s call finished in %.1fs", provider, time.monotonic() - started)

        self._reply(200, result)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not TOKEN:
        LOG.error(
            "AI_BRIDGE_TOKEN is not set - refusing to start without authentication"
        )
        return 1

    LOG.info(
        "ai-bridge on %s:%s (claude=%s, codex=%s, slots=%s, workdir=%s, "
        "effort claude/codex=%s/%s)",
        HOST,
        PORT,
        _which("claude") is not None,
        _which("codex") is not None,
        MAX_CONCURRENT_RUNS,
        workdir(),
        CLAUDE_EFFORT,
        CODEX_EFFORT,
    )

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    shutting_down = threading.Event()

    def _handle_shutdown_signal(signum: int, _frame: object) -> None:
        # A second SIGTERM/SIGINT while the first is still being handled
        # (e.g. launchd escalating) must not re-run the kill sweep or spawn a
        # second shutdown thread.
        if shutting_down.is_set():
            return
        shutting_down.set()
        LOG.info("received signal %s, shutting down", signum)
        _kill_inflight_runs()
        # server.shutdown() blocks until serve_forever()'s loop notices the
        # request and returns. This handler runs on the main thread, which is
        # the same thread blocked inside serve_forever() below — calling
        # shutdown() here would wait on a flag only that same loop iteration
        # can set, which is a deadlock. A separate thread lets serve_forever()
        # actually observe the request and return.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    server.serve_forever()
    LOG.info("ai-bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
