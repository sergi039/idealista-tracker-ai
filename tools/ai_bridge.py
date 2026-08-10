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
MIN_RUN_SECONDS = float(os.environ.get("AI_BRIDGE_MIN_RUN_SECONDS", "5"))

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
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
    }
)
AUTH_ENV_KEEP = frozenset({"CODEX_HOME", "CLAUDE_CONFIG_DIR"})

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
        # Only queueing can push the budget below the floor, so the floor is
        # capped at half the request's own budget: a caller who asked for a
        # short timeout and got a slot immediately still gets its run.
        floor = min(MIN_RUN_SECONDS, timeout / 2)
        if budget < floor:
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
        try:
            stdout, stderr = proc.communicate(stdin_text, timeout=budget)
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
        _RUN_SLOTS.release()

    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()[:2000]
        raise BridgeError(f"{cmd[0]} exited {proc.returncode}: {detail}")
    return stdout


def complete_claude(prompt: str, system: str, model: str, timeout: int) -> dict:
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

    payload = json.loads(_run(cmd, prompt, timeout) or "{}")
    if payload.get("is_error"):
        raise BridgeError(f"claude returned an error: {str(payload)[:500]}")

    usage = payload.get("usage") or {}
    _log_usage("claude", usage)
    return {
        "text": str(payload.get("result") or ""),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        },
        "provider": "claude",
    }


def complete_codex(prompt: str, system: str, model: str, timeout: int) -> dict:
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

    answer = ""
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
            # The *last* agent message is the answer. Concatenating every one
            # of them, as this did before, turns a preamble plus a JSON object
            # into something that parses as neither.
            text = str(item.get("text") or "").strip()
            if text:
                answer = text
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            completed = True
        elif event.get("type") == "turn.failed":
            raise BridgeError(f"codex turn failed: {str(event)[:500]}")

    if not completed:
        # A stream that stopped without turn.completed was cut off. Returning
        # whatever text arrived would report a partial answer as a whole one.
        raise BridgeError("codex stream ended without turn.completed")

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

        timeout = min(int(request.get("timeout") or DEFAULT_TIMEOUT), 900)
        started = time.monotonic()
        try:
            result = handler(
                prompt,
                str(request.get("system") or ""),
                str(request.get("model") or ""),
                timeout,
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
