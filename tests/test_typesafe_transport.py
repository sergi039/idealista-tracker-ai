"""services/typesafe_transport.py: one POST, every failure as one error type.

No network: `http.client.HTTPSConnection` is replaced by a scripted stand-in
per test. `Config` is only touched through `patch.object`, which restores it
-- tests/conftest.py fails the session on a Config attribute that changed or
appeared.
"""

import http.client
import io
import json
import threading
from unittest.mock import patch

import pytest

from config import Config
from services import typesafe_transport as ts

QUESTIONS = {"q": {"type": "noul", "instructions": "Does this convey urgency?"}}
GOOD_BODY = json.dumps(
    {
        "model": "jev-1.13.0",
        "answers": {"q": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }
).encode()


class _Response:
    def __init__(self, status=200, body=b"", incremental=True):
        self.status = status
        self._buf = io.BytesIO(body)
        self.closed = False
        if incremental:
            self.read1 = self._buf.read1

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        self.closed = True


class _Connection:
    """Scripted stand-in for `http.client.HTTPSConnection`."""

    script: dict = {}
    made: list = []

    def __init__(self, host, port=None, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.closed = threading.Event()
        _Connection.made.append(self)

    def request(self, method, path, body=None, headers=None):
        self.method, self.path, self.body, self.headers = method, path, body, headers
        exc = self.script.get("raise_on_request")
        if exc is not None:
            raise exc

    def getresponse(self):
        if self.script.get("hang"):
            # Blocked until the watchdog closes the connection, like a peer
            # that never finishes its headers.
            self.closed.wait(5.0)
            raise OSError(9, "Bad file descriptor")
        exc = self.script.get("raise_on_response")
        if exc is not None:
            raise exc
        return self.script["response"]

    def close(self):
        self.closed.set()


@pytest.fixture
def connection(monkeypatch):
    _Connection.script = {}
    _Connection.made = []
    monkeypatch.setattr(ts.http.client, "HTTPSConnection", _Connection)
    return _Connection


def _with_key(**overrides):
    values = {"TYPESAFE_API_KEY": "test-key", **overrides}
    patches = [patch.object(Config, name, value) for name, value in values.items()]

    class _All:
        def __enter__(self):
            for p in patches:
                p.__enter__()

        def __exit__(self, *exc):
            for p in reversed(patches):
                p.__exit__(*exc)

    return _All()


class TestRequestShape:
    def test_without_a_key_the_route_is_absent_and_no_connection_is_made(
        self, connection
    ):
        with patch.object(Config, "TYPESAFE_API_KEY", None):
            with pytest.raises(ts.TypeSafeNotConfigured):
                ts.system_one("text", QUESTIONS)
            assert ts.is_configured() is False
        assert connection.made == []

    def test_the_request_carries_key_pinned_model_questions_and_timeout(
        self, connection
    ):
        connection.script = {"response": _Response(200, GOOD_BODY)}
        with _with_key(TYPESAFE_TIMEOUT_SECONDS=7.5):
            payload = ts.system_one({"listing_text": "vistas al mar"}, QUESTIONS)
        made = connection.made[0]
        assert payload["answers"]["q"]["noul"] == 0.9
        assert (made.host, made.port, made.timeout) == ("api.typesafe.ai", 443, 7.5)
        assert (made.method, made.path) == ("POST", "/v1/systemone")
        assert made.headers["Authorization"] == "Bearer test-key"
        assert json.loads(made.body) == {
            "state": {"listing_text": "vistas al mar"},
            "model": Config.TYPESAFE_MODEL,
            "questions": QUESTIONS,
        }
        assert Config.TYPESAFE_MODEL == "jev-1.13.0", "the threshold was tuned on 1.13"
        assert made.closed.is_set(), "the connection is closed after the exchange"

    def test_a_path_prefix_in_the_origin_is_kept(self, connection):
        connection.script = {"response": _Response(200, GOOD_BODY)}
        with _with_key(TYPESAFE_API_URL="https://proxy.example:8443/typesafe/"):
            ts.system_one("text", QUESTIONS)
        made = connection.made[0]
        assert (made.host, made.port, made.path) == (
            "proxy.example",
            8443,
            "/typesafe/v1/systemone",
        )

    @pytest.mark.parametrize(
        "url, fragment",
        [
            ("http://api.typesafe.ai", "https"),
            ("https://[bad", "valid URL"),
            ("https://api.typesafe.ai:99999", "valid URL"),
            ("", "https"),
        ],
    )
    def test_a_bad_origin_is_refused_before_a_connection_is_made(
        self, connection, url, fragment
    ):
        with _with_key(TYPESAFE_API_URL=url):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert fragment in str(info.value)
        assert connection.made == []


class TestStatuses:
    def test_an_http_error_keeps_its_status_and_never_reads_the_body(self, connection):
        secret = "apikey_" + "s" * 40
        response = _Response(401, f'{{"error": "invalid key {secret}"}}'.encode())
        connection.script = {"response": response}
        with _with_key(TYPESAFE_API_KEY=secret):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert str(info.value) == "typesafe returned 401"
        assert info.value.status == 401
        assert response.closed and response._buf.tell() == 0

    def test_a_redirect_is_a_status_never_followed(self, connection):
        connection.script = {"response": _Response(302, b"")}
        with _with_key():
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert info.value.status == 302
        assert len(connection.made) == 1


class TestFailures:
    def test_an_os_error_is_reported_by_class_and_errno(self, connection):
        connection.script = {"raise_on_response": ConnectionRefusedError(61, "refused")}
        with _with_key():
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert info.value.status is None
        assert "ConnectionRefusedError(61)" in str(info.value)

    def test_a_malformed_status_line_is_reported_by_class_not_content(self, connection):
        marker = "SENSITIVE_MARKER_" + "m" * 30
        connection.script = {"raise_on_response": http.client.BadStatusLine(marker)}
        with _with_key():
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert marker not in str(info.value)
        assert "BadStatusLine" in str(info.value)

    def test_the_watchdog_cuts_an_exchange_that_never_finishes(self, connection):
        """A peer that sends a header byte every few seconds satisfies every
        socket timeout and would otherwise block `getresponse()` for days."""
        connection.script = {"hang": True}
        with _with_key(TYPESAFE_TIMEOUT_SECONDS=0.5):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "time allowance" in str(info.value)
        assert info.value.__cause__ is None
        assert connection.made[0].closed.is_set()

    def test_a_header_refusal_at_send_time_drops_the_chain(self, connection):
        secret = "apikey_" + "s" * 40
        connection.script = {
            "raise_on_request": ValueError(
                f"Invalid header value b'Bearer {secret}\\n'"
            )
        }
        with _with_key(TYPESAFE_API_KEY=secret):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert secret not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__ is True


class TestTheKeyNeverReachesAnErrorMessage:
    def test_a_key_with_a_trailing_newline_is_refused_before_anything_is_sent(
        self, connection
    ):
        secret = "apikey_" + "s" * 40 + "\n"
        with patch.object(Config, "TYPESAFE_API_KEY", secret):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "malformed" in str(info.value)
        assert "apikey_" not in str(info.value)
        assert connection.made == []

    @pytest.mark.parametrize("bad", ["with space", "tab\there", "ключ", ""])
    def test_other_malformed_keys_are_refused_too(self, connection, bad):
        with patch.object(Config, "TYPESAFE_API_KEY", bad):
            with pytest.raises(ts.TypeSafeTransportError):
                ts.system_one("text", QUESTIONS)
        assert connection.made == []


class TestBodies:
    def test_a_body_over_the_bound_is_refused_not_truncated(self, connection):
        big = (
            b'{"answers": {"q": {"noul": 0.5}}, "pad": "' + b"x" * ts.MAX_RESPONSE_BYTES
        )
        connection.script = {"response": _Response(200, big)}
        with _with_key():
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "over" in str(info.value)

    @pytest.mark.parametrize(
        "body, fragment",
        [
            (b"<html>maintenance</html>", "non-JSON"),
            (b"[" * 1100 + b"]" * 1100, "non-JSON"),
            (b'{"model": "jev-1.13.0"}', "answers"),
            (b'[{"answers": {}}]', "answers"),
            (b'{"answers": null}', "answers"),
        ],
    )
    def test_a_body_without_answers_is_refused(self, connection, body, fragment):
        connection.script = {"response": _Response(200, body)}
        with _with_key():
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert fragment in str(info.value)

    def test_a_response_without_read1_is_refused(self, connection):
        connection.script = {"response": _Response(200, GOOD_BODY, incremental=False)}
        with _with_key():
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "incrementally" in str(info.value)

    def test_the_real_response_class_reads_incrementally(self):
        assert callable(getattr(http.client.HTTPResponse, "read1", None))
