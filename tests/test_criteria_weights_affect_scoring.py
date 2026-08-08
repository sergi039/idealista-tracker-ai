"""Regression coverage for criteria writes used by dual-profile scoring (#27)."""

import json
import os
from decimal import Decimal

import pytest

from app import create_app, db
from config import Config
from models import Land, ScoringCriteria
from services.scoring_service import ScoringService
from tests import setup_test_environment

API_ENDPOINT = "/api/criteria"
FORM_ENDPOINT = "/criteria/update"
UPDATED_WEIGHTS = {
    "infrastructure_basic": 0.0,
    "environment": 1.0,
}


@pytest.fixture
def app():
    """Create an app with an isolated in-memory database."""
    setup_test_environment()
    original_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    try:
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()
    finally:
        if original_db_url is not None:
            os.environ["DATABASE_URL"] = original_db_url
        else:
            os.environ.pop("DATABASE_URL", None)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def scored_land_id(app):
    """Persist a land whose infrastructure scores 100 and environment scores 0."""
    with app.app_context():
        land = Land(
            source_email_id="criteria_weight_scoring_test",
            title="Criteria weight scoring test",
            land_type="developed",
            infrastructure_basic={
                "electricity": True,
                "water": True,
                "internet": True,
                "gas": True,
            },
            environment={"orientation": "north"},
        )
        db.session.add(land)
        db.session.flush()
        ScoringService().calculate_score(land)
        db.session.commit()
        return land.id


def _update_criteria(client, endpoint):
    if endpoint == API_ENDPOINT:
        response = client.put(
            endpoint,
            data=json.dumps({"criteria": UPDATED_WEIGHTS}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.get_json()["success"] is True
        return

    response = client.post(
        endpoint,
        data={
            f"weight_{name}": str(weight) for name, weight in UPDATED_WEIGHTS.items()
        },
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert [category for category, _ in session.get("_flashes", [])] == ["success"]


def test_config_profiles_are_used_when_no_weights_were_written(app, scored_land_id):
    """An empty criteria table must retain the configured scoring defaults."""
    with app.app_context():
        assert ScoringCriteria.query.count() == 0

        service = ScoringService()
        assert service._load_profile_weights("investment") == pytest.approx(
            Config.SCORING_PROFILES["investment"]
        )
        assert service._load_profile_weights("lifestyle") == pytest.approx(
            Config.SCORING_PROFILES["lifestyle"]
        )

        land = db.session.get(Land, scored_land_id)
        assert float(service.calculate_score(land)) > 0


@pytest.mark.parametrize("endpoint", [API_ENDPOINT, FORM_ENDPOINT])
def test_endpoint_weights_change_subsequent_scoring(
    app, client, scored_land_id, endpoint
):
    """Both legacy writers must persist weights that both profiles consume."""
    with app.app_context():
        baseline = db.session.get(Land, scored_land_id).score_total
        assert baseline is not None
        assert baseline > 0

    _update_criteria(client, endpoint)

    with app.app_context():
        for profile in ("investment", "lifestyle"):
            rows = {
                row.criteria_name: float(row.weight)
                for row in ScoringCriteria.query.filter_by(profile=profile).all()
            }
            assert rows == pytest.approx(UPDATED_WEIGHTS)

        combined_criteria = ScoringCriteria.query.filter(
            ScoringCriteria.profile == "combined",
            ScoringCriteria.criteria_name.in_(UPDATED_WEIGHTS),
        ).all()
        assert combined_criteria == []

        db.session.expire_all()
        rescored = db.session.get(Land, scored_land_id)
        endpoint_score = rescored.score_total
        assert endpoint_score == Decimal("0.00")
        assert endpoint_score != baseline

        subsequent_score = ScoringService().calculate_score(rescored)
        assert subsequent_score == Decimal("0.0")


def test_legacy_combined_rows_are_a_scoring_fallback(app):
    """Existing pre-fix criteria remain effective until a profile is rewritten."""
    with app.app_context():
        db.session.add_all(
            [
                ScoringCriteria(
                    criteria_name=name,
                    profile="combined",
                    weight=Decimal(str(weight)),
                )
                for name, weight in UPDATED_WEIGHTS.items()
            ]
        )
        db.session.commit()

        service = ScoringService()
        for profile in ("investment", "lifestyle"):
            assert service._load_profile_weights(profile) == pytest.approx(
                UPDATED_WEIGHTS
            )
