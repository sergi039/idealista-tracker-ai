"""Issue #244: the shim swallowed the model, so #226 was a no-op for Land.

#226 made every analysis report the model that *ran* rather than the one that
was asked for. `AnthropicService` reads it off the response object — which, on
this deployment, is not `anthropic.Anthropic` but the `SubscriptionClient` shim
in `services/subscription_transport.py`. Its `_Message` had no `model` field, so
`getattr(message, "model", None) or DEFAULT_MODEL` always took the fallback and
stored the configured alias, exactly what #226 removed everywhere else.

Two halves are pinned: the shim carries the bridge's answer through, and the
service never substitutes `DEFAULT_MODEL` for an unknown one.
"""

from unittest.mock import patch

import pytest

from services import subscription_transport
from services.subscription_transport import SubscriptionClient


def _bridge_answer(model):
    return {
        "text": "{}",
        "provider": "claude",
        "model": model,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }


class TestTheShimCarriesTheModel:
    @pytest.mark.parametrize("reported", ["claude-sonnet-5", None])
    def test_it_reports_what_the_bridge_said(self, reported):
        client = SubscriptionClient(provider="claude", default_model="configured-id")

        with patch.object(
            subscription_transport, "complete", return_value=_bridge_answer(reported)
        ):
            message = client.messages.create(
                messages=[{"role": "user", "content": "hi"}], model="configured-id"
            )

        assert message.model == reported, (
            "the shim dropped the bridge's answer, which is what made the "
            "#226 fix a no-op on this path"
        )

    def test_it_does_not_fall_back_to_the_configured_id(self):
        client = SubscriptionClient(provider="claude", default_model="configured-id")

        with patch.object(
            subscription_transport, "complete", return_value=_bridge_answer(None)
        ):
            message = client.messages.create(
                messages=[{"role": "user", "content": "hi"}]
            )

        assert message.model is None


class TestTheServiceDoesNotSubstitute:
    def test_default_model_is_never_a_stand_in_for_an_unknown_one(self):
        """The service must read the answer, not paper over its absence."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "services" / "anthropic_service.py"
        ).read_text(encoding="utf-8")

        assert 'getattr(message, "model", None) or DEFAULT_MODEL' not in source
        assert source.count('"model": getattr(message, "model", None)') == 3, (
            "all three response shapes report what answered, or nothing"
        )
