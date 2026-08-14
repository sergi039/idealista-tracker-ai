"""The market-settings AI refresh applies whole or not at all.

The /criteria "Refresh with AI" button runs one bridged Claude call and
overwrites the single ``market_settings`` row. These tests pin the contract:
a full, in-bounds answer is applied in one commit and moves ``updated_at``;
a bridge failure, invalid JSON, or any out-of-bounds value leaves the stored
row byte-for-byte as it was; and the page renders the "Last updated" stamp
the button's work is judged by.
"""

import copy
import json

import pytest

from app import create_app, db
from services import subscription_transport
from tests import setup_test_environment

REFRESH_ENDPOINT = "/criteria/refresh_market_settings"

VALID_PAYLOAD = {
    "construction_costs": {
        "basic": {"min": 1300, "avg": 1550, "max": 1800},
        "premium": {"min": 1800, "avg": 2200, "max": 2700},
    },
    "purchase_costs_ratio": 0.11,
    "rental_adjustments": {
        "urban": {
            "vacancy_rate": 0.05,
            "operating_expenses": 0.15,
            "management_fee": 0.0,
        },
        "suburban": {
            "vacancy_rate": 0.08,
            "operating_expenses": 0.15,
            "management_fee": 0.0,
        },
        "rural": {
            "vacancy_rate": 0.18,
            "operating_expenses": 0.18,
            "management_fee": 0.10,
        },
    },
    "rental_prices": {
        "urban": {"min": 10, "avg": 12, "max": 14},
        "suburban": {"min": 8, "avg": 10, "max": 12},
        "rural": {"min": 6, "avg": 8, "max": 10},
    },
    "sources_note": "Idealista provincial reports and 2026 build-cost guides.",
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _settings_snapshot():
    from models import MarketSettings

    settings = MarketSettings.get_settings()
    return {
        column.name: getattr(settings, column.name)
        for column in MarketSettings.__table__.columns
    }


def _mock_bridge(monkeypatch, payload):
    calls = {}

    def fake_complete(prompt, **kwargs):
        calls["prompt"] = prompt
        calls["kwargs"] = kwargs
        return {"text": json.dumps(payload)}

    monkeypatch.setattr(subscription_transport, "complete", fake_complete)
    return calls


def _flashes(client):
    with client.session_transaction() as session:
        return session.get("_flashes", [])


class TestRefreshApplies:
    def test_valid_answer_updates_values_and_timestamp(self, client, monkeypatch):
        before = _settings_snapshot()
        calls = _mock_bridge(monkeypatch, VALID_PAYLOAD)

        response = client.post(REFRESH_ENDPOINT)

        assert response.status_code == 302
        assert response.headers["Location"].endswith("#market-settings")
        assert calls["kwargs"]["schema"] is not None

        after = _settings_snapshot()
        assert after["construction_basic_min"] == 1300
        assert after["construction_basic_avg"] == 1550
        assert after["construction_premium_max"] == 2700
        assert float(after["purchase_costs_ratio"]) == pytest.approx(0.11)
        assert float(after["rural_vacancy_rate"]) == pytest.approx(0.18)
        assert after["urban_rental_avg"] == 12
        assert after["rural_rental_max"] == 10
        assert after["updated_at"] != before["updated_at"]

        category, message = _flashes(client)[-1]
        assert category == "success"
        assert "refreshed via AI" in message

    def test_confirming_answer_still_moves_the_stamp(self, client, monkeypatch):
        """A refresh that changes nothing still records that it ran."""
        _mock_bridge(monkeypatch, VALID_PAYLOAD)
        client.post(REFRESH_ENDPOINT)
        first = _settings_snapshot()["updated_at"]

        client.post(REFRESH_ENDPOINT)
        assert _settings_snapshot()["updated_at"] != first

        category, message = _flashes(client)[-1]
        assert category == "success"
        assert "nothing changed" in message


class TestRefreshRefuses:
    def test_bridge_failure_keeps_stored_values(self, client, monkeypatch):
        before = _settings_snapshot()

        def fail(prompt, **kwargs):
            raise subscription_transport.SubscriptionTransportError(
                "bridge unreachable"
            )

        monkeypatch.setattr(subscription_transport, "complete", fail)

        response = client.post(REFRESH_ENDPOINT)

        assert response.status_code == 302
        assert _settings_snapshot() == before
        category, message = _flashes(client)[-1]
        assert category == "error"
        assert "failed" in message

    def test_out_of_bounds_value_rejects_the_whole_answer(self, client, monkeypatch):
        before = _settings_snapshot()
        payload = copy.deepcopy(VALID_PAYLOAD)
        payload["construction_costs"]["basic"]["min"] = 100  # below the form's 500
        _mock_bridge(monkeypatch, payload)

        client.post(REFRESH_ENDPOINT)

        assert _settings_snapshot() == before
        category, message = _flashes(client)[-1]
        assert category == "error"
        assert "rejected" in message

    def test_min_avg_max_order_is_enforced(self, client, monkeypatch):
        before = _settings_snapshot()
        payload = copy.deepcopy(VALID_PAYLOAD)
        payload["rental_prices"]["urban"] = {"min": 14, "avg": 12, "max": 10}
        _mock_bridge(monkeypatch, payload)

        client.post(REFRESH_ENDPOINT)

        assert _settings_snapshot() == before
        assert _flashes(client)[-1][0] == "error"

    def test_incomplete_answer_is_rejected(self, client, monkeypatch):
        before = _settings_snapshot()
        payload = copy.deepcopy(VALID_PAYLOAD)
        del payload["rental_prices"]
        _mock_bridge(monkeypatch, payload)

        client.post(REFRESH_ENDPOINT)

        assert _settings_snapshot() == before
        assert _flashes(client)[-1][0] == "error"

    def test_non_json_answer_is_rejected(self, client, monkeypatch):
        before = _settings_snapshot()
        monkeypatch.setattr(
            subscription_transport,
            "complete",
            lambda prompt, **kwargs: {"text": "I think construction costs rose."},
        )

        client.post(REFRESH_ENDPOINT)

        assert _settings_snapshot() == before
        assert _flashes(client)[-1][0] == "error"


class TestCriteriaPageShowsTheStamp:
    def test_last_updated_is_rendered(self, client):
        from models import MarketSettings

        settings = MarketSettings.get_settings()
        stamp = settings.updated_at.strftime("%Y-%m-%d %H:%M")

        response = client.get("/criteria")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Last updated:" in body
        assert stamp in body
        assert "Refresh with AI" in body
