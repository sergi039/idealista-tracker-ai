"""A credential must never reach a log handler in plain text.

The failure this pins actually happened on 2026-08-09: the ingest was run by
hand, `urllib3` logged its requests at DEBUG, and because the Google clients
carry their API key in the query string, the owner's key was written out in
full. Nothing in the application printed it - the HTTP library printed the URL,
which is the same thing.

Lowering a log level does not fix that; it only postpones it until someone
raises the level again. These tests pin the filter, not the level.
"""

import io
import logging
import sys
from contextlib import redirect_stderr

import pytest

from utils.log_redaction import (
    REDACTED,
    RedactingFormatter,
    SecretRedactingFilter,
    install_log_redaction,
    redact,
)

# Shaped like a Google key (the literal prefix plus 35 characters) so the
# pattern under test is exercised for real. Assembled from fragments rather
# than written out: a whole-looking key in a source file trips secret scanners
# and teaches the wrong habit, and the repository's pre-push hook rejects it.
FAKE_GOOGLE_KEY = "AI" + "za" + "SyFAKE_TEST_KEY_" + ("0" * 19)

# A credential-shaped value with no marker of its own, for the paths that have
# to withhold rather than pattern-match.
SECRET_VALUE = "sk-live-" + "TOPSECRET"


@pytest.fixture
def captured():
    """A logger whose output we can read, with the filter installed on it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("test_log_redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    install_log_redaction(logger)
    yield logger, stream
    logger.handlers = []


class TestTheRealFailure:
    def test_without_the_filter_the_key_is_written_out(self):
        """Baseline: this is what happened, and what must stop happening.

        Same logger, same call, no filter installed. If this ever stops
        leaking, the tests below prove nothing and the reason has to be found
        before trusting them.
        """
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test_log_redaction_baseline")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        logger.debug(
            '%s://%s:%s "%s %s %s" %s %s',
            "https",
            "maps.googleapis.com",
            443,
            "GET",
            f"/maps/api/place/nearbysearch/json?location=43.5,-6.4&key={FAKE_GOOGLE_KEY}",
            "HTTP/1.1",
            200,
            42358,
        )
        logger.handlers = []

        assert FAKE_GOOGLE_KEY in stream.getvalue()

    def test_a_urllib3_style_request_line_loses_its_key(self, captured):
        """The exact shape urllib3 emits: URL arrives as a `%s` argument.

        A filter that only looked at `record.msg` would pass this straight
        through, because the format string alone holds no secret.
        """
        logger, stream = captured

        logger.debug(
            '%s://%s:%s "%s %s %s" %s %s',
            "https",
            "maps.googleapis.com",
            443,
            "GET",
            f"/maps/api/place/nearbysearch/json?location=43.5,-6.4&key={FAKE_GOOGLE_KEY}",
            "HTTP/1.1",
            200,
            42358,
        )

        output = stream.getvalue()
        assert FAKE_GOOGLE_KEY not in output, "the API key reached the handler"
        assert "AIza" not in output
        assert REDACTED in output
        # The rest of the line must survive: a redacted log is still a log.
        assert "maps.googleapis.com" in output
        assert "nearbysearch" in output
        assert "location=43.5,-6.4" in output

    def test_the_distance_matrix_url_loses_its_key(self, captured):
        logger, stream = captured

        logger.debug(
            "https://maps.googleapis.com:443 %s",
            f"GET /maps/api/distancematrix/json?origins=43.5,-6.4&mode=driving&key={FAKE_GOOGLE_KEY} HTTP/1.1",
        )

        output = stream.getvalue()
        assert FAKE_GOOGLE_KEY not in output
        assert "mode=driving" in output


class TestWhatCountsAsASecret:
    @pytest.mark.parametrize(
        "param",
        ["key", "api_key", "apikey", "token", "access_token", "password", "secret"],
    )
    def test_sensitive_query_parameters_are_stripped(self, param):
        redacted = redact(f"https://example.com/x?a=1&{param}=s3cr3t-value&b=2")

        assert "s3cr3t-value" not in redacted
        assert REDACTED in redacted
        assert "a=1" in redacted
        assert "b=2" in redacted

    def test_a_bare_google_key_outside_a_query_string_is_caught(self):
        redacted = redact(f"Places lookup failed for key {FAKE_GOOGLE_KEY}")

        assert FAKE_GOOGLE_KEY not in redacted
        assert REDACTED in redacted

    def test_a_bearer_token_is_stripped(self):
        redacted = redact("Authorization: Bearer abcdef1234567890xyz")

        assert "abcdef1234567890xyz" not in redacted
        assert REDACTED in redacted

    @pytest.mark.parametrize(
        "token",
        [
            "abcdefgh+TOPSECRET",  # base64 '+'
            "AAAA/BBBB+CCCC==",  # base64 '/' and padding
            "eyJhbGci.eyJzdWIi.SflKxwRJSMeKKF2QT4f",  # JWT
            "sk-live-TOPSECRET",
        ],
    )
    def test_a_bearer_token_is_stripped_whole(self, token):
        """Reported as a BLOCKER on PR #148: an allow-list of characters stops
        at the first one it forgot and leaves the rest in the log.

        `Bearer abcdefgh+TOPSECRET` under `[A-Za-z0-9._-]+` became
        `Bearer REDACTED+TOPSECRET` — a leak that looks handled, which is worse
        than no match at all.
        """
        redacted = redact(f"Authorization: Bearer {token}")

        assert token not in redacted
        assert "TOPSECRET" not in redacted
        assert "SflKxwRJ" not in redacted
        assert redacted == f"Authorization: Bearer {REDACTED}"

    @pytest.mark.parametrize(
        "value", ["abc+def/ghi==", "a%20b", "x=y=z", "sk-live-TOPSECRET"]
    )
    def test_a_query_value_is_stripped_whole(self, value):
        redacted = redact(f"https://x/y?key={value}&z=1")

        assert value not in redacted
        assert redacted == f"https://x/y?key={REDACTED}&z=1"

    @pytest.mark.parametrize(
        "line",
        [
            'config: {"api_key": "sk-live-TOPSECRET"}',
            "token='sk-live-TOPSECRET'",
            '{"access_token": "sk-live-TOPSECRET"}',
            'password = "sk-live-TOPSECRET"',
        ],
    )
    def test_a_credential_in_structured_output_is_stripped(self, line):
        """A config dict logged at startup, or a JSON body echoed on error."""
        redacted = redact(line)

        assert "TOPSECRET" not in redacted
        assert REDACTED in redacted

    @pytest.mark.parametrize(
        "line",
        [
            '{"key": "airport", "mode": "driving"}',
            '{"key": "police", "status": "ok"}',
            '{"key": "supermarket"}',
        ],
    )
    def test_the_travel_preset_key_is_not_a_credential(self, line):
        """`key` means a preset name in this codebase's own data.

        `services/property_travel_service.py` stores `{"key": "airport"}` and
        enrichment stores `{"key": "police"}`. Redacting those would blind the
        diagnostics this filter exists to keep readable. In a *query string*
        `key=` is Google's credential parameter, so there the bare name is
        still treated as one — the distinction is deliberate.
        """
        assert redact(line) == line

    def test_an_ordinary_word_ending_in_key_is_left_alone(self):
        """`monkey=1` is not a credential, and neither is a `sort_key` column."""
        text = "sorting by sort_key=price, monkey=1, donkey=2"

        assert redact(text) == text

    def test_a_message_with_no_secret_is_untouched(self):
        text = "Scheduled ingestion completed. Processed 3 properties"

        assert redact(text) == text


class TestTracebacks:
    """A traceback is text only inside the formatter, never at filter time.

    The first version of these tests pre-populated `record.exc_text` and called
    the filter directly. That passed against code which leaked every real
    exception, and was caught in review on PR #148: `logging` renders
    `exc_info` in `Formatter.format`, and a handler filters *before* it
    formats, so no filter ever sees a traceback. These tests go through a real
    logger with a real raised exception instead.
    """

    def test_without_redaction_an_exception_leaks_the_key(self):
        """Baseline — this is the hole the review found."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)

        logger = logging.getLogger("test_log_redaction_exc_baseline")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        try:
            raise RuntimeError(
                f"GET https://maps.googleapis.com/x?key={FAKE_GOOGLE_KEY}"
            )
        except RuntimeError:
            logger.exception("request failed")
        logger.handlers = []

        assert FAKE_GOOGLE_KEY in stream.getvalue()

    def test_a_raised_exception_carrying_a_key_is_redacted(self, captured):
        """The real path: raise, `logger.exception`, read the handler output."""
        logger, stream = captured

        try:
            raise RuntimeError(
                f"GET https://maps.googleapis.com/x?key={FAKE_GOOGLE_KEY}"
            )
        except RuntimeError:
            logger.exception("request failed")

        output = stream.getvalue()
        assert FAKE_GOOGLE_KEY not in output, "the traceback carried the key out"
        assert "AIza" not in output
        assert REDACTED in output
        # Still a usable traceback.
        assert "RuntimeError" in output
        assert "Traceback" in output
        assert "request failed" in output

    def test_the_cached_traceback_is_redacted_in_place(self):
        """A second handler must not re-emit the original from `exc_text`.

        `logging` caches the rendered traceback on the record, so a formatter
        that redacted only its own return value would leave the raw text behind
        for whoever formats next.
        """
        try:
            raise RuntimeError(f"boom key={FAKE_GOOGLE_KEY}")
        except RuntimeError:
            record = logging.LogRecord(
                name="t",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="request failed",
                args=(),
                exc_info=sys.exc_info(),
            )

        RedactingFormatter(logging.Formatter("%(message)s")).format(record)

        assert record.exc_text is not None, "the formatter did not render the traceback"
        assert FAKE_GOOGLE_KEY not in record.exc_text
        assert REDACTED in record.exc_text


class TestBrokenFormatting:
    """A record that cannot be rendered leaks *more*, not less.

    Reported as a BLOCKER on PR #148. `logging` does not drop a record whose
    `%` substitution fails — it calls `Handler.handleError`, which prints the
    format string and the raw arguments to stderr. The filter used to pass such
    a record through untouched, on the reasoning that an unrendered record
    cannot leak a rendered secret. That reasoning was wrong.
    """

    @staticmethod
    def _log_with_broken_formatting(name, msg, *args, install=True):
        stderr = io.StringIO()
        handler = logging.StreamHandler(io.StringIO())

        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if install:
            install_log_redaction(logger)

        with redirect_stderr(stderr):
            logger.error(msg, *args)
        logger.handlers = []
        return stderr.getvalue()

    def test_without_redaction_the_arguments_are_printed(self):
        """Baseline — the reported failing input, unprotected."""
        err = self._log_with_broken_formatting(
            "test_broken_baseline", "token=%s %s", "TOPSECRET", install=False
        )

        assert "TOPSECRET" in err

    def test_the_reported_failing_input_no_longer_leaks(self):
        err = self._log_with_broken_formatting(
            "test_broken_fixed", "token=%s %s", "TOPSECRET"
        )

        assert "TOPSECRET" not in err
        # The diagnostic still says how many arguments arrived, which is what
        # it is for.
        assert "<str>" in err

    def test_too_many_arguments_do_not_leak(self):
        err = self._log_with_broken_formatting(
            "test_broken_extra", "a %s", "TOPSECRET", "X"
        )

        assert "TOPSECRET" not in err
        assert "'<str>', '<str>'" in err

    def test_dict_arguments_do_not_leak(self):
        err = self._log_with_broken_formatting(
            "test_broken_dict", "%(a)s %(b)s", {"a": "token=TOPSECRET"}
        )

        assert "TOPSECRET" not in err

    def test_a_non_string_argument_keeps_its_type_in_the_diagnostic(self):
        err = self._log_with_broken_formatting("test_broken_int", "n=%d %s", 42)

        assert "<int>" in err


class TestASecondReviewRound:
    """Findings from the independent review of the merged head.

    Each one is a route to `handleError`, to a cached record attribute, or to
    the caller — reached past the three the earlier rounds closed.
    """

    def test_a_broken_handler_format_string_does_not_print_the_arguments(self):
        """`handleError` is reachable from the *formatter*, not just the filter.

        The record renders fine, so the filter's own guard never runs; then the
        handler's format string names a field the record does not carry, and
        `handleError` prints the raw arguments to stderr.
        """
        stderr = io.StringIO()
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(logging.Formatter("%(nonexistent)s %(message)s"))

        logger = logging.getLogger("test_broken_formatter")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        install_log_redaction(logger)

        # Held in a variable rather than written inline: `handleError` prints
        # the call stack, source lines included, so a literal here would show up
        # in stderr as the *test's* own text and prove nothing either way.
        secret = SECRET_VALUE
        with redirect_stderr(stderr):
            logger.error("%s", secret)
        logger.handlers = []

        err = stderr.getvalue()
        assert "TOPSECRET" not in err, "the broken formatter printed the argument"
        assert "<str>" in err

    def test_a_message_whose_str_raises_does_not_reach_the_caller(self):
        """A filter that raises turns a log call into an application failure.

        `logging` catches nothing around `Filter.filter`, so an exception here
        travels out of `logger.error(...)`. `str(record.msg)` runs the object's
        own `__str__` — the very thing that failed rendering a moment earlier.
        """

        class Unprintable:
            def __str__(self):
                raise ValueError("cannot render")

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("test_unprintable_msg")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        install_log_redaction(logger)

        # Not written inline: `handleError` prints the call stack with source
        # lines, so a literal here would land in stderr as the test's own text.
        secret = SECRET_VALUE
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            logger.error(Unprintable(), secret)  # used to raise inside the filter
        logger.handlers = []

        assert "TOPSECRET" not in stderr.getvalue()

    def test_a_credential_in_stack_info_is_redacted_on_the_record(self):
        """`stack_info` is cached like `exc_text`, so it is redacted in place.

        A second handler formatting the same record would otherwise render the
        original text straight out of the attribute.
        """
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=None,
        )
        record.stack_info = f'  File "x.py", line 1\n    call("?key={FAKE_GOOGLE_KEY}")'

        RedactingFormatter(logging.Formatter("%(message)s")).format(record)

        assert FAKE_GOOGLE_KEY not in record.stack_info
        assert REDACTED in record.stack_info

    @pytest.mark.parametrize(
        "line",
        [
            "api_key=sk-live-TOPSECRET",
            "token: sk-live-TOPSECRET",
            "Using access_token=sk-live-TOPSECRET for the call",
            "password=sk-live-TOPSECRET",
        ],
    )
    def test_an_unquoted_credential_outside_a_query_string_is_stripped(self, line):
        """Neither JSON nor a URL: `logger.info("api_key=%s", key)` renders this.

        The query-parameter pattern needs a `?` or `&` to anchor to and the
        structured pattern needs quotes, so this shape fell between them.
        """
        redacted = redact(line)

        assert "TOPSECRET" not in redacted
        assert REDACTED in redacted

    def test_the_unquoted_pattern_still_leaves_preset_names_alone(self):
        """Widening must not start redacting the codebase's own data."""
        text = "sorting by sort_key=price, monkey=1"

        assert redact(text) == text
        assert redact('{"key": "airport"}') == '{"key": "airport"}'


class TestAThirdReviewRound:
    """Findings from the review of the previous round's fixes.

    All three are the same mistake seen from different sides: a guard that
    protects one route while a neighbouring one stays open.
    """

    @staticmethod
    def _emit(name, msg, *args, fmt=None):
        """Log one record and return (stderr, the record the handler saw)."""
        seen = {}

        class Spy(logging.Filter):
            def filter(self, record):
                seen["record"] = record
                return True

        stderr = io.StringIO()
        handler = logging.StreamHandler(io.StringIO())
        if fmt:
            handler.setFormatter(logging.Formatter(fmt))

        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        install_log_redaction(logger)
        handler.addFilter(Spy())  # after the redactor, so it sees the result

        with redirect_stderr(stderr):
            logger.error(msg, *args)
        logger.handlers = []
        return stderr.getvalue(), seen.get("record")

    def test_redacting_a_format_string_keeps_its_placeholders(self):
        """Eating a placeholder makes a broken record format cleanly.

        `"GET /x?key=%s %s"` with one argument must reach `handleError`. If the
        query pattern swallows the `%s` into `REDACTED`, what is left has one
        placeholder for one argument, the record emits, and the diagnostic that
        says formatting is broken never appears.
        """
        secret = SECRET_VALUE
        err, _ = self._emit("test_placeholder_kept", "GET /x?key=%s %s", secret)

        assert err.strip(), "the broken-formatting diagnostic was swallowed"
        assert "TOPSECRET" not in err
        assert "<str>" in err

    def test_a_formatter_failure_does_not_leave_the_value_on_the_record(self):
        """`Formatter.format` caches `record.message` before it can fail.

        The arguments are withheld on that path, but the rendered value is
        already sitting on the record for whatever reads it next.
        """
        secret = SECRET_VALUE
        _, record = self._emit(
            "test_message_cache", "%s", secret, fmt="%(nonexistent)s %(message)s"
        )

        assert "TOPSECRET" not in getattr(record, "message", "")

    def test_withholding_that_cannot_name_the_types_still_does_not_raise(self):
        """A mapping argument whose `items()` raises reaches the withholder.

        An exception escaping a filter leaves `logging` altogether and lands in
        the caller — a log line must never do that.
        """

        class ExplodingDict(dict):
            def items(self):
                raise RuntimeError("boom")

        # `_emit` would propagate the RuntimeError out of `logger.error(...)`
        # if the withholder let it through, so reaching the assertions at all is
        # half the proof; the other half is that the record still went through.
        _, record = self._emit(
            "test_exploding_args", "%(a)s %(b)s", ExplodingDict(a="ok")
        )

        assert record is not None, "the record was dropped instead of handled"
        assert record.args == (), "the unnameable arguments were not withheld"

    def test_a_credential_in_a_mapping_key_is_not_printed(self):
        """`handleError` prints the mapping whole, keys included.

        The values become type names, but a key is text the caller chose and
        went out verbatim.
        """
        err, _ = self._emit(
            "test_mapping_key", "%(a)s %(b)s", {"api_key=" + "TOPSECRET": 1}
        )

        assert err.strip(), "the diagnostic did not fire"
        assert "TOPSECRET" not in err

    def test_a_credential_cached_in_asctime_is_redacted(self):
        """Everything `Formatter.format` caches is redacted in place.

        `asctime` only carries one if a `datefmt` does, which is far-fetched —
        it is covered because "redact what is returned but leave the cache" is
        the mistake `exc_text` already made once.
        """
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="done",
            args=(),
            exc_info=None,
        )
        inner = logging.Formatter("%(asctime)s %(message)s", datefmt="token=" + "SEC")

        RedactingFormatter(inner).format(record)

        assert "SEC" not in record.asctime
        assert REDACTED in record.asctime


class TestAFourthReviewRound:
    """Findings from the review of the third round.

    Two are the same partial-coverage shape as before; the third is the
    allow-list mistake wearing quotes.
    """

    def test_a_quoted_value_ends_at_its_own_quote(self):
        """Either quote at either end truncates the value at the wrong one.

        `password="abc'TOPSECRET"` matched the opening `"` and the inner `'`,
        redacting three characters and leaving the rest — the Bearer bug again
        in a different costume.
        """
        redacted = redact("password=\"abc'" + "TOPSECRET" + '"')

        assert "TOPSECRET" not in redacted
        assert redacted == f'password="{REDACTED}"'

    def test_a_formatter_that_raises_a_credential_does_not_print_it(self):
        """`handleError` prints the exception and its traceback.

        An inner formatter raising `ValueError("api_key=…")` reports the
        credential itself, and chaining would carry the original along even if
        the replacement were redacted.
        """
        secret = "api_key=" + "TOPSECRET"

        class Exploding(logging.Formatter):
            def format(self, record):
                raise ValueError(secret)

        stderr = io.StringIO()
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(Exploding())

        logger = logging.getLogger("test_formatter_raises_secret")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        install_log_redaction(logger)

        with redirect_stderr(stderr):
            logger.error("something happened")
        logger.handlers = []

        err = stderr.getvalue()
        assert err.strip(), "the broken formatter was not reported at all"
        assert "TOPSECRET" not in err
        assert REDACTED in err

    def test_the_failure_path_also_sweeps_the_cache(self):
        """`asctime` is assigned before the format string is applied.

        The successful path redacts the cache; the failing one dropped
        `message` and left everything else half-built.
        """
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="done",
            args=(),
            exc_info=None,
        )
        inner = logging.Formatter(
            "%(asctime)s %(missing)s", datefmt="api_key=" + "TOPSECRET"
        )

        with pytest.raises(Exception):
            RedactingFormatter(inner).format(record)

        assert "TOPSECRET" not in getattr(record, "asctime", "")


class TestInstallation:
    def test_installing_twice_does_not_stack_filters(self, captured):
        """One filter per handler, however many times the installer runs.

        Asserted per handler rather than by the return value: pytest's logging
        plugin attaches handlers of its own after the fixture has run, so a
        second call legitimately covers those. What must never happen is two
        redacting filters on one handler, each redacting the other's output.
        """
        logger, _ = captured

        install_log_redaction(logger)
        install_log_redaction(logger)

        for handler in logger.handlers:
            installed = [
                f for f in handler.filters if isinstance(f, SecretRedactingFilter)
            ]
            assert len(installed) == 1, f"{handler!r} carries {len(installed)} filters"
            assert isinstance(handler.formatter, RedactingFormatter)
            # And the wrapper must not have wrapped itself.
            assert not isinstance(handler.formatter.inner, RedactingFormatter)

    def test_both_halves_are_installed(self, captured):
        """Filter for the message, formatter for the traceback — both or neither.

        Pinned separately because installing only the filter is exactly the
        state PR #148 was blocked in: it looks covered and leaks every
        exception.
        """
        logger, _ = captured

        for handler in logger.handlers:
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters), (
                "message redaction missing"
            )
            assert isinstance(handler.formatter, RedactingFormatter), (
                "traceback redaction missing"
            )

    def test_it_reports_when_there_is_nothing_to_cover(self):
        """Zero handlers means the call achieved nothing - the caller can tell."""
        logger = logging.getLogger("test_log_redaction_empty")
        logger.handlers = []

        assert install_log_redaction(logger) == 0

    def test_the_application_installs_it_on_the_root_handlers(self):
        """Importing the app must leave the root logger covered.

        This is the half that matters in production: records from urllib3
        propagate to the root handlers, so that is where the filter has to sit.
        A filter on the root *logger* would never see them.
        """
        import app as app_module  # noqa: F401  (import runs the setup)

        root = logging.getLogger()
        assert root.handlers, "root logger has no handlers to filter"
        for handler in root.handlers:
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters), (
                "a root handler would write credentials unredacted"
            )
