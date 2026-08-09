"""Regression tests for #47: GET /api/criteria reports criteria, and only those.

`ScoringService.load_custom_weights()` selects every active `ScoringCriteria`
row in the legacy namespace (`profile='combined' OR profile IS NULL`). That
namespace holds two unrelated kinds of row and has no discriminator column:

1. genuine legacy criterion weights written by the pre-#27 implementation, and
2. the `investment`/`lifestyle` pair written by
   `POST /criteria/update_combined_mix` - the ratio between the two profile
   scores, owned by `_load_combined_mix()`, not a criterion.

Everything matched went into `self.weights` *and* into the MCDM normalization
total. The mix rows sum to ~1.0 on their own, so every genuine criterion weight
came out roughly halved, and two non-criteria were reported alongside them.

Scores were never affected - `calculate_score` reads `_load_profile_weights`,
never `self.weights`. The one production consumer is `get_current_weights()`,
served by `GET /api/criteria`, so these tests assert the API response rather
than the internal dict.

#48/#50 validated criterion names on the way *in*, which does not close this:
the mix rows are written on purpose by their own endpoint and are still
matched, and rows already in a database from before that fix are still there.
Both cases are covered below.
"""

from decimal import Decimal

import pytest

from app import create_app, db
from config import Config
from models import ScoringCriteria
from services.scoring_service import ScoringService, known_criteria_names
from tests import setup_test_environment

API_ENDPOINT = "/api/criteria"
MIX_ENDPOINT = "/criteria/update_combined_mix"

# The two names POST /criteria/update_combined_mix writes at profile='combined'.
# Note that `investment_yield` *is* a criterion; bare `investment` is not.
MIX_NAMES = ("investment", "lifestyle")

# A full criterion set that is deliberately not the Config defaults, so a
# response built from the config fallback cannot be mistaken for one built from
# these rows. It sums to 1.0, which makes the MCDM normalization an identity:
# the expected response is exactly what was seeded.
SEEDED_WEIGHTS = {
    "investment_yield": 0.05,
    "location_quality": 0.05,
    "transport": 0.30,
    "infrastructure_basic": 0.10,
    "infrastructure_extended": 0.10,
    "environment": 0.10,
    "physical_characteristics": 0.10,
    "services_quality": 0.10,
    "legal_status": 0.05,
    "development_potential": 0.05,
}

# Rows a database can already hold from before the write path validated names
# (#48): a typo of "transport", and "neighborhood", which has a scorer but has
# never been one of the weighted criteria.
LEGACY_UNKNOWN_WEIGHTS = {"transprot": 0.30, "neighborhood": 0.20}


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


def seed_rows(app, weights):
    """Write rows straight to the legacy namespace, as a deployment holds them.

    Same shape as tests/test_scoring_service.py's
    TestConfigWeightsNotMutated._combined_profile_rows(): genuine criterion
    weights at profile='combined'. Direct inserts are the point for the legacy
    rows - names update_weights() now rejects cannot be written any other way.
    """
    with app.app_context():
        db.session.add_all(
            [
                ScoringCriteria(
                    criteria_name=name,
                    profile="combined",
                    weight=Decimal(str(weight)),
                    active=True,
                )
                for name, weight in weights.items()
            ]
        )
        db.session.commit()


def set_combined_mix(app, client, investment, lifestyle):
    """Set the mix the supported way, through its own validated endpoint."""
    resp = client.post(
        MIX_ENDPOINT,
        data={
            "investment_weight": str(investment),
            "lifestyle_weight": str(lifestyle),
        },
    )
    assert resp.status_code == 302

    with app.app_context():
        written = {
            row.criteria_name: float(row.weight)
            for row in ScoringCriteria.query.filter(
                ScoringCriteria.criteria_name.in_(MIX_NAMES)
            ).all()
        }
    assert written == pytest.approx(
        {"investment": investment, "lifestyle": lifestyle}
    ), f"seeding the mix failed: {written}"


def get_criteria(client):
    resp = client.get(API_ENDPOINT)
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    return payload["criteria"]


def test_seed_data_tracks_the_real_criterion_set():
    """Guard: SEEDED_WEIGHTS must stay in step with config.py.

    If a criterion is added or renamed, fail here with a clear reason instead
    of quietly testing a stale set.
    """
    assert set(SEEDED_WEIGHTS) == known_criteria_names()
    assert sum(SEEDED_WEIGHTS.values()) == pytest.approx(1.0)
    assert SEEDED_WEIGHTS != Config.DEFAULT_SCORING_WEIGHTS
    assert not set(LEGACY_UNKNOWN_WEIGHTS) & known_criteria_names()


class TestGetCriteriaExcludesTheCombinedMix:
    """The mix rows are legitimate, still written today, and not criteria."""

    def test_mix_names_are_absent_from_the_response(self, app, client):
        seed_rows(app, SEEDED_WEIGHTS)
        set_combined_mix(app, client, 0.4, 0.6)

        criteria = get_criteria(client)

        for name in MIX_NAMES:
            assert name not in criteria, (
                f"GET /api/criteria reported the combined-mix row '{name}' "
                "as a scoring criterion"
            )
        assert set(criteria) == known_criteria_names()

    def test_genuine_weights_keep_their_values(self, app, client):
        """The halving the issue is actually about.

        Ten criteria summing to 1.0 plus a mix summing to 1.0 normalize against
        a total of 2.0, so every real weight is reported at half its value.
        """
        seed_rows(app, SEEDED_WEIGHTS)
        set_combined_mix(app, client, 0.4, 0.6)

        criteria = get_criteria(client)

        assert criteria == pytest.approx(SEEDED_WEIGHTS)

    def test_the_mix_itself_is_untouched(self, app, client):
        """Guard, not a regression test: passes before and after the fix.

        The read filter must not be mistaken for a reason to stop writing or
        reading those rows - _load_combined_mix() is their correct reader.
        """
        seed_rows(app, SEEDED_WEIGHTS)
        set_combined_mix(app, client, 0.4, 0.6)

        with app.app_context():
            mix = ScoringService()._load_combined_mix()

        assert mix["investment"] == pytest.approx(0.4)
        assert mix["lifestyle"] == pytest.approx(0.6)


class TestGetCriteriaIgnoresLegacyUnknownRows:
    """Validating the write path cannot retract rows already in a database."""

    def test_legacy_unknown_names_are_absent_from_the_response(self, app, client):
        seed_rows(app, SEEDED_WEIGHTS)
        seed_rows(app, LEGACY_UNKNOWN_WEIGHTS)

        criteria = get_criteria(client)

        for name in LEGACY_UNKNOWN_WEIGHTS:
            assert name not in criteria, (
                f"GET /api/criteria reported the legacy row '{name}' as a "
                "scoring criterion"
            )
        assert set(criteria) == known_criteria_names()

    def test_legacy_unknown_names_do_not_skew_the_genuine_weights(self, app, client):
        seed_rows(app, SEEDED_WEIGHTS)
        seed_rows(app, LEGACY_UNKNOWN_WEIGHTS)

        criteria = get_criteria(client)

        assert criteria == pytest.approx(SEEDED_WEIGHTS)


class TestGetCriteriaStillReadsTheDatabase:
    """Positive control, not a regression test: passes before and after.

    A filter that dropped everything would satisfy the tests above, so pin down
    that genuine rows are still read and still normalized.
    """

    def test_db_rows_are_reported_rather_than_the_config_defaults(self, app, client):
        seed_rows(app, SEEDED_WEIGHTS)

        criteria = get_criteria(client)

        assert criteria == pytest.approx(SEEDED_WEIGHTS)
        assert criteria != pytest.approx(dict(Config.DEFAULT_SCORING_WEIGHTS))

    def test_genuine_weights_are_still_normalized(self, app, client):
        """Double every seeded weight: the response must normalize back to 1.0.

        Doubling rather than halving keeps every value at two decimals -
        ScoringCriteria.weight is Numeric(3, 2), so 0.025 would be stored
        rounded and the assertion would be measuring that instead.
        """
        seed_rows(app, {name: w * 2 for name, w in SEEDED_WEIGHTS.items()})

        criteria = get_criteria(client)

        assert criteria == pytest.approx(SEEDED_WEIGHTS)
