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
#:
#: The value is matched as "everything up to whitespace or a quote" rather than
#: as an allow-list of characters. An allow-list is the wrong shape here: base64
#: carries `+`, `/` and `=`, and a token stops at the first character the list
#: forgot, leaving the rest in the log. `Bearer abcdefgh+TOPSECRET` redacted
#: with `[A-Za-z0-9._-]+` yields `Bearer REDACTED+TOPSECRET` — worse than not
#: matching at all, because it looks handled.
_BEARER_RE: Final = re.compile(r"(?i)(bearer\s+)([^\s\"'<>,;]+)")

#: Credentials in structured output — a config dict logged at startup, a JSON
#: body echoed on error: `"api_key": "…"`, `token='…'`.
#:
#: Deliberately excludes a bare `key`, unlike the query-parameter pattern. This
#: codebase stores `{"key": "airport"}` in its travel presets and `{"key":
#: "police"}` in enrichment data; redacting those would blind the diagnostics
#: this filter exists to keep readable. In a query string `key=` is Google's
#: credential parameter, so there the bare name stays.
_STRUCTURED_SECRET_RE: Final = re.compile(
    r"(?i)([\"']?(?:api_?key|access_token|auth_token|token|password|secret)[\"']?"
    r"\s*[:=]\s*[\"'])([^\"']+)([\"'])"
)


def redact(text: str) -> str:
    """Return *text* with any credential it carries replaced by ``REDACTED``."""
    text = _QUERY_PARAM_RE.sub(r"\1" + REDACTED, text)
    text = _GOOGLE_KEY_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(r"\1" + REDACTED, text)
    text = _STRUCTURED_SECRET_RE.sub(r"\1" + REDACTED + r"\3", text)
    return text


def _withhold_args(args):
    """Replace a record's arguments with their type names.

    Only reached when rendering already failed, and then the values cannot be
    judged. Pattern matching works on a *marked* secret — `?key=…`,
    `Bearer …`, an `AIza…` key — but a bare argument carries no marker: in
    `logger.error("token=%s %s", "TOPSECRET")` the only thing identifying
    `TOPSECRET` as a credential is the format string it never got substituted
    into. Redacting by pattern therefore cannot work here, and guessing wrong
    means printing the secret.

    So the values are withheld rather than inspected. The type names are kept
    because they are what a broken-formatting diagnostic is actually for —
    seeing that three arguments arrived for two placeholders — and a type name
    is not a credential.
    """
    if not args:
        return args
    if isinstance(args, dict):
        return {k: f"<{type(v).__name__}>" for k, v in args.items()}
    return tuple(f"<{type(a).__name__}>" for a in args)


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

    A record whose rendering *raises* still has to be redacted, and this was
    the third thing review caught. `logging` does not drop such a record: it
    calls `Handler.handleError`, which prints the format string and the raw
    arguments to stderr as a diagnostic. So `logger.error("token=%s %s",
    "TOPSECRET")` — one placeholder too many — wrote `Arguments:
    ('TOPSECRET',)` in the clear. Passing the record through untouched was
    exactly wrong: a record that cannot be rendered leaks *more*, not less.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Rendering failed, so `handleError` will print `record.msg` and
            # `record.args` verbatim. The message can still be pattern-matched;
            # the arguments cannot be judged at all, so they are withheld.
            record.msg = redact(str(record.msg))
            record.args = _withhold_args(record.args)
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
