"""Issue #226: an analysis was labelled with the model that was asked for.

`tools/ai_bridge.py` passes `-m` to the codex CLI only when the configured id
looks like one it ships; anything else is dropped with a warning inside the
host's log — invisible from the app — and the CLI runs its own default model.
The services returned `self.model` regardless, the route persisted it, and the
page printed `Powered by ChatGPT (gpt-4o)` over an analysis some other model
wrote.

The contract pinned here: the bridge reports the id it actually passed, or
`None` when the CLI chose; nothing downstream substitutes the configured id for
it. `None` reads as "unknown" on the page, which is the truth.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from app import create_app, db
from tests import setup_test_environment

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("ai_bridge_model_label", _BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge():
    return _load_bridge()


@pytest.fixture
def fake_cli(bridge, monkeypatch):
    """Run neither CLI; hand the parser a stream it can read."""
    state = {"cmd": None, "stdout": ""}

    def fake_run(cmd, stdin_text, timeout):
        state["cmd"] = cmd
        return state["stdout"]

    monkeypatch.setattr(bridge, "_which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(bridge, "_run", fake_run)
    return state


def _codex_stream(text: str) -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": text},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        ]
    )


class TestTheBridgeReportsWhatItRan:
    def test_a_model_the_cli_ships_is_passed_and_reported(self, bridge, fake_cli):
        fake_cli["stdout"] = _codex_stream('{"ok": true}')

        result = bridge.complete_codex("prompt", "", "gpt-5.6-terra", 60)

        assert "-m" in fake_cli["cmd"]
        assert fake_cli["cmd"][fake_cli["cmd"].index("-m") + 1] == "gpt-5.6-terra"
        assert result["model"] == "gpt-5.6-terra"

    def test_a_model_the_cli_does_not_ship_is_reported_as_none(self, bridge, fake_cli):
        """The defect: this run used the CLI default, and said `gpt-4o`."""
        fake_cli["stdout"] = _codex_stream('{"ok": true}')

        result = bridge.complete_codex("prompt", "", "gpt-4o", 60)

        assert "-m" not in fake_cli["cmd"], "an unknown id must not reach the CLI"
        assert result["model"] is None, (
            "reporting the requested id is what labelled an analysis with a "
            "model nothing confirms was used"
        )

    def test_no_model_asked_for_is_reported_as_none(self, bridge, fake_cli):
        fake_cli["stdout"] = _codex_stream('{"ok": true}')

        assert bridge.complete_codex("prompt", "", "", 60)["model"] is None

    def test_claude_reports_the_model_it_was_given(self, bridge, fake_cli):
        fake_cli["stdout"] = json.dumps({"result": "{}", "usage": {}})

        result = bridge.complete_claude("prompt", "", "claude-sonnet-5", 60)

        assert result["model"] == "claude-sonnet-5"
        assert bridge.complete_claude("prompt", "", "", 60)["model"] is None


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


class TestTheServicesDoNotSubstituteTheConfiguredId:
    def _bridge_answer(self, monkeypatch, module, model):
        payload = {
            "text": json.dumps({"price_analysis": {"verdict": "FAIR_PRICE"}}),
            "provider": "codex",
            "model": model,
        }
        monkeypatch.setattr(
            module.subscription_transport, "complete", lambda *a, **k: payload
        )

    def test_the_property_service_reports_the_bridge_model(self, app, monkeypatch):
        from services import property_ai_service as module

        service = module.PropertyAIService()
        service.bridge_configured = True
        service.openai_model = "gpt-4o"
        self._bridge_answer(monkeypatch, module, None)

        result = service._analyze_openai("prompt")

        assert result["status"] == "success"
        assert result["model"] is None, "the configured id was asserted again"

    def test_it_reports_the_id_the_bridge_confirms(self, app, monkeypatch):
        from services import property_ai_service as module

        service = module.PropertyAIService()
        service.bridge_configured = True
        service.openai_model = "gpt-4o"
        self._bridge_answer(monkeypatch, module, "gpt-5.6-terra")

        assert service._analyze_openai("prompt")["model"] == "gpt-5.6-terra"

    def test_the_legacy_openai_service_reports_the_bridge_model(self, app, monkeypatch):
        """`OpenAIService` refuses to exist without a bridge token, so the
        constructor is given one rather than the service being faked."""
        from config import Config
        from models import Land
        from services import openai_service as module

        monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "test-token", raising=False)
        service = module.OpenAIService()
        service.model = "gpt-4o"
        self._bridge_answer(monkeypatch, module, None)

        land = Land(source_email_id="issue-226-land", title="A plot")
        db.session.add(land)
        db.session.commit()

        result = service.analyze_property_structured(land)

        assert result["status"] == "success"
        assert result["model"] is None
