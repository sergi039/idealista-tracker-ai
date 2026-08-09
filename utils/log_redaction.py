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
    """Rewrite the *message* of a record that carries a credential.

    The record is rendered first and the rendered text is redacted, because the
    secret usually arrives as a `%s` argument rather than as part of the format
    string — `urllib3` logs ``'%s://%s:%s "%s %s %s" %s %s'`` with the URL in
    the arguments. Rendering here means the handler's own formatting has
    nothing left to interpolate, so ``args`` is cleared along with it.

    This covers the message and nothing else. A traceback is **not** available
    at filter time: `Formatter.format` is what turns `exc_info` into text, and
    handlers filter before they format. Tracebacks are handled by
    :class:`RedactingFormatter`, which is the only place they exist as text.

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

        return True


class RedactingFormatter(logging.Formatter):
    """Wrap another formatter and redact whatever it produces.

    Necessary because a filter cannot see a traceback. `logging` renders
    `exc_info` inside `Formatter.format`, long after `Handler.filter` has run,
    so an exception carrying a credential in its message — say a request URL
    quoted by the library that raised — reaches the output untouched by any
    filter. Verified against a live logger before this class existed: the key
    was written out in full.

    Wrapping rather than replacing keeps whatever format string the handler was
    configured with. The cached `exc_text` is redacted in place too, so a second
    handler formatting the same record cannot re-emit the original traceback
    from the cache.
    """

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        formatted = self.inner.format(record)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return redact(formatted)


def install_log_redaction(logger: logging.Logger | None = None) -> int:
    """Cover every handler of *logger* (root by default) against leaks.

    Each handler gets both halves: the filter for the message, the formatter
    wrapper for the traceback. Neither alone is enough — see the class
    docstrings for which case each one catches.

    Idempotent: calling it twice stacks neither a second filter nor a second
    wrapper. Returns the number of handlers newly covered, so a caller can tell
    whether logging was configured at all — zero means there were no handlers
    and the call achieved nothing.
    """
    target = logger if logger is not None else logging.getLogger()

    covered = 0
    for handler in target.handlers:
        already_filtered = any(
            isinstance(f, SecretRedactingFilter) for f in handler.filters
        )
        already_formatted = isinstance(handler.formatter, RedactingFormatter)
        if already_filtered and already_formatted:
            continue

        if not already_filtered:
            handler.addFilter(SecretRedactingFilter())
        if not already_formatted:
            handler.setFormatter(
                RedactingFormatter(handler.formatter or logging.Formatter())
            )
        covered += 1
    return covered
