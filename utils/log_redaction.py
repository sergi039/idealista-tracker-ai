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
#: The closing quote is a backreference to the opening one, and the value is
#: "anything that is not that quote". Accepting either quote at both ends is
#: the allow-list mistake in another costume: `password="abc'TOPSECRET"` ends
#: the value at the apostrophe and yields `password="REDACTED'TOPSECRET"` —
#: a leak that looks handled, which is the whole reason the Bearer pattern
#: stopped matching by character class.
_STRUCTURED_SECRET_RE: Final = re.compile(
    r"(?i)([\"']?(?:api_?key|access_token|auth_token|token|password|secret)[\"']?"
    r"\s*[:=]\s*([\"']))((?:(?!\2).)*)(\2)"
)

#: The same names with an *unquoted* value: `token=abc`, `api_key: abc`. A log
#: line is not always JSON — `logger.info("api_key=%s", key)` renders one of
#: these, and the query-parameter pattern above cannot help because there is no
#: `?` or `&` to anchor to. Reported in review as the remaining hole.
#:
#: Bare `key` is excluded here as well, for the reason given above: `sort_key=`
#: and the codebase's own preset names would otherwise be redacted.
_UNQUOTED_SECRET_RE: Final = re.compile(
    r"(?i)\b((?:api_?key|access_token|auth_token|token|password|secret)\s*[:=]\s*)"
    r"([^\s\"',;)\]}&]+)"
)

#: A printf conversion specifier, in the shapes `logging` accepts:
#: `%s`, `%(name)s`, `%-5.2f`, `%%`.
_PRINTF_SPEC_RE: Final = re.compile(
    r"%(?:\([^)]*\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?[diouxXeEfFgGcrsa%]"
)


def redact(text: str) -> str:
    """Return *text* with any credential it carries replaced by ``REDACTED``."""
    text = _QUERY_PARAM_RE.sub(r"\1" + REDACTED, text)
    text = _GOOGLE_KEY_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(r"\1" + REDACTED, text)
    text = _STRUCTURED_SECRET_RE.sub(r"\1" + REDACTED + r"\4", text)
    text = _UNQUOTED_SECRET_RE.sub(r"\1" + REDACTED, text)
    return text


def redact_format_string(text: str) -> str:
    """Redact a format string without consuming its placeholders.

    Only the failure paths need this. Everywhere else the record is rendered
    first, so a `%s` has already become whatever it stood for and the patterns
    see ordinary text. Here the text is still the template, and a pattern that
    swallowed the placeholder out of ``"GET /x?key=%s %s"`` would leave
    ``"GET /x?key=REDACTED %s"`` — one placeholder for one argument, which
    formats cleanly. The record would then be emitted instead of reaching
    `Handler.handleError`, and the diagnostic that says formatting is broken
    would disappear silently. Reported in review, and the placeholder is not a
    credential in any case: it is the hole a credential gets substituted into.
    """
    specifiers = _PRINTF_SPEC_RE.findall(text)
    literals = _PRINTF_SPEC_RE.split(text)

    out = [redact(literals[0])]
    for specifier, literal in zip(specifiers, literals[1:]):
        out.append(specifier)
        out.append(redact(literal))
    return "".join(out)


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

    One consequence is deliberate and worth stating. Substituting a type name
    can make a record that failed to render succeed on the retry: an argument
    whose ``__str__`` raises becomes ``'<Thing>'``, which formats fine, so the
    handler emits ``<Thing>`` instead of `handleError` reporting the broken
    argument. The failure is reported less loudly than it would have been, and
    that is the trade taken knowingly — the alternative is a diagnostic path
    that prints values nothing has been able to inspect.
    """
    if not args:
        return args
    if isinstance(args, dict):
        # The *keys* are text the caller chose and `handleError` prints them
        # with the rest — `logger.error("%(a)s", {"api_key=…": 1})` put a
        # credential in the key. Values become type names; keys get the same
        # pattern pass as any other text.
        return {redact(str(k)): f"<{type(v).__name__}>" for k, v in args.items()}
    return tuple(f"<{type(a).__name__}>" for a in args)


#: Stand-in for a message that cannot be turned into text at all.
UNPRINTABLE: Final = "<unprintable log message withheld>"

#: Marks a record whose arguments have already been replaced by type names.
_WITHHELD_FLAG: Final = "_redaction_args_withheld"


def _withhold_record_args(record: logging.LogRecord) -> None:
    """Withhold a record's arguments, at most once.

    One record can reach this twice — the filter withholds when rendering
    fails, and the formatter withholds again when the *handler's* format string
    is what failed. Applied twice, the type names describe each other:
    ``42`` becomes ``'<int>'`` becomes ``'<str>'``, and the diagnostic starts
    lying about what arrived.
    """
    if getattr(record, _WITHHELD_FLAG, False):
        return
    try:
        record.args = _withhold_args(record.args)
    except Exception:
        # Even naming the types can fail: a mapping argument whose `items()`
        # raises reaches here, and an exception escaping a filter leaves
        # `logging` entirely and lands in the caller. Withhold everything
        # instead — an empty tuple is always safe, and `getMessage()` then
        # returns the format string untouched.
        record.args = ()
    setattr(record, _WITHHELD_FLAG, True)


def _withhold(record: logging.LogRecord) -> None:
    """Make a record that failed to render safe to print as a diagnostic.

    `logging` never drops such a record: `Handler.handleError` writes
    ``record.msg`` and ``record.args`` to stderr. The message is still worth
    pattern-matching; the arguments are not judgeable and are withheld.

    Rendering the message can fail a second time — ``str(record.msg)`` runs the
    object's own ``__str__``, and that is what raised in the first place. A
    filter that let it propagate would be worse than the leak it guards: an
    exception raised in `Filter.filter` travels out of ``logger.error(...)``
    into the caller, turning a log line into an application failure. `logging`
    itself never does that, so neither may this.
    """
    try:
        record.msg = redact_format_string(str(record.msg))
    except Exception:
        record.msg = UNPRINTABLE
    _withhold_record_args(record)


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
            _withhold(record)
            return True

        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        return True


class FormattingFailed(Exception):
    """Stands in for a formatter's own exception, with the text redacted.

    `Handler.handleError` prints the exception and its traceback to stderr, so
    a formatter that raised ``ValueError("api_key=…")`` would report the
    credential itself. The failure still has to be reported — the handler is
    broken and someone has to know — so the report is kept and the payload is
    dropped.
    """


def _redact_cached(record: logging.LogRecord) -> None:
    """Redact every rendered field `Formatter.format` leaves on the record.

    Returning redacted text is not enough: these are a cache, and a handler
    formatting the same record afterwards reads them instead of rendering
    again. `exc_text` and `stack_info` carry real text; `asctime` only ever
    carries a credential if a `datefmt` does, and is covered because "redact
    what is returned, leave the cache" is a mistake this module already made
    once.
    """
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    if getattr(record, "asctime", None):
        record.asctime = redact(record.asctime)


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
        try:
            formatted = self.inner.format(record)
        except Exception as exc:
            # `handleError` is reached from here too, by a route the filter
            # cannot cover: the record rendered fine, and the *handler's* format
            # string is what failed - a `%(field)s` the record does not carry.
            # It prints `record.args` just the same, and those arguments have
            # never been through a pattern (an unrendered argument carries no
            # marker to match). Withhold them, then let the failure continue so
            # logging still reports the broken formatter.
            _withhold_record_args(record)
            # `Formatter.format` sets `record.message` from the arguments before
            # it applies the format string, so the rendered value is already
            # cached on the record even though the failure meant nothing was
            # written. Drop it, or the next thing to read the record gets the
            # value the withholding above just refused to print. Any later
            # formatter recreates it from the withheld arguments.
            record.__dict__.pop("message", None)
            # The half-built cache is left behind too - `asctime` is assigned
            # before the format string is applied - so this path needs the same
            # sweep the successful one gets.
            _redact_cached(record)
            # And the failure itself is printed by `handleError`, message and
            # traceback: an inner formatter that raised
            # `ValueError("api_key=...")` would put it on stderr. Re-raising a
            # redacted stand-in keeps the report and drops the payload;
            # `from None` is deliberate, or the original arrives chained to it.
            raise FormattingFailed(redact(f"{type(exc).__name__}: {exc}")) from None

        _redact_cached(record)
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
