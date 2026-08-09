"""Regression tests for POST /criteria/update_combined_mix.

The view used to re-import `db` locally (`from app import db`) *after* its
first `db.session` call. That made `db` a function-local name for the whole
function body, so every request raised UnboundLocalError on
`db.session.commit()` - caught by a blanket `except Exception`, logged, and
turned into a generic flash. The weights were never saved and nothing was
rescored, on every single call. Found by `ruff check --select F823`.

These tests drive the real endpoint and assert the rows actually land in the
database, so a re-introduced shadowing import (or a re-broadened `except`)
fails the suite instead of failing silently in production.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Land, ScoringCriteria
from tests import setup_test_environment

ENDPOINT = "/criteria/update_combined_mix"


@pytest.fixture
def app():
    """App bound to a private in-memory DB before db.init_app() runs.

    setup_test_environment() puts an in-memory DATABASE_URL in the environment,
    which is the only override create_app() reads: Flask-SQLAlchemy binds the
    engine inside init_app(), so assigning SQLALCHEMY_DATABASE_URI afterwards
    would do nothing.
    """
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
def test_land(app):
    """A single land so the rescore loop has something to iterate over."""
    with app.app_context():
        land = Land(
            source_email_id="combined_mix_test_1",
            title="Combined Mix Test Land",
            municipality="Valencia",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            description="Parcela urbana con vistas al mar",
        )
        db.session.add(land)
        db.session.commit()
        return land.id


def flashes(client):
    """Flash messages left in the session by the last (unfollowed) redirect."""
    with client.session_transaction() as sess:
        return list(sess.get("_flashes", []))


def combined_rows(app):
    with app.app_context():
        return {
            row.criteria_name: row
            for row in ScoringCriteria.query.filter_by(profile="combined").all()
        }


class TestUpdateCombinedMix:
    def test_weights_are_persisted_on_first_call(self, app, client):
        """The insert branch must commit: no rows exist yet."""
        assert combined_rows(app) == {}

        resp = client.post(
            ENDPOINT, data={"investment_weight": "0.4", "lifestyle_weight": "0.6"}
        )

        assert resp.status_code == 302
        assert [c for c, _ in flashes(client)] == ["success"], (
            f"expected a single success flash, got {flashes(client)}"
        )

        rows = combined_rows(app)
        assert set(rows) == {"investment", "lifestyle"}
        assert float(rows["investment"].weight) == pytest.approx(0.4)
        assert float(rows["lifestyle"].weight) == pytest.approx(0.6)
        assert rows["investment"].active
        assert rows["lifestyle"].active

    def test_weights_are_updated_on_repeat_call(self, app, client):
        """The update branch must commit too: rows already exist."""
        client.post(
            ENDPOINT, data={"investment_weight": "0.4", "lifestyle_weight": "0.6"}
        )
        resp = client.post(
            ENDPOINT, data={"investment_weight": "0.25", "lifestyle_weight": "0.75"}
        )

        assert resp.status_code == 302
        assert "error" not in [c for c, _ in flashes(client)]

        rows = combined_rows(app)
        assert len(rows) == 2, "repeat call must update rows, not duplicate them"
        assert float(rows["investment"].weight) == pytest.approx(0.25)
        assert float(rows["lifestyle"].weight) == pytest.approx(0.75)

    def test_lands_are_rescored(self, app, client, test_land):
        """The batched rescore after the commit must actually run."""
        resp = client.post(
            ENDPOINT, data={"investment_weight": "0.4", "lifestyle_weight": "0.6"}
        )

        assert resp.status_code == 302
        messages = [m for _, m in flashes(client)]
        assert any("1 properties rescored" in m for m in messages), (
            f"rescore loop did not run: {messages}"
        )

        with app.app_context():
            land = db.session.get(Land, test_land)
            assert land.score_total is not None

    def test_weights_must_sum_to_one(self, app, client):
        resp = client.post(
            ENDPOINT, data={"investment_weight": "0.5", "lifestyle_weight": "0.9"}
        )

        assert resp.status_code == 302
        assert [c for c, _ in flashes(client)] == ["error"]
        assert combined_rows(app) == {}, "invalid weights must not be written"

    def test_non_numeric_weights_are_rejected(self, app, client):
        resp = client.post(
            ENDPOINT, data={"investment_weight": "abc", "lifestyle_weight": "0.6"}
        )

        assert resp.status_code == 302
        assert [c for c, _ in flashes(client)] == ["error"]
        assert combined_rows(app) == {}

    def test_unexpected_error_is_not_swallowed(self, app, client, test_land):
        """A bug in the rescore path must surface, not become a flash message.

        This is the guard against the failure mode that hid the original
        UnboundLocalError for as long as it did.
        """
        with patch(
            "services.scoring_service.ScoringService",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                client.post(
                    ENDPOINT,
                    data={"investment_weight": "0.4", "lifestyle_weight": "0.6"},
                )
