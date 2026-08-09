"""Redact credentials out of log records before a handler writes them.

The Google clients take their API key as a *query parameter*, so the key is
part of every request URL. Any HTTP client that logs the URL therefore logs the
key: `urllib3` does exactly that at DEBUG level, printing the full target of
each request. Turning DEBUG on anywhere — `DEV_MODE=true`, a library that
configures logging itself, a one-off `python -c` that never calls
`logging.basicConfig` — is enough to write the key into a log file in plain
text. Observed in practice on 2026-08-09 while running the ingest by hand.

Not fixed by lowering a log level, because that only holds until the next time
someone raises it. The filter strips the value wherever it appears, so a leak
needs a deliberate bypass rather than an accident.

Install with :func:`install_log_redaction`. The filter is attached to the root
logger's *handlers*, not to the root logger itself: a filter on a logger only
sees records logged directly through it, while records propagating up from
`urllib3` and friends are filtered by the handler that finally emits them.
"""

from __future__ import annotations

import logging
import re
from typing import Final

REDACTED: Final = "REDACTED"

#: Query parameters whose value is a credential. Matched case-insensitively and
#: anchored to `?` or `&` so an ordinary word ending in "key" is left alone.
_SENSITIVE_QUERY_PARAMS: Final = (
    "key",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "token",
    "password",
    "secret",
    "signature",
)

_QUERY_PARAM_RE: Final = re.compile(
    r"(?i)([?&](?:" + "|".join(_SENSITIVE_QUERY_PARAMS) + r")=)([^&\s\"'<>]+)"
)

#: Google API keys are recognisable on their own, so a key that reaches a log
#: outside a query string — a stray print, an error message quoting config —
#: is caught too. The length is fixed by Google: "AIza" plus 35 characters.
_GOOGLE_KEY_RE: Final = re.compile(r"AIza[0-9A-Za-z_\-]{35}")

#: Bearer tokens in an Authorization header that got logged.
_BEARER_RE: Final = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})")


def redact(text: str) -> str:
    """Return *text* with any credential it carries replaced by ``REDACTED``."""
    text = _QUERY_PARAM_RE.sub(r"\1" + REDACTED, text)
    text = _GOOGLE_KEY_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(r"\1" + REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Rewrite log records that carry a credential.

    The record is rendered first and the rendered text is redacted, because the
    secret usually arrives as a `%s` argument rather than as part of the format
    string — `urllib3` logs ``'%s://%s:%s "%s %s %s" %s %s'`` with the URL in
    the arguments. Rendering here means the handler's own formatting has
    nothing left to interpolate, so ``args`` is cleared along with it.

    A record whose rendering raises is passed through untouched: swallowing it
    would hide a logging bug, and a record that cannot be rendered cannot leak
    a rendered secret either. That is deliberate, not an oversight.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, keep as-is
            return True

        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        # `exc_text` is the cached rendering of a traceback, which can quote a
        # request URL from the frame that raised.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)

        return True


def install_log_redaction(logger: logging.Logger | None = None) -> int:
    """Attach the filter to every handler of *logger* (root by default).

    Idempotent: calling it twice does not stack two filters on one handler.
    Returns the number of handlers newly covered, so a caller can tell whether
    logging was configured at all — zero means there are no handlers yet and
    the call achieved nothing.
    """
    target = logger if logger is not None else logging.getLogger()

    covered = 0
    for handler in target.handlers:
        if any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            continue
        handler.addFilter(SecretRedactingFilter())
        covered += 1
    return covered
