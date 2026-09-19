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
official SDK: `typesafe-sdk` 0.7 would add httpx2 and tenacity to the
production image for one POST. No retries here on purpose -- the caller's
fallback to the bridge is the retry. Three things the bridge transport does
not need and this one does, because the peer is a third party reached with a
bearer key: the URL must be https, a redirect is refused rather than followed
(urllib's default re-sends the request headers, key included, to wherever a
3xx points), and the model is pinned so a threshold tuned against one release
is not silently applied to the next.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from config import Config

logger = logging.getLogger(__name__)

SYSTEM_ONE_PATH = "/v1/systemone"
# The API answers with a small JSON document: one answer per question plus
# usage. Bounded so a misbehaving endpoint cannot hold memory hostage, and a
# body that reaches the bound is refused, not truncated into a parse error.
MAX_RESPONSE_BYTES = 1024 * 1024
_ERROR_DETAIL_BYTES = 300


class TypeSafeTransportError(RuntimeError):
    """TypeSafe could not serve the request.

    `status` is the HTTP status when there was a response (401 bad key, 422
    invalid body, 429 rate limit, 529 overloaded, 3xx refused redirect) and
    `None` when the request never got that far -- unreachable host, timeout,
    missing or invalid configuration.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class TypeSafeNotConfigured(TypeSafeTransportError):
    """No `TYPESAFE_API_KEY`: the route is absent, not broken."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure here, never followed.

    The API lives at one https origin; whatever answers with a 3xx is not it,
    and following it would hand the bearer key to the new location.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect to {newurl!r} refused", headers, fp
        )


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def is_configured() -> bool:
    return bool(Config.TYPESAFE_API_KEY)


def _base_url() -> str:
    base = (Config.TYPESAFE_API_URL or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc:
        raise TypeSafeTransportError("TYPESAFE_API_URL must be an https:// origin")
    return base


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The first bytes of an error body, or a marker when even that fails.

    Reading the body is a second network operation: it can time out or be cut
    off exactly like the first. Inside an `except` clause that failure would
    escape past every sibling handler, so it is contained here.
    """
    try:
        return exc.read(8192).decode("utf-8", "replace")[:_ERROR_DETAIL_BYTES]
    except Exception:  # noqa: BLE001 - the outer error is what is reported
        return "<error body unreadable>"
    finally:
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - nothing left to release
            pass


def system_one(
    state: Any,
    questions: Dict[str, Dict[str, Any]],
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """POST one evaluation; return the decoded body (`model`, `answers`, `usage`).

    Every failure is a `TypeSafeTransportError`, so a caller has one thing to
    catch and never sees `urllib` internals. `timeout` bounds each blocking
    socket operation, as `urlopen` defines it -- an allowance, not a deadline
    on the whole exchange.
    """
    if not Config.TYPESAFE_API_KEY:
        raise TypeSafeNotConfigured("TYPESAFE_API_KEY is not configured")
    base = _base_url()
    if not questions:
        raise TypeSafeTransportError("system_one needs at least one question")

    payload = {
        "state": state,
        "model": model or Config.TYPESAFE_MODEL,
        "questions": questions,
    }
    request = urllib.request.Request(
        f"{base}{SYSTEM_ONE_PATH}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.TYPESAFE_API_KEY}",
        },
        method="POST",
    )
    seconds = Config.TYPESAFE_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        with _OPENER.open(request, timeout=seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise TypeSafeTransportError(
            f"typesafe returned {exc.code}: {_error_detail(exc)}", status=exc.code
        ) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise TypeSafeTransportError(
            f"typesafe unreachable at {base}: {reason}"
        ) from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise TypeSafeTransportError(
            f"typesafe returned a body over {MAX_RESPONSE_BYTES} bytes"
        )
    try:
        decoded = json.loads(body)
    except ValueError as exc:
        raise TypeSafeTransportError("typesafe returned a non-JSON body") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("answers"), dict):
        raise TypeSafeTransportError("typesafe returned a body without answers")
    return decoded
