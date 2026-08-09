"""Regression tests for #48: criterion names are validated on the way in.

`PUT /api/criteria` and `POST /criteria/update` used to validate weight
*values* and nothing else. Both write through `ScoringService.update_weights()`.
Before #27 remapped its logical `combined` target to the real scoring profiles,
that default selected the same namespace and the same two names that
`_load_combined_mix()` reads for the investment/lifestyle ratio. So a request of

    PUT /api/criteria {"criteria": {"investment": 0.9, "lifestyle": 0.1}}

wrote rows indistinguishable from the ones `POST /criteria/update_combined_mix`
writes, silently repointing the combined score of every land, and answered
"Criteria updated successfully and all lands rescored". Typos were just as
silent: `{"transprot": 0.3}` wrote a junk row and returned success.

The criterion set is the ten keys of `Config.DEFAULT_SCORING_WEIGHTS`. These
tests cover all three surfaces: the JSON API, the form route, and the shared
primitive itself, which must not trust its callers.
"""

import json
from decimal import Decimal

import pytest

from app import create_app, db
from config import Config
from models import ScoringCriteria
from services.scoring_service import ScoringService
from tests import setup_test_environment

API_ENDPOINT = "/api/criteria"
FORM_ENDPOINT = "/criteria/update"
MIX_ENDPOINT = "/criteria/update_combined_mix"

# A plausible typo of "transport" - the exact class of name that used to be
# written to the database and reported as a success.
UNKNOWN_CRITERION = "transprot"
KNOWN_CRITERION = "transport"


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


def combined_rows(app):
    """Every row in the legacy `combined` namespace, by criterion name."""
    with app.app_context():
        return {
            row.criteria_name: float(row.weight)
            for row in ScoringCriteria.query.filter_by(profile="combined").all()
        }


def profile_rows(app, profile):
    """Every criterion row in one real scoring profile."""
    with app.app_context():
        return {
            row.criteria_name: float(row.weight)
            for row in ScoringCriteria.query.filter_by(profile=profile).all()
        }


def load_mix(app):
    with app.app_context():
        return ScoringService()._load_combined_mix()


def flashes(client):
    """Flash messages left in the session by the last (unfollowed) redirect."""
    with client.session_transaction() as sess:
        return list(sess.get("_flashes", []))


def clear_flashes(client):
    """Drop pending flashes. Nothing renders them here, so they accumulate."""
    with client.session_transaction() as sess:
        sess.pop("_flashes", None)


def set_combined_mix(client, investment, lifestyle):
    """Set the mix the supported way, through its own validated endpoint."""
    resp = client.post(
        MIX_ENDPOINT,
        data={
            "investment_weight": str(investment),
            "lifestyle_weight": str(lifestyle),
        },
    )
    assert resp.status_code == 302
    assert [c for c, _ in flashes(client)] == ["success"], (
        f"seeding the mix failed: {flashes(client)}"
    )
    clear_flashes(client)


class TestApiRejectsUnknownCriteria:
    """PUT /api/criteria - the JSON boundary."""

    def test_unknown_criterion_name_is_rejected(self, app, client):
        resp = client.put(
            API_ENDPOINT,
            data=json.dumps({"criteria": {UNKNOWN_CRITERION: 0.3}}),
            content_type="application/json",
        )

        assert resp.status_code == 400
        payload = json.loads(resp.data)
        assert payload["success"] is False
        assert UNKNOWN_CRITERION in payload["error"]
        assert combined_rows(app) == {}, "a rejected name must not be written"

    def test_known_criterion_name_is_still_accepted(self, app, client):
        """Positive control: the validation must not reject real criteria."""
        resp = client.put(
            API_ENDPOINT,
            data=json.dumps({"criteria": {KNOWN_CRITERION: 0.3}}),
            content_type="application/json",
        )

        assert resp.status_code == 200
        assert json.loads(resp.data)["success"] is True
        for profile in ("investment", "lifestyle"):
            assert profile_rows(app, profile) == {KNOWN_CRITERION: pytest.approx(0.3)}
        assert combined_rows(app) == {}

    def test_put_cannot_repoint_the_combined_mix(self, app, client):
        """The damage this issue is actually about.

        `investment`/`lifestyle` are not criteria; they are the ratio between
        the two profile scores. A PUT that names them must not reach the rows
        `_load_combined_mix()` reads.
        """
        set_combined_mix(client, 0.4, 0.6)
        assert load_mix(app) == pytest.approx({"investment": 0.4, "lifestyle": 0.6})

        resp = client.put(
            API_ENDPOINT,
            data=json.dumps({"criteria": {"investment": 0.9, "lifestyle": 0.1}}),
            content_type="application/json",
        )

        # The mix is asserted before the status code on purpose: the damage is
        # the point, and it is what an unfixed build reports here.
        assert load_mix(app) == pytest.approx({"investment": 0.4, "lifestyle": 0.6}), (
            "PUT /api/criteria silently repointed the combined mix"
        )
        assert combined_rows(app) == pytest.approx(
            {"investment": 0.4, "lifestyle": 0.6}
        )
        assert resp.status_code == 400


class TestFormRouteRejectsUnknownCriteria:
    """POST /criteria/update - the form boundary, same hole, different door."""

    def test_unknown_criterion_name_is_rejected(self, app, client):
        resp = client.post(FORM_ENDPOINT, data={f"weight_{UNKNOWN_CRITERION}": "0.3"})

        assert resp.status_code == 302
        assert [c for c, _ in flashes(client)] == ["error"]
        assert combined_rows(app) == {}, "a rejected name must not be written"

    def test_known_criterion_name_is_still_accepted(self, app, client):
        """Positive control: the real form fields must keep working."""
        resp = client.post(FORM_ENDPOINT, data={f"weight_{KNOWN_CRITERION}": "0.3"})

        assert resp.status_code == 302
        assert [c for c, _ in flashes(client)] == ["success"]
        for profile in ("investment", "lifestyle"):
            assert profile_rows(app, profile) == {KNOWN_CRITERION: pytest.approx(0.3)}
        assert combined_rows(app) == {}

    def test_form_cannot_repoint_the_combined_mix(self, app, client):
        set_combined_mix(client, 0.4, 0.6)

        resp = client.post(
            FORM_ENDPOINT,
            data={"weight_investment": "0.9", "weight_lifestyle": "0.1"},
        )

        assert load_mix(app) == pytest.approx({"investment": 0.4, "lifestyle": 0.6}), (
            "POST /criteria/update silently repointed the combined mix"
        )
        assert resp.status_code == 302
        assert [c for c, _ in flashes(client)] == ["error"]


class TestUpdateWeightsRejectsUnknownCriteria:
    """ScoringService.update_weights() - the shared primitive."""

    def test_unknown_name_returns_false(self, app):
        with app.app_context():
            service = ScoringService()

            assert service.update_weights({UNKNOWN_CRITERION: 0.3}) is False

            assert ScoringCriteria.query.count() == 0
            assert UNKNOWN_CRITERION not in service.weights

    def test_mix_names_are_not_criteria(self, app):
        """`investment`/`lifestyle` are the mix, and must be refused here too."""
        with app.app_context():
            db.session.add_all(
                [
                    ScoringCriteria(
                        criteria_name="investment",
                        profile="combined",
                        weight=Decimal("0.40"),
                        active=True,
                    ),
                    ScoringCriteria(
                        criteria_name="lifestyle",
                        profile="combined",
                        weight=Decimal("0.60"),
                        active=True,
                    ),
                ]
            )
            db.session.commit()

            service = ScoringService()

            assert (
                service.update_weights({"investment": 0.9, "lifestyle": 0.1}) is False
            )

            assert service._load_combined_mix() == pytest.approx(
                {"investment": 0.4, "lifestyle": 0.6}
            )

    def test_one_unknown_name_rejects_the_whole_payload(self, app):
        """No partial writes: validation happens before anything is committed."""
        with app.app_context():
            service = ScoringService()

            assert (
                service.update_weights({KNOWN_CRITERION: 0.3, UNKNOWN_CRITERION: 0.2})
                is False
            )

            assert ScoringCriteria.query.count() == 0

    def test_known_names_are_still_written(self, app):
        """Positive control, for every profile the primitive accepts."""
        with app.app_context():
            service = ScoringService()

            for profile in ("combined", "investment", "lifestyle"):
                assert service.update_weights({KNOWN_CRITERION: 0.3}, profile=profile)

                target_profiles = (
                    ("investment", "lifestyle") if profile == "combined" else (profile,)
                )
                for target_profile in target_profiles:
                    row = ScoringCriteria.query.filter_by(
                        criteria_name=KNOWN_CRITERION, profile=target_profile
                    ).first()
                    assert row is not None
                    assert float(row.weight) == pytest.approx(0.3)

            assert ScoringCriteria.query.filter_by(profile="combined").count() == 0

    def test_every_default_criterion_is_accepted(self, app):
        """The known set is exactly Config.DEFAULT_SCORING_WEIGHTS, not a copy."""
        with app.app_context():
            service = ScoringService()

            assert service.update_weights(dict(Config.DEFAULT_SCORING_WEIGHTS))

            for profile in ("investment", "lifestyle"):
                written = {
                    row.criteria_name
                    for row in ScoringCriteria.query.filter_by(profile=profile).all()
                }
                assert written == set(Config.DEFAULT_SCORING_WEIGHTS)

            assert ScoringCriteria.query.filter_by(profile="combined").count() == 0
