"""Client for the host-side subscription AI bridge.

The app calls Claude and OpenAI through the owner's *subscriptions* (Claude
Code and Codex CLIs running on the host), never through API keys. This module
talks to `tools/ai_bridge.py` over HTTP and exposes a thin shim shaped like the
slice of the Anthropic SDK the services already use, so call sites keep
reading `message.content[0].text`.

Fail closed: without AI_BRIDGE_TOKEN (or with the bridge down) an AI call
raises instead of silently falling back to a key-based path.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300


class SubscriptionTransportError(RuntimeError):
    """The bridge could not serve the request."""


def _post(path: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    base = (Config.AI_BRIDGE_URL or "").rstrip("/")
    if not base:
        raise SubscriptionTransportError("AI_BRIDGE_URL is not configured")
    if not Config.AI_BRIDGE_TOKEN:
        raise SubscriptionTransportError("AI_BRIDGE_TOKEN is not configured")

    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.AI_BRIDGE_TOKEN}",
        },
        method="POST",
    )
    try:
        # Bounded read: the bridge answers with a small JSON document. The
        # margin added to `timeout` is config.py's AI_BRIDGE_SOCKET_MARGIN_
        # SECONDS -- the single place that number is derived (#206 item 3),
        # so it tracks the bridge's own AI_BRIDGE_KILL_GRACE instead of the
        # old hardcoded `+ 15`, which was exactly `3 * KILL_GRACE_SECONDS` at
        # the default grace and left no slack for anything else.
        socket_timeout = timeout + Config.AI_BRIDGE_SOCKET_MARGIN_SECONDS
        with urllib.request.urlopen(request, timeout=socket_timeout) as response:
            body = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = exc.read(8192).decode("utf-8", "replace")
        raise SubscriptionTransportError(
            f"bridge returned {exc.code}: {detail[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SubscriptionTransportError(
            f"bridge unreachable at {base}: {exc.reason}"
        ) from exc

    try:
        return json.loads(body)
    except ValueError as exc:
        raise SubscriptionTransportError("bridge returned a non-JSON body") from exc


def complete(
    prompt: str,
    *,
    provider: str = "claude",
    system: str = "",
    model: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Run one completion through the subscription CLI behind the bridge."""
    result = _post(
        "/v1/complete",
        {
            "provider": provider,
            "prompt": prompt,
            "system": system,
            "model": model,
            "timeout": timeout,
        },
        timeout,
    )
    text = str(result.get("text") or "")
    if not text.strip():
        raise SubscriptionTransportError(f"{provider} returned an empty completion")
    return result


def health() -> Dict[str, Any]:
    """Report whether the bridge is up and which CLIs it can see."""
    base = (Config.AI_BRIDGE_URL or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "AI_BRIDGE_URL is not configured"}
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=10) as response:
            return json.loads(response.read(65536))
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"ok": False, "error": str(exc)}


# --- Anthropic-SDK-shaped shim -------------------------------------------
# The existing services read `message.content[0].text` and `message.usage`.
# Keeping that shape means the transport swap does not touch prompt building
# or response parsing.


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass
class _Message:
    content: List[_TextBlock] = field(default_factory=list)
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str = "end_turn"


class _Messages:
    def __init__(self, provider: str, default_model: str) -> None:
        self._provider = provider
        self._default_model = default_model

    def create(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: str = "",
        system: str = "",
        max_tokens: Optional[int] = None,  # noqa: ARG002 - CLI has no equivalent knob
        temperature: Optional[float] = None,  # noqa: ARG002 - same
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        **_ignored: Any,
    ) -> _Message:
        prompt_parts: List[str] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                prompt_parts.append(content)
            elif isinstance(content, list):
                prompt_parts.extend(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict)
                )
        prompt = "\n\n".join(part for part in prompt_parts if part)

        result = complete(
            prompt,
            provider=self._provider,
            system=system,
            model=model or self._default_model,
            timeout=timeout,
        )
        usage = result.get("usage") or {}
        return _Message(
            content=[_TextBlock(text=str(result.get("text") or ""))],
            usage=_Usage(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            ),
        )


class SubscriptionClient:
    """Drop-in stand-in for `anthropic.Anthropic` backed by the subscription CLI."""

    def __init__(self, provider: str = "claude", default_model: str = "") -> None:
        self.provider = provider
        self.messages = _Messages(provider, default_model)
