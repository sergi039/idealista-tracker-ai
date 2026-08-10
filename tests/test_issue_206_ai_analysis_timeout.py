"""#206 item 3: one source for the AI analysis timeout, not four literals.

Before this, `timeout=600` was spelled out independently in
`services/openai_service.py` and `services/property_ai_service.py`, and
`services/subscription_transport.py`'s `_post()` added a hardcoded `+ 15`
margin -- exactly `3 * KILL_GRACE_SECONDS` at the bridge's default
`AI_BRIDGE_KILL_GRACE`, with no slack for anything else. These tests pin the
wiring: the two analysis call sites ask for `config.py`'s
`AI_ANALYSIS_TIMEOUT_SECONDS`, and the transport's socket timeout tracks
`config.py`'s `AI_BRIDGE_SOCKET_MARGIN_SECONDS` rather than a bare literal.
"""

import json
from unittest.mock import patch

import pytest

from config import Config
from services import subscription_transport
from services.openai_service import OpenAIService
from services.property_ai_service import PropertyAIService


class TestPropertyAIServiceUsesTheConfiguredTimeout:
    def test_claude_path_asks_for_the_configured_timeout(self, monkeypatch):
        captured = {}

        def fake_complete(prompt, **kwargs):
            captured.update(kwargs)
            return {"text": "{}"}

        monkeypatch.setattr(subscription_transport, "complete", fake_complete)
        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = PropertyAIService()
            service._analyze_claude("prompt text")

        assert captured["timeout"] == Config.AI_ANALYSIS_TIMEOUT_SECONDS

    def test_openai_path_asks_for_the_configured_timeout(self, monkeypatch):
        captured = {}

        def fake_complete(prompt, **kwargs):
            captured.update(kwargs)
            return {"text": "{}"}

        monkeypatch.setattr(subscription_transport, "complete", fake_complete)
        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = PropertyAIService()
            service._analyze_openai("prompt text")

        assert captured["timeout"] == Config.AI_ANALYSIS_TIMEOUT_SECONDS


class TestOpenAIServiceUsesTheConfiguredTimeout:
    def test_analyze_property_structured_asks_for_the_configured_timeout(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        captured = {}

        def fake_complete(prompt, **kwargs):
            captured.update(kwargs)
            return {"text": "{}"}

        monkeypatch.setattr(subscription_transport, "complete", fake_complete)
        monkeypatch.setattr(
            "services.market_analysis_service.MarketAnalysisService.get_enriched_data",
            lambda self, land: {},
        )

        class _EmptyQuery:
            def filter(self, *a, **k):
                return self

            def filter_by(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def all(self):
                return []

        monkeypatch.setattr("app.db.session.query", lambda *a, **k: _EmptyQuery())

        land = SimpleNamespace(
            id=1,
            title="Test land",
            price=100000,
            area=500,
            municipality="Valencia",
            land_type="developed",
            score_total=50,
            travel_time_nearest_beach=None,
            nearest_beach_name=None,
            travel_time_oviedo=None,
            travel_time_gijon=None,
            travel_time_airport=None,
            description=None,
        )

        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = OpenAIService()
            service.analyze_property_structured(land)

        assert captured["timeout"] == Config.AI_ANALYSIS_TIMEOUT_SECONDS


class TestSubscriptionTransportSocketMargin:
    def test_the_socket_timeout_tracks_the_configured_bridge_margin(self, monkeypatch):
        """The old `timeout + 15` is now `timeout + AI_BRIDGE_SOCKET_MARGIN_
        SECONDS`, which itself tracks the bridge's own AI_BRIDGE_KILL_GRACE
        rather than a bare literal that could silently fall out of sync."""
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_a, **_k):
                return json.dumps({"text": "ok"}).encode()

        def fake_urlopen(request, timeout=None):
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setattr(
            subscription_transport.urllib.request, "urlopen", fake_urlopen
        )
        with (
            patch.object(Config, "AI_BRIDGE_URL", "http://bridge.example"),
            patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"),
        ):
            subscription_transport.complete("prompt", timeout=42)

        assert captured["timeout"] == pytest.approx(
            42 + Config.AI_BRIDGE_SOCKET_MARGIN_SECONDS
        )

    def test_the_margin_formula_matches_the_bridges_own_kill_sequence(self):
        """Pins the formula itself, not just today's numbers: the socket
        margin must stay `3 * AI_BRIDGE_KILL_GRACE_SECONDS + AI_BRIDGE_
        REQUEST_MARGIN_SECONDS` (the bridge's SIGTERM wait, post-SIGKILL
        wait and pipe drain, plus real slack) so a future edit that
        hardcodes a number again, instead of deriving it, is caught here."""
        assert Config.AI_BRIDGE_SOCKET_MARGIN_SECONDS == pytest.approx(
            3 * Config.AI_BRIDGE_KILL_GRACE_SECONDS
            + Config.AI_BRIDGE_REQUEST_MARGIN_SECONDS
        )
