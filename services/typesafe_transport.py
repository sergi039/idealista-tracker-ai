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
import time
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
_CHUNK_BYTES = 64 * 1024


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
    try:
        parts = urlsplit(base)
    except ValueError as exc:  # an unbalanced IPv6 bracket, for one
        raise TypeSafeTransportError("TYPESAFE_API_URL is not a valid URL") from exc
    if parts.scheme != "https" or not parts.netloc:
        raise TypeSafeTransportError("TYPESAFE_API_URL must be an https:// origin")
    return base


def _failure_name(exc: BaseException) -> str:
    """`ClassName` or `ClassName(errno)`: what failed, never what the peer said."""
    reason = getattr(exc, "reason", None)
    inner = reason if isinstance(reason, BaseException) else exc
    errno = getattr(inner, "errno", None)
    name = type(inner).__name__
    return f"{name}({errno})" if isinstance(errno, int) else name


def _discard(response: Any) -> None:
    """Close a response whose body is deliberately not read."""
    try:
        response.close()
    except Exception:  # noqa: BLE001 - nothing left to release
        pass


def _read_within(response: Any, deadline: float) -> bytes:
    """The body, as long as it arrives within the deadline and the size bound.

    The opener's `timeout` bounds each blocking socket operation, so a peer
    sending one byte every few seconds could keep a single read alive for as
    long as it liked. Reading in chunks against a wall-clock deadline turns
    the allowance into a bound: at most one chunk's blocking time past it.
    """
    chunks: list = []
    size = 0
    while True:
        chunk = response.read(_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise TypeSafeTransportError(
                f"typesafe returned a body over {MAX_RESPONSE_BYTES} bytes"
            )
        if time.monotonic() > deadline:
            raise TypeSafeTransportError(
                "typesafe response exceeded the time allowance"
            )
        chunks.append(chunk)


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
    socket operation, as `urlopen` defines it, and the body read runs against
    a wall-clock deadline of the same length, so one exchange takes at most
    about twice `timeout`. No response body is ever copied into a message: a
    vendor's error text may echo the request, bearer key included, and the
    message reaches the log and the stored detail.
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
    deadline = time.monotonic() + seconds
    try:
        with _OPENER.open(request, timeout=seconds) as response:
            body = _read_within(response, deadline)
    except urllib.error.HTTPError as exc:
        # The status is the whole diagnosis; the body stays unread.
        _discard(exc)
        raise TypeSafeTransportError(
            f"typesafe returned {exc.code}", status=exc.code
        ) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # The class and errno are the whole diagnosis. The exception's text
        # can carry what the peer sent (a malformed status line, say), and
        # this message reaches the log and the stored detail.
        raise TypeSafeTransportError(
            f"typesafe unreachable at {base}: {_failure_name(exc)}"
        ) from exc

    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError) as exc:  # nesting past the limit, too
        raise TypeSafeTransportError("typesafe returned a non-JSON body") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("answers"), dict):
        raise TypeSafeTransportError("typesafe returned a body without answers")
    return decoded
