"""services/typesafe_transport.py: one POST, every failure as one error type.

No network: `urllib.request.urlopen` is replaced per test. `Config` is only
touched through `patch.object`, which restores it -- tests/conftest.py fails
the session on a Config attribute that changed or appeared.
"""

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from config import Config
from services import typesafe_transport as ts

QUESTIONS = {"q": {"type": "noul", "instructions": "Does this convey urgency?"}}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _urlopen_returning(body: bytes, captured: dict):
    def _urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data)
        return _Response(body)

    return _urlopen


class TestSystemOne:
    def test_without_a_key_the_route_is_absent_and_nothing_is_sent(self, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("no request may leave without a key")

        monkeypatch.setattr(ts.urllib.request, "urlopen", _explode)
        with patch.object(Config, "TYPESAFE_API_KEY", None):
            with pytest.raises(ts.TypeSafeNotConfigured):
                ts.system_one("text", QUESTIONS)
            assert ts.is_configured() is False

    def test_the_request_carries_key_model_questions_and_the_configured_timeout(
        self, monkeypatch
    ):
        captured = {}
        body = json.dumps(
            {
                "model": "jev-1.13.0",
                "answers": {"q": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
        ).encode()
        monkeypatch.setattr(
            ts.urllib.request, "urlopen", _urlopen_returning(body, captured)
        )
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
            "model": "jev-latest",
            "questions": QUESTIONS,
        }

    def test_an_http_error_keeps_its_status_and_is_never_a_urllib_error(
        self, monkeypatch
    ):
        def _urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                None,
                io.BytesIO(b'{"error": "Missing or invalid API key"}'),
            )

        monkeypatch.setattr(ts.urllib.request, "urlopen", _urlopen)
        with patch.object(Config, "TYPESAFE_API_KEY", "bad-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert info.value.status == 401
        assert "401" in str(info.value)

    def test_a_timeout_is_a_transport_error_without_a_status(self, monkeypatch):
        def _urlopen(request, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(ts.urllib.request, "urlopen", _urlopen)
        with patch.object(Config, "TYPESAFE_API_KEY", "test-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert info.value.status is None
        assert "unreachable" in str(info.value)

    @pytest.mark.parametrize(
        "body, fragment",
        [
            (b"<html>maintenance</html>", "non-JSON"),
            (b'{"model": "jev-1.13.0"}', "answers"),
        ],
    )
    def test_a_body_without_answers_is_refused(self, monkeypatch, body, fragment):
        monkeypatch.setattr(ts.urllib.request, "urlopen", _urlopen_returning(body, {}))
        with patch.object(Config, "TYPESAFE_API_KEY", "test-key"):
            with pytest.raises(ts.TypeSafeTransportError) as info:
                ts.system_one("text", QUESTIONS)
        assert fragment in str(info.value)
