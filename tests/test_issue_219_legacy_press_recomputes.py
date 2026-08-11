"""Issue #219: a press on the legacy land page means recompute.

`generate_openai_structured` answered a press with the **stored** analysis
whenever the caller left `force` out — a finished run standing in for a new
one. That is the shape the owner ruled out for the universal path in #206:
joining applies to a run still in flight, never to a finished row.

Its only caller always sent `force: true`, so the branch was dead by caller,
not by contract — live again the moment a script or a hand-made request omitted
the flag. It is gone, and these tests are what keeps it gone.

`TestConcurrentPressDoesNotPayTwice` covers the other half of #206's contract:
removing the `force` shortcut must not weaken the *only* protection against
paying twice for the same land -- `background_jobs`' dedupe_key, which joins a
press to a run still in flight instead of starting a second one.
tests/test_issue_190_review_blockers.py already proves this at the HTTP level
for the universal property route
(test_a_sync_request_is_refused_with_409_when_a_live_async_job_exists); the
legacy land route used the same `dedupe_key`/`run_job_sync`/`JobAlreadyActive`
machinery but had no equivalent proof at the route level.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import create_app, db
from models import AiAnalysisVariant, BackgroundJob, Land
from tests import setup_test_environment

FRESH = {"price_analysis": {"verdict": "UNDERPRICED", "summary": "Fresh run."}}
STORED = {"price_analysis": {"verdict": "OVERPRICED", "summary": "Stored earlier."}}


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


@pytest.fixture
def land_with_stored_analysis(app):
    land = Land(
        source_email_id="issue-219", title="A plot", url="https://x/inmueble/1/"
    )
    db.session.add(land)
    db.session.commit()
    db.session.add(
        AiAnalysisVariant(
            land_id=land.id,
            provider="openai",
            model="stored-model",
            analysis=STORED,
        )
    )
    db.session.commit()
    return land


def _fresh_service():
    """A service whose analysis is distinguishable from the stored one."""

    class _Service:
        def analyze_property_structured(self, land):
            return {
                "status": "success",
                "structured_analysis": FRESH,
                "model": "fresh-model",
            }

    return _Service()


class TestAPressRecomputes:
    @pytest.mark.parametrize("body", [{}, {"force": False}, None])
    def test_a_press_without_force_still_runs_the_analysis(
        self, app, client, land_with_stored_analysis, body
    ):
        land = land_with_stored_analysis

        with patch(
            "services.openai_service.get_openai_service", return_value=_fresh_service()
        ):
            response = client.post(
                f"/api/analysis/generate/{land.id}/openai?sync=1",
                data=json.dumps(body) if body is not None else None,
                content_type="application/json",
            )

        payload = response.get_json()
        assert payload["success"] is True
        assert payload["analysis"] == FRESH, (
            "the stored analysis answered a press that asked for a new one"
        )
        assert payload["model"] == "fresh-model"
        assert "already exists" not in json.dumps(payload)

    def test_the_stored_variant_is_replaced_not_duplicated(
        self, app, client, land_with_stored_analysis
    ):
        land = land_with_stored_analysis

        with patch(
            "services.openai_service.get_openai_service", return_value=_fresh_service()
        ):
            client.post(f"/api/analysis/generate/{land.id}/openai?sync=1")

        variants = AiAnalysisVariant.query.filter_by(
            land_id=land.id, provider="openai"
        ).all()
        assert len(variants) == 1, "migration 017 keeps one row per (land, provider)"
        assert variants[0].analysis == FRESH
        assert variants[0].model == "fresh-model"


class TestConcurrentPressDoesNotPayTwice:
    """A press must always join a run already in flight for the same land --
    the one case #206 keeps a second press from starting a second one. That
    guarantee lives in `background_jobs`' dedupe_key
    (`land_openai_analysis:{id}:openai`), untouched by deleting the `force`
    shortcut; this pins it so it stays that way."""

    def test_a_second_press_joins_the_live_job_instead_of_paying_again(
        self, app, client, land_with_stored_analysis
    ):
        land = land_with_stored_analysis

        live_job_id = "7" * 32
        db.session.add(
            BackgroundJob(
                id=live_job_id,
                job_type="land_openai_analysis",
                status="running",
                dedupe_key=f"land_openai_analysis:{land.id}:openai",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        db.session.commit()

        with patch("services.openai_service.get_openai_service") as mock_get_service:
            response = client.post(f"/api/analysis/generate/{land.id}/openai")

        assert response.status_code == 409, (
            "a press while a run is already in flight must join it, not start "
            "a second, paid one"
        )
        body = response.get_json()
        assert body["success"] is False
        assert body["job_id"] == live_job_id
        mock_get_service.assert_not_called()


def test_the_short_circuit_is_gone_from_the_route():
    """A caller-side flag is not a contract; the branch itself must not return."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "routes" / "api_routes.py"
    text = source.read_text(encoding="utf-8")

    assert "ChatGPT analysis already exists" not in text
    assert (
        '"force"'
        not in text.split("def generate_openai_structured")[1].split("\ndef ")[0]
    ), "the flag that guarded the short circuit outlived it"
