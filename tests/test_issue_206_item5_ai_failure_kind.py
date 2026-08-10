"""#206 item 5: a retryable AI-bridge outcome must not read like a genuine one.

Before this, `tools/ai_bridge.py` already answered 503 (every run slot busy),
504 (ran out of time) and 502 (the CLI genuinely failed) -- distinct on
purpose, per its own comment. Nothing downstream read the distinction:
`SubscriptionTransportError` folded `exc.code` into an unstructured message,
and `services/property_ai_service.py` (`_analyze_openai`/`_analyze_claude`)
and `services/openai_service.py` all collapsed every one of the three into
the same `{"status": "failed", "error": "AI analysis service is temporarily
unavailable"}` (or, for `openai_service.py`, let the exception propagate
raw). "The bridge is busy, try again shortly" and "the CLI is broken" looked
identical to the user, though only one of them means something is actually
wrong.

This file pins: `SubscriptionTransportError.status` carries the bridge's
HTTP status; `subscription_transport.describe_failure()` maps it to a
`(failure_kind, message)` pair that is distinct for 503 vs 504 vs everything
else; both analysis services surface that as a `failure_kind` field instead
of raising or repeating the same string; and the two API routes that call
them forward `failure_kind` into the job/response the frontend reads.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from config import Config
from services import subscription_transport
from services.subscription_transport import SubscriptionTransportError

REPO_ROOT = Path(__file__).resolve().parent.parent
PROPERTY_DETAIL_TEMPLATE = REPO_ROOT / "templates" / "property_detail.html"
LAND_DETAIL_TEMPLATE = REPO_ROOT / "templates" / "land_detail.html"
MAIN_JS = REPO_ROOT / "static" / "js" / "main.js"


class TestSubscriptionTransportErrorCarriesStatus:
    def test_default_status_is_none(self):
        exc = SubscriptionTransportError("bridge unreachable")
        assert exc.status is None

    def test_status_can_be_set(self):
        exc = SubscriptionTransportError("bridge returned 503: busy", status=503)
        assert exc.status == 503


class TestDescribeFailureMapping:
    def test_503_is_bridge_busy_and_retryable(self):
        kind, message = subscription_transport.describe_failure(
            SubscriptionTransportError("bridge returned 503: busy", status=503)
        )
        assert kind == "bridge_busy"
        assert "busy" in message.lower() or "retry" in message.lower()

    def test_504_is_timeout_and_retryable(self):
        kind, message = subscription_transport.describe_failure(
            SubscriptionTransportError("bridge returned 504: timed out", status=504)
        )
        assert kind == "timeout"
        assert "budget" in message.lower() or "time" in message.lower()

    def test_502_is_a_genuine_failure(self):
        kind, _message = subscription_transport.describe_failure(
            SubscriptionTransportError("bridge returned 502: cli crashed", status=502)
        )
        assert kind == "failed"

    def test_no_status_is_a_genuine_failure(self):
        """`URLError` (bridge unreachable) and config errors carry no status."""
        kind, _message = subscription_transport.describe_failure(
            SubscriptionTransportError("bridge unreachable at http://x: refused")
        )
        assert kind == "failed"

    def test_503_and_504_and_other_are_three_distinct_messages(self):
        """The defect this closes, stated directly: before the fix all three
        produced the exact same string."""
        busy = subscription_transport.describe_failure(
            SubscriptionTransportError("x", status=503)
        )
        timeout = subscription_transport.describe_failure(
            SubscriptionTransportError("x", status=504)
        )
        failed = subscription_transport.describe_failure(
            SubscriptionTransportError("x", status=502)
        )
        messages = {busy[1], timeout[1], failed[1]}
        kinds = {busy[0], timeout[0], failed[0]}
        assert len(messages) == 3, "503/504/other must not share a message"
        assert len(kinds) == 3, "503/504/other must not share a failure_kind"


class TestPropertyAIServiceMapsFailureKind:
    """`_analyze_openai` and `_analyze_claude` in property_ai_service.py."""

    def _service(self):
        from services.property_ai_service import PropertyAIService

        return PropertyAIService()

    @pytest.mark.parametrize(
        "method_name,provider",
        [("_analyze_openai", "openai"), ("_analyze_claude", "claude")],
    )
    @pytest.mark.parametrize(
        "status,expected_kind",
        [(503, "bridge_busy"), (504, "timeout"), (502, "failed"), (None, "failed")],
    )
    def test_status_maps_to_failure_kind(
        self, monkeypatch, method_name, provider, status, expected_kind
    ):
        def fake_complete(*_args, **_kwargs):
            raise SubscriptionTransportError(f"bridge returned {status}", status=status)

        monkeypatch.setattr(subscription_transport, "complete", fake_complete)
        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = self._service()
            result = getattr(service, method_name)("prompt text")

        assert result["status"] == "failed"
        assert result["failure_kind"] == expected_kind
        assert result["error"]

    def test_bridge_busy_and_timeout_read_differently(self, monkeypatch):
        """The concrete UI-facing bug: before the fix, a busy bridge and a
        timed-out run were the same string."""

        def make_fake(status):
            def fake_complete(*_args, **_kwargs):
                raise SubscriptionTransportError("x", status=status)

            return fake_complete

        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = self._service()

            monkeypatch.setattr(subscription_transport, "complete", make_fake(503))
            busy_result = service._analyze_claude("prompt")

            monkeypatch.setattr(subscription_transport, "complete", make_fake(504))
            timeout_result = service._analyze_claude("prompt")

        assert busy_result["error"] != timeout_result["error"]
        assert busy_result["failure_kind"] != timeout_result["failure_kind"]

    def test_missing_bridge_token_is_a_genuine_failure(self):
        with patch.object(Config, "AI_BRIDGE_TOKEN", ""):
            service = self._service()
            result = service._analyze_claude("prompt")
        assert result["status"] == "failed"
        assert result["failure_kind"] == "failed"


class TestOpenAIServiceMapsFailureKind:
    """`analyze_property_structured` in services/openai_service.py used to
    let `SubscriptionTransportError` propagate uncaught -- the caller's job
    runner recorded `str(exc)` (the raw "bridge returned 503: ..." wording)
    as the job's error. It must now return the same failed/failure_kind
    shape as property_ai_service.py instead of raising."""

    def _land(self):
        from types import SimpleNamespace

        return SimpleNamespace(
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

    @pytest.mark.parametrize(
        "status,expected_kind",
        [(503, "bridge_busy"), (504, "timeout"), (502, "failed")],
    )
    def test_transport_error_becomes_a_failed_result_not_an_exception(
        self, monkeypatch, status, expected_kind
    ):
        from services.openai_service import OpenAIService

        def fake_complete(*_args, **_kwargs):
            raise SubscriptionTransportError(f"bridge returned {status}", status=status)

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

        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            service = OpenAIService()
            # Must not raise -- that is the regression this test would catch.
            result = service.analyze_property_structured(self._land())

        assert result["status"] == "failed"
        assert result["failure_kind"] == expected_kind
        assert result["error"]


class TestApiRoutesForwardFailureKind:
    """The two `/api/.../analyze/...` routes must not drop `failure_kind` on
    the way from the service result into the job/response body the frontend
    reads (`result.failure_kind` in property_detail.html / land_detail.html)."""

    @pytest.fixture
    def app(self):
        from tests import setup_test_environment

        setup_test_environment()
        from app import create_app, db

        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    @patch("services.property_ai_service.PropertyAIService.analyze_property_structured")
    def test_universal_property_route_forwards_failure_kind(
        self, mock_analyze, client, app
    ):
        from app import db
        from models import Property

        with app.app_context():
            prop = Property(
                source_email_id="failkind_prop_1",
                title="Test Property",
                municipality="Alicante",
            )
            db.session.add(prop)
            db.session.commit()
            prop_id = prop.id

        mock_analyze.return_value = {
            "status": "failed",
            "error": "The AI bridge is busy running another analysis. Try again shortly.",
            "failure_kind": "bridge_busy",
        }

        resp = client.post(
            f"/api/property/{prop_id}/analyze/structured",
            json={"provider": "claude"},
        )
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False
        assert data["failure_kind"] == "bridge_busy"

    @patch("services.openai_service.OpenAIService.analyze_property_structured")
    def test_land_openai_route_forwards_failure_kind(self, mock_analyze, client, app):
        from app import db
        from models import Land

        with app.app_context():
            land = Land(
                source_email_id="failkind_land_1",
                title="Test Land",
                municipality="Alicante",
                land_type="developed",
            )
            db.session.add(land)
            db.session.commit()
            land_id = land.id

        mock_analyze.return_value = {
            "status": "failed",
            "error": "The analysis did not finish within its time budget. Try again.",
            "failure_kind": "timeout",
        }

        # get_openai_service()'s singleton constructs OpenAIService(), which
        # requires AI_BRIDGE_TOKEN to be set (it fails closed otherwise) --
        # unrelated to the failure_kind plumbing this test targets.
        with patch.object(Config, "AI_BRIDGE_TOKEN", "test-bridge-token"):
            resp = client.post(f"/api/analysis/generate/{land_id}/openai")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False
        assert data["failure_kind"] == "timeout"


class TestSpinnerCopyIsStateDrivenNotADurationPromise:
    """#195's "This may take 1-5 minutes" was honest at a 4m50s run; #202/#214
    made real runs 19-41s, and a single fast run does not license a new upper
    bound either. Both detail pages must report state (queued/running)
    instead of a duration -- see test_price_change_on_detail_pages.py and
    test_tablet_list_layout.py for the established pattern of pinning
    template/JS copy directly."""

    def test_property_detail_drops_the_duration_promise(self):
        source = PROPERTY_DETAIL_TEMPLATE.read_text(encoding="utf-8")
        assert "1-5 minutes" not in source
        assert (
            'id="ai-claude-status-text"' in source or "ai-claude-status-text" in source
        )
        assert "ai-chatgpt-status-text" in source

    def test_land_detail_drops_the_duration_promise(self):
        source = LAND_DETAIL_TEMPLATE.read_text(encoding="utf-8")
        assert "1-5 minutes" not in source
        assert "ai-claude-status-text" in source
        assert "ai-chatgpt-status-text" in source

    def test_no_template_still_promises_a_duration(self):
        """Same guard as the price-info one in test_price_change_on_detail_
        pages.py -- a duration promise anywhere else would be the same
        regression under a different filename."""
        for template in (REPO_ROOT / "templates").rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            assert "1-5 minutes" not in source, template.name

    def test_main_js_reports_job_status_not_a_duration(self):
        source = MAIN_JS.read_text(encoding="utf-8")
        assert "describeJobStatus" in source
        assert "queued" in source.lower()
        assert "running" in source.lower()

    def test_detail_pages_wire_the_status_text_from_the_job(self):
        """The copy is only honestly state-driven if the pages actually read
        pollJob's per-poll job object, not just if the strings exist."""
        property_source = PROPERTY_DETAIL_TEMPLATE.read_text(encoding="utf-8")
        land_source = LAND_DETAIL_TEMPLATE.read_text(encoding="utf-8")
        for source in (property_source, land_source):
            assert "describeJobStatus" in source
            assert "onUpdate" in source
