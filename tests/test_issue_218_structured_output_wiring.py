"""End-to-end wiring for issue #218: the category schema built in
`services/property_ai_service.py` has to actually reach
`services/subscription_transport.py`'s `complete()`, and a caller that does
not pass one has to see exactly the old payload shape.

The schema's own content is pinned in tests/test_ai_structured_schemas.py;
the CLI-flag/cleanup/fallback behaviour on the bridge side is pinned in
tests/test_ai_bridge_schema.py. This file is the middle hop: does
`PropertyAIService` actually pass the right schema for the right category,
and does `subscription_transport.complete()` forward it (or its absence)
faithfully.
"""

import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from config import Config
from services import subscription_transport
from services.property_ai_service import (
    GENERIC_STRUCTURED_JSON_SCHEMA,
    HOUSING_STRUCTURED_JSON_SCHEMA,
    LAND_STRUCTURED_JSON_SCHEMA,
    PropertyAIService,
)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_a, **_k):
        return self._body


class TestSubscriptionTransportSchemaPassthrough:
    def test_without_a_schema_the_payload_carries_null(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data)
            return _FakeResponse(json.dumps({"text": "ok"}).encode())

        monkeypatch.setattr(
            subscription_transport.urllib.request, "urlopen", fake_urlopen
        )
        with (
            patch.object(Config, "AI_BRIDGE_URL", "http://bridge.example"),
            patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"),
        ):
            subscription_transport.complete("prompt")

        assert captured["payload"]["schema"] is None
        # Every other key stays exactly what a pre-#218 caller sent.
        assert set(captured["payload"]) == {
            "provider",
            "prompt",
            "system",
            "model",
            "timeout",
            "schema",
        }

    def test_a_given_schema_reaches_the_bridge_payload_unchanged(self, monkeypatch):
        schema = {"type": "object", "properties": {}, "required": []}
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data)
            return _FakeResponse(json.dumps({"text": "ok"}).encode())

        monkeypatch.setattr(
            subscription_transport.urllib.request, "urlopen", fake_urlopen
        )
        with (
            patch.object(Config, "AI_BRIDGE_URL", "http://bridge.example"),
            patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"),
        ):
            subscription_transport.complete("prompt", schema=schema)

        assert captured["payload"]["schema"] == schema


class TestPropertyAIServiceSendsTheCategorySchema:
    @pytest.fixture
    def app(self):
        from tests import setup_test_environment

        setup_test_environment()
        from app import create_app, db

        app = create_app()
        app.config["TESTING"] = True
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()

    def _property(self, category):
        from models import Property

        return Property(
            source_email_id=f"issue218_{category}",
            title="Test property",
            municipality="Barcelona",
            property_category=category,
            property_subtype="apartment",
            price=Decimal("250000.00"),
            area=Decimal("90.00"),
        )

    @pytest.mark.parametrize(
        ("category", "expected_schema"),
        [
            ("housing", HOUSING_STRUCTURED_JSON_SCHEMA),
            ("new_development", HOUSING_STRUCTURED_JSON_SCHEMA),
            ("land", LAND_STRUCTURED_JSON_SCHEMA),
            ("commercial", GENERIC_STRUCTURED_JSON_SCHEMA),
        ],
    )
    def test_claude_path_sends_the_matching_category_schema(
        self, app, monkeypatch, category, expected_schema
    ):
        from app import db

        captured = {}

        def fake_complete(prompt, **kwargs):
            captured.update(kwargs)
            return {"text": "{}"}

        monkeypatch.setattr(subscription_transport, "complete", fake_complete)

        with app.app_context():
            prop = self._property(category)
            db.session.add(prop)
            db.session.commit()

            with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
                service = PropertyAIService()
                service.analyze_property_structured(prop, provider="claude")

        assert captured["schema"] == expected_schema

    def test_openai_path_sends_the_matching_category_schema(self, app, monkeypatch):
        from app import db

        captured = {}

        def fake_complete(prompt, **kwargs):
            captured.update(kwargs)
            return {"text": "{}"}

        monkeypatch.setattr(subscription_transport, "complete", fake_complete)

        with app.app_context():
            prop = self._property("land")
            db.session.add(prop)
            db.session.commit()

            with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
                service = PropertyAIService()
                service.analyze_property_structured(prop, provider="openai")

        assert captured["schema"] == LAND_STRUCTURED_JSON_SCHEMA
