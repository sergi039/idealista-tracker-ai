"""services/typesafe_transport.py: one POST, every failure as one error type.

No network: the module's opener is replaced per test. `Config` is only touched
through `patch.object`, which restores it -- tests/conftest.py fails the session
on a Config attribute that changed or appeared.
"""

import io
import json
import urllib.error
import urllib.request
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


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener_returning(body: bytes, captured: dict):
    def _open(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data)
        return _Response(body)

    return _open


def _opener_raising(exc):
    def _open(request, timeout=None):
        raise exc

    return _open


def _explode(*args, **kwargs):
    raise AssertionError("no request may leave in this case")


class TestSystemOne:
    def test_without_a_key_the_route_is_absent_and_nothing_is_sent(self, monkeypatch):
        monkeypatch.setattr(ts._OPENER, "open", _explode)
        with patch.object(Config, "TYPESAFE_API_KEY", None):
            with pytest.raises(ts.TypeSafeNotConfigured):
                ts.system_one("text", QUESTIONS)
            assert ts.is_configured() is False

    def test_the_request_carries_key_pinned_model_questions_and_timeout(
        self, monkeypatch
    ):
        captured = {}
        monkeypatch.setattr(ts._OPENER, "open", _opener_returning(GOOD_BODY, captured))
        with (
            patch.object(Config, "TYPESAFE_API_KEY", "test-key"),
            patch.object(Config, "TYPESAFE_TIMEOUT_SECONDS", 7.5),
        ):
            payload = ts.system_one({"listing_text": "vistas al mar"}, QUESTIONS)

        assert payload["answers"]["q"]["noul"] == 0.9
        assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
        assert captured["timeout"] == 7.5
        assert captured["authorization"] == "Bearer test-key"
        assert captured["body"] == {
            "state": {"listing_text": "vistas al mar"},
            "model": Config.TYPESAFE_MODEL,
            "questions": QUESTIONS,
        }
        assert Config.TYPESAFE_MODEL == "jev-1.13.0", "the threshold was tuned on 1.13"

    def test_a_plain_http_origin_is_refused_before_anything_is_sent(self, monkeypatch):
        monkeypatch.setattr(ts._OPENER, "open", _explode)
        with (
            patch.object(Config, "TYPESAFE_API_KEY", "test-key"),
            patch.object(Config, "TYPESAFE_API_URL", "http://api.typesafe.ai"),
        ):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "https" in str(info.value)

    def test_a_redirect_is_refused_and_the_opener_carries_that_handler(self):
        assert any(isinstance(h, ts._RefuseRedirects) for h in ts._OPENER.handlers)
        request = urllib.request.Request("https://api.typesafe.ai/v1/systemone")
        with pytest.raises(urllib.error.HTTPError) as info:
            ts._RefuseRedirects().redirect_request(
                request, None, 302, "Found", {}, "https://elsewhere.example/steal"
            )
        assert info.value.code == 302
        assert "refused" in str(info.value.reason)

    def test_an_http_error_keeps_its_status_and_is_never_a_urllib_error(
        self, monkeypatch
    ):
        error = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone",
            401,
            "Unauthorized",
            None,
            io.BytesIO(b'{"error": "Missing or invalid API key"}'),
        )
        monkeypatch.setattr(ts._OPENER, "open", _opener_raising(error))
        with patch.object(Config, "TYPESAFE_API_KEY", "bad-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert info.value.status == 401
        assert "401" in str(info.value)

    def test_a_response_body_is_never_copied_into_the_message(self, monkeypatch):
        """A vendor's error text may echo the request, bearer key included, and
        the message reaches the log and the stored detail."""
        secret = "apikey_" + "s" * 40
        error = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone",
            401,
            "Unauthorized",
            None,
            io.BytesIO(f'{{"error": "invalid key {secret}"}}'.encode()),
        )
        monkeypatch.setattr(ts._OPENER, "open", _opener_raising(error))
        with patch.object(Config, "TYPESAFE_API_KEY", secret):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert str(info.value) == "typesafe returned 401"
        assert secret not in str(info.value)
        assert info.value.status == 401

    def test_an_invalid_url_is_a_transport_error_not_a_valueerror(self, monkeypatch):
        monkeypatch.setattr(ts._OPENER, "open", _explode)
        with (
            patch.object(Config, "TYPESAFE_API_KEY", "test-key"),
            patch.object(Config, "TYPESAFE_API_URL", "https://[bad"),
        ):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "valid URL" in str(info.value)

    def test_a_dripping_body_is_cut_off_at_the_wall_clock_deadline(self, monkeypatch):
        """`timeout` bounds each socket operation; a peer sending one byte at a
        time within it would otherwise keep the read alive indefinitely."""

        class _Drip:
            def read(self, n=-1):
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        clock = {"now": 1000.0}

        def _monotonic():
            clock["now"] += 4.0
            return clock["now"]

        monkeypatch.setattr(ts.time, "monotonic", _monotonic)
        monkeypatch.setattr(ts._OPENER, "open", lambda request, timeout=None: _Drip())
        with (
            patch.object(Config, "TYPESAFE_API_KEY", "test-key"),
            patch.object(Config, "TYPESAFE_TIMEOUT_SECONDS", 10.0),
        ):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "allowance" in str(info.value)
        assert clock["now"] - 1000.0 < 30.0, (
            "the loop stopped shortly after the deadline"
        )

    def test_a_timeout_is_a_transport_error_without_a_status(self, monkeypatch):
        monkeypatch.setattr(ts._OPENER, "open", _opener_raising(TimeoutError("slow")))
        with patch.object(Config, "TYPESAFE_API_KEY", "test-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert info.value.status is None
        assert "unreachable" in str(info.value)

    def test_a_body_over_the_bound_is_refused_not_truncated(self, monkeypatch):
        big = (
            b'{"answers": {"q": {"noul": 0.5}}, "pad": "' + b"x" * ts.MAX_RESPONSE_BYTES
        )
        monkeypatch.setattr(ts._OPENER, "open", _opener_returning(big, {}))
        with patch.object(Config, "TYPESAFE_API_KEY", "test-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert "over" in str(info.value)

    @pytest.mark.parametrize(
        "body, fragment",
        [
            (b"<html>maintenance</html>", "non-JSON"),
            (b'{"model": "jev-1.13.0"}', "answers"),
            (b'[{"answers": {}}]', "answers"),
            (b'{"answers": null}', "answers"),
        ],
    )
    def test_a_body_without_answers_is_refused(self, monkeypatch, body, fragment):
        monkeypatch.setattr(ts._OPENER, "open", _opener_returning(body, {}))
        with patch.object(Config, "TYPESAFE_API_KEY", "test-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert fragment in str(info.value)
