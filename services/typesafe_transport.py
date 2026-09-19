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

`http.client` directly, rather than `urllib` or the official SDK. The SDK
(`typesafe-sdk` 0.7) would add httpx2 and tenacity to the production image
for one POST. `urllib` was tried first and rejected in review: its timeout
bounds each socket operation, never the exchange, so a peer dripping one byte
per operation could hold the connect, the headers or the body open for as
long as it liked; and its redirect handler re-sends the request headers --
the bearer key among them -- to wherever a 3xx points. Here a watchdog closes
the connection when the allowance runs out, whatever phase the exchange is
in, and a 3xx is a status like any other, never followed. No retries on
purpose: the caller's fallback to the bridge is the retry. Nothing the peer
sends -- a body, a status line, a header -- is ever copied into a message,
because the message reaches the log and the stored detail.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from config import Config

logger = logging.getLogger(__name__)

SYSTEM_ONE_PATH = "/v1/systemone"
# The API answers with a small JSON document: one answer per question plus
# usage. Bounded so a misbehaving endpoint cannot hold memory hostage, and a
# body that reaches the bound is refused, not truncated into a parse error.
MAX_RESPONSE_BYTES = 1024 * 1024
_CHUNK_BYTES = 64 * 1024
# A bearer token is printable ASCII without whitespace. Anything else -- a
# trailing newline from a hand-edited .env, say -- would make http.client
# refuse the header with a message that quotes it, credential included.
_KEY_SHAPE = re.compile(r"[\x21-\x7e]+")


class TypeSafeTransportError(RuntimeError):
    """TypeSafe could not serve the request.

    `status` is the HTTP status when there was a response (401 bad key, 422
    invalid body, 429 rate limit, 529 overloaded, 3xx never followed) and
    `None` when the request never got that far -- unreachable host, the
    allowance running out, missing or invalid configuration.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class TypeSafeNotConfigured(TypeSafeTransportError):
    """No `TYPESAFE_API_KEY`: the route is absent, not broken."""


def is_configured() -> bool:
    return bool(Config.TYPESAFE_API_KEY)


def _origin() -> Tuple[str, int, str]:
    """(host, port, path prefix) of `TYPESAFE_API_URL`, an https origin."""
    base = (Config.TYPESAFE_API_URL or "").strip().rstrip("/")
    try:
        parts = urlsplit(base)
        host, port = parts.hostname, parts.port  # `.port` refuses a bad one
    except ValueError as exc:  # an unbalanced IPv6 bracket, a port past 65535
        raise TypeSafeTransportError("TYPESAFE_API_URL is not a valid URL") from exc
    if parts.scheme != "https" or not host:
        raise TypeSafeTransportError("TYPESAFE_API_URL must be an https:// origin")
    return host, port or 443, parts.path.rstrip("/")


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


def _read_bounded(response: Any) -> bytes:
    """The body through `read1`, refused past the size bound.

    `read1` returns whatever one socket read produced -- `read(n)` would
    block until `n` bytes had arrived -- so the size bound is checked after
    every piece the peer sends; the watchdog in `system_one` bounds the time.
    """
    read1 = getattr(response, "read1", None)
    if read1 is None:
        _discard(response)
        raise TypeSafeTransportError("typesafe response cannot be read incrementally")
    chunks: list = []
    size = 0
    while True:
        chunk = read1(_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise TypeSafeTransportError(
                f"typesafe returned a body over {MAX_RESPONSE_BYTES} bytes"
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
    catch and never sees `http.client` internals. `timeout` bounds each
    socket operation and, through a watchdog that closes the connection when
    it runs out, the whole exchange -- connect, headers and body alike -- so
    one call takes at most about `timeout` seconds.
    """
    key = Config.TYPESAFE_API_KEY
    if not key:
        raise TypeSafeNotConfigured("TYPESAFE_API_KEY is not configured")
    if not _KEY_SHAPE.fullmatch(key):
        raise TypeSafeTransportError(
            "TYPESAFE_API_KEY is malformed (whitespace or control characters)"
        )
    host, port, prefix = _origin()
    if not questions:
        raise TypeSafeTransportError("system_one needs at least one question")

    seconds = Config.TYPESAFE_TIMEOUT_SECONDS if timeout is None else timeout
    payload = json.dumps(
        {
            "state": state,
            "model": model or Config.TYPESAFE_MODEL,
            "questions": questions,
        }
    ).encode()

    connection = http.client.HTTPSConnection(host, port, timeout=seconds)
    expired = threading.Event()

    def _cut() -> None:
        expired.set()
        connection.close()  # whatever the exchange is blocked on now fails

    watchdog = threading.Timer(seconds, _cut)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request(
            "POST",
            f"{prefix}{SYSTEM_ONE_PATH}",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        response = connection.getresponse()
        status = response.status
        if status != 200:
            # The status is the whole diagnosis; the body stays unread, and
            # a 3xx is not followed: http.client never does.
            _discard(response)
            raise TypeSafeTransportError(f"typesafe returned {status}", status=status)
        body = _read_bounded(response)
    except TypeSafeTransportError:
        raise
    except (http.client.HTTPException, OSError) as exc:
        if expired.is_set():
            raise TypeSafeTransportError(
                "typesafe exchange exceeded the time allowance"
            ) from None
        # The class and errno are the whole diagnosis. The exception's text
        # can carry what the peer sent (a malformed status line, say).
        raise TypeSafeTransportError(
            f"typesafe unreachable at https://{host}: {_failure_name(exc)}"
        ) from exc
    except ValueError:
        # http.client refuses a malformed header with a message that quotes
        # it. The chain is dropped on purpose: a traceback would carry the
        # value. The key-shape check above makes this unreachable in practice.
        raise TypeSafeTransportError(
            "typesafe request was refused before it was sent"
        ) from None
    finally:
        watchdog.cancel()
        connection.close()

    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError) as exc:  # nesting past the limit, too
        raise TypeSafeTransportError("typesafe returned a non-JSON body") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("answers"), dict):
        raise TypeSafeTransportError("typesafe returned a body without answers")
    return decoded
