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
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("ai_bridge")

HOST = os.environ.get("AI_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_BRIDGE_PORT", "5061"))
TOKEN = os.environ.get("AI_BRIDGE_TOKEN", "")
DEFAULT_TIMEOUT = int(os.environ.get("AI_BRIDGE_TIMEOUT", "300"))
MAX_BODY_BYTES = 512 * 1024

# The CLIs live outside the login shell's PATH when launched from a LaunchAgent.
EXTRA_PATH = os.pathsep.join(
    [
        os.path.expanduser("~/.local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
)


def _cli_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([EXTRA_PATH, env.get("PATH", "")])
    # A key in the environment would silently take precedence over the
    # subscription session inside the CLIs; this bridge exists to avoid that.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    return env


def _which(name: str) -> str | None:
    return shutil.which(name, path=_cli_env()["PATH"])


class BridgeError(RuntimeError):
    """A CLI call could not be completed."""


def _run(cmd: list[str], stdin_text: str, timeout: int) -> str:
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_cli_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"{cmd[0]} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:2000]
        raise BridgeError(f"{cmd[0]} exited {proc.returncode}: {detail}")
    return proc.stdout


def complete_claude(prompt: str, system: str, model: str, timeout: int) -> dict:
    if _which("claude") is None:
        raise BridgeError("claude CLI not found on host")

    cmd = [_which("claude"), "-p", "--output-format", "json", "--tools", ""]
    if model:
        cmd += ["--model", model]
    if system:
        cmd += ["--system-prompt", system]

    payload = json.loads(_run(cmd, prompt, timeout) or "{}")
    if payload.get("is_error"):
        raise BridgeError(f"claude returned an error: {str(payload)[:500]}")

    usage = payload.get("usage") or {}
    return {
        "text": str(payload.get("result") or ""),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
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
    ]
    # The CLI only knows its own model ids. An API-era name such as
    # "gpt-5-mini" makes it exit 1, so fall back to the CLI default instead of
    # failing the whole analysis.
    if model and (model.startswith("gpt-5.") or model.startswith("codex")):
        cmd += ["-m", model]
    elif model:
        LOG.warning("ignoring model %r: not a codex CLI model id", model)

    # Codex has no system-prompt flag; prepend it to the user turn instead.
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    text_parts: list[str] = []
    usage: dict = {}
    for line in _run(cmd, full_prompt, timeout).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text_parts.append(str(item.get("text") or ""))
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
        elif event.get("type") == "turn.failed":
            raise BridgeError(f"codex turn failed: {str(event)[:500]}")

    return {
        "text": "\n".join(p for p in text_parts if p).strip(),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
        "provider": "codex",
    }


PROVIDERS = {"claude": complete_claude, "codex": complete_codex, "openai": complete_codex}


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
        try:
            result = handler(prompt, str(request.get("system") or ""), str(request.get("model") or ""), timeout)
        except BridgeError as exc:
            LOG.error("%s call failed: %s", provider, exc)
            self._reply(502, {"error": str(exc)})
            return

        self._reply(200, result)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        LOG.error("AI_BRIDGE_TOKEN is not set - refusing to start without authentication")
        return 1

    LOG.info(
        "ai-bridge on %s:%s (claude=%s, codex=%s)",
        HOST,
        PORT,
        _which("claude") is not None,
        _which("codex") is not None,
    )
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
