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

import pytest

from utils.log_redaction import (
    REDACTED,
    SecretRedactingFilter,
    install_log_redaction,
    redact,
)

# Shaped like a Google key (the literal prefix plus 35 characters) so the
# pattern under test is exercised for real. Assembled from fragments rather
# than written out: a whole-looking key in a source file trips secret scanners
# and teaches the wrong habit, and the repository's pre-push hook rejects it.
FAKE_GOOGLE_KEY = "AI" + "za" + "SyFAKE_TEST_KEY_" + ("0" * 19)


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

    def test_an_ordinary_word_ending_in_key_is_left_alone(self):
        """`monkey=1` is not a credential, and neither is a `sort_key` column."""
        text = "sorting by sort_key=price, monkey=1, donkey=2"

        assert redact(text) == text

    def test_a_message_with_no_secret_is_untouched(self):
        text = "Scheduled ingestion completed. Processed 3 properties"

        assert redact(text) == text


class TestTracebacks:
    def test_a_cached_traceback_is_redacted_too(self, captured):
        """`exc_text` can quote the request URL from the frame that raised."""
        logger, stream = captured

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=None,
        )
        record.exc_text = f"  requests.get('https://x/y?key={FAKE_GOOGLE_KEY}')"

        SecretRedactingFilter().filter(record)

        assert FAKE_GOOGLE_KEY not in record.exc_text
        assert REDACTED in record.exc_text


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
