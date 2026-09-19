"""One typed decision from TypeSafe's Jev, the System One model.

Not a completion: the caller sends `state` (the text or JSON to judge) and a
map of typed questions, and gets typed answers with probabilities back --
nothing to parse out of prose. Today's one caller is the sea-view text claim
(`services/sea_view_service.classify_text_with_jev`).

This is the one per-token-billed AI route in the app -- the exception that
config.py's AI_BRIDGE comment forbids in general. The owner approved it on
2026-09-19 for this signal alone: the decision is tiny (~500 input tokens at
$0.042 per million, output free, ~0.1 s) where the bridge spends a cold CLI
run and up to 300 s of an Enrich press on the same three-way call. Without
`TYPESAFE_API_KEY` the route is simply absent: the caller falls back to the
bridge, so a missing key degrades to yesterday's behaviour and never fails a
request.

Raw `urllib`, mirroring `subscription_transport._post`, rather than the
official SDK: `typesafe-sdk` 0.7 would pull httpx2, pydantic, pydantic-core
and tenacity into the production image for one POST. No retries here on
purpose -- the caller's fallback to the bridge is the retry.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from config import Config

logger = logging.getLogger(__name__)

SYSTEM_ONE_PATH = "/v1/systemone"
DEFAULT_MODEL = "jev-latest"
# The API answers with a small JSON document: one answer per question plus
# usage. Bounded so a misbehaving endpoint cannot hold memory hostage.
MAX_RESPONSE_BYTES = 1024 * 1024


class TypeSafeTransportError(RuntimeError):
    """TypeSafe could not serve the request.

    `status` is the HTTP status when there was a response (401 bad key, 422
    invalid body, 429 rate limit, 529 overloaded) and `None` when the request
    never got that far -- unreachable host, timeout, missing configuration.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class TypeSafeNotConfigured(TypeSafeTransportError):
    """No `TYPESAFE_API_KEY`: the route is absent, not broken."""


def is_configured() -> bool:
    return bool(Config.TYPESAFE_API_KEY)


def system_one(
    state: Any,
    questions: Dict[str, Dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """POST one evaluation; return the decoded body (`model`, `answers`, `usage`).

    Every failure is a `TypeSafeTransportError`, so a caller has one thing to
    catch and never sees `urllib` internals.
    """
    if not Config.TYPESAFE_API_KEY:
        raise TypeSafeNotConfigured("TYPESAFE_API_KEY is not configured")
    base = (Config.TYPESAFE_API_URL or "").rstrip("/")
    if not base:
        raise TypeSafeTransportError("TYPESAFE_API_URL is not configured")
    if not questions:
        raise TypeSafeTransportError("system_one needs at least one question")

    request = urllib.request.Request(
        f"{base}{SYSTEM_ONE_PATH}",
        data=json.dumps(
            {"state": state, "model": model, "questions": questions}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.TYPESAFE_API_KEY}",
        },
        method="POST",
    )
    seconds = Config.TYPESAFE_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        detail = exc.read(8192).decode("utf-8", "replace")
        raise TypeSafeTransportError(
            f"typesafe returned {exc.code}: {detail[:300]}", status=exc.code
        ) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise TypeSafeTransportError(
            f"typesafe unreachable at {base}: {reason}"
        ) from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise TypeSafeTransportError("typesafe returned a non-JSON body") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise TypeSafeTransportError("typesafe returned a body without answers")
    return payload
