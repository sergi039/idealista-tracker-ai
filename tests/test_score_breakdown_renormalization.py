"""The D8 breakdown's arithmetic must reconcile with the score badge.

`PropertyScoringService._weighted_average` drops None components and divides
by the sum of the *measured* weights. The card therefore shows effective
weights (w / Σ measured w), or its products would stop summing to the badge
exactly when a component is excluded — the case the "not measured — excluded"
wording exists for (Phase-1 diff review finding, 2026-08-13).
"""

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment

# Default housing investment weights with the sea lookup refused: the badge
# shows (80*0.6 + 60*0.25) / 0.85 = 74.1, and the card must agree.
SCORING = {
    "version": 1,
    "category": "housing",
    "profiles": {
        "investment": {
            "score": 74.11764705882354,
            "weights": {
                "value_score": 0.6,
                "travel_score": 0.25,
                "sea_score": 0.15,
                "size_score": 0.0,
            },
            "components": {
                "value_score": 80.0,
                "travel_score": 60.0,
                "sea_score": None,
                "size_score": 70.0,
            },
        },
    },
    "combined_mix": {"investment": 0.32, "lifestyle": 0.68},
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


def _add_property(app):
    with app.app_context():
        prop = Property(
            source_email_id="breakdown-fixture",
            title="BreakdownFixture",
            municipality="El Franco",
            location_lat=43.55,
            location_lon=-6.83,
            score_total=74.1,
            score_investment=74.1,
            score_lifestyle=70.0,
            scoring=SCORING,
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


def test_excluded_component_renormalizes_the_shown_weights(app, client):
    pid = _add_property(app)
    body = client.get(f"/properties/{pid}").get_data(as_text=True)

    # 0.6/0.85 → 0.71, 80 × (0.6/0.85) = 56.5; 0.25/0.85 → 0.29, → 17.6.
    # 56.5 + 17.6 ≈ 74.1 — the badge. Raw 0.60/0.25 would sum to 63.
    assert "× 0.71" in body
    assert "56.5" in body
    assert "× 0.29" in body
    assert "17.6" in body


def test_the_excluded_component_says_so(app, client):
    pid = _add_property(app)
    body = client.get(f"/properties/{pid}").get_data(as_text=True)

    assert "not measured — excluded" in body


def test_zero_weight_components_stay_hidden(app, client):
    pid = _add_property(app)
    body = client.get(f"/properties/{pid}").get_data(as_text=True)

    # size_score carries weight 0.0 — it must not render a row that would
    # suggest it contributes.
    assert "× 0.00" not in body
