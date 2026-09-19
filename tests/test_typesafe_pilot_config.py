"""The Jev pilot's settings fail closed and are counted into the Enrich budget."""

from unittest.mock import patch

import pytest

import config
from config import Config
from services import enrich_budget
from services.subscription_transport import DEFAULT_TIMEOUT_SECONDS


class TestBoundedFloat:
    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1.5", "-0.1", "abc"])
    def test_a_setting_outside_its_range_refuses_to_load(self, monkeypatch, raw):
        monkeypatch.setenv("PILOT_TEST_SETTING", raw)
        with pytest.raises(ValueError):
            config._bounded_float("PILOT_TEST_SETTING", 0.7, 0.0, 1.0)

    def test_unset_and_in_range_values_load(self, monkeypatch):
        monkeypatch.delenv("PILOT_TEST_SETTING", raising=False)
        assert config._bounded_float("PILOT_TEST_SETTING", 0.7, 0.0, 1.0) == 0.7
        monkeypatch.setenv("PILOT_TEST_SETTING", "0.85")
        assert config._bounded_float("PILOT_TEST_SETTING", 0.7, 0.0, 1.0) == 0.85

    def test_the_pilot_settings_are_within_their_ranges(self):
        assert 0.0 <= Config.SEA_VIEW_TEXT_MIN_CONFIDENCE <= 1.0
        assert 0.5 <= Config.TYPESAFE_TIMEOUT_SECONDS <= 120.0
        assert Config.TYPESAFE_MODEL == "jev-1.13.0"


class TestEnrichBudgetCountsJev:
    def test_the_ai_allowance_counts_jev_twice_before_the_bridge(self):
        """Connect timeout plus the transport's read deadline, both the same
        setting, then the bridge's own allowance."""
        margin = Config.AI_BRIDGE_SOCKET_MARGIN_SECONDS
        with patch.object(Config, "TYPESAFE_TIMEOUT_SECONDS", 10.0):
            assert enrich_budget.ai_allowance_seconds() == pytest.approx(
                20.0 + DEFAULT_TIMEOUT_SECONDS + margin
            )
        with patch.object(Config, "TYPESAFE_TIMEOUT_SECONDS", 25.0):
            assert enrich_budget.ai_allowance_seconds() == pytest.approx(
                50.0 + DEFAULT_TIMEOUT_SECONDS + margin
            )

    def test_the_poll_ceiling_moves_with_twice_the_jev_timeout(self):
        with patch.object(Config, "TYPESAFE_TIMEOUT_SECONDS", 10.0):
            base = enrich_budget.poll_timeout_ms()
        with patch.object(Config, "TYPESAFE_TIMEOUT_SECONDS", 40.0):
            assert enrich_budget.poll_timeout_ms() == base + 60_000
