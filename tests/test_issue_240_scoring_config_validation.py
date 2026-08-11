"""Issue #240: one unusable value took a whole subscription's scoring with it.

`scoring_config` is hand-edited JSON on a subscription. A weight typed as a
string reached `float(v)` inside a dict comprehension, the exception travelled
up to the caller's blanket `except`, and every listing under that profile was
left with no score — reported nowhere. The owner sees a saved config and
unchanged listings.

Two halves: the scorer skips an override it cannot use and keeps the default
for that key (the treatment `_resolve_sea_distance_config` already gave its own
section), and the save refuses a config with a non-numeric weight in the first
place, naming the key.
"""

import itertools
import json
from decimal import Decimal

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.property_scoring_service import PropertyScoringService
from tests import setup_test_environment


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


_PROFILE_SEQ = itertools.count(1)


def _profile(scoring_config=None):
    profile = SearchProfile(
        name=f"Houses in Asturias {next(_PROFILE_SEQ)}",
        is_active=True,
        is_default=False,
        travel_targets={"presets": {}, "custom": []},
        scoring_config=scoring_config,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _property(profile):
    prop = Property(
        source_email_id=f"issue-240-{profile.id}",
        title="Land plot in Siero",
        property_category="land",
        search_profile_id=profile.id,
        price=Decimal("70000.00"),
        area=Decimal("1200.00"),
        municipality="Siero",
    )
    db.session.add(prop)
    db.session.commit()
    return prop


class TestOneBadOverrideDoesNotStopTheScoring:
    """`float("high")` raised out of the weight comprehension. The caller's
    blanket `except` turned that into "no score" for every listing under the
    profile — so the scorer is driven directly here, where the exception was."""

    def _score(self, prop, profile):
        return PropertyScoringService().scorer_for(prop).calculate(prop, profile)

    @pytest.mark.parametrize(
        "config",
        [
            {"categories": {"land": {"investment": {"price": "high", "size": 0.4}}}},
            {"categories": {"land": {"lifestyle": {"sea": "close"}}}},
            {"categories": {"land": {"travel_minutes": {"best": "soon", "worst": 90}}}},
            {
                "categories": {
                    "land": {"combined_mix": {"investment": "most", "lifestyle": 0.5}}
                }
            },
        ],
    )
    def test_an_unusable_override_falls_back_to_the_default(self, app, config):
        baseline_profile = _profile()
        baseline_prop = _property(baseline_profile)
        baseline = self._score(baseline_prop, baseline_profile)

        broken_profile = _profile(config)
        broken_prop = _property(broken_profile)

        result = self._score(broken_prop, broken_profile)

        assert (result.investment, result.lifestyle, result.combined) == (
            baseline.investment,
            baseline.lifestyle,
            baseline.combined,
        ), "an unusable override must read as no override, not as no score"

    def test_a_usable_override_still_applies(self, app):
        """The skip must not become "ignore the config": the applied mix is
        recorded in the breakdown, so it is observable without a peer set."""
        baseline_profile = _profile()
        baseline = self._score(_property(baseline_profile), baseline_profile)

        tuned_profile = _profile(
            {
                "categories": {
                    "land": {"combined_mix": {"investment": 1, "lifestyle": 0}}
                }
            }
        )
        tuned = self._score(_property(tuned_profile), tuned_profile)

        assert tuned.scoring_payload["combined_mix"] == {
            "investment": 1.0,
            "lifestyle": 0.0,
        }
        assert (
            tuned.scoring_payload["combined_mix"]
            != baseline.scoring_payload["combined_mix"]
        )

    def test_an_unusable_mix_keeps_the_default_mix(self, app):
        baseline_profile = _profile()
        baseline = self._score(_property(baseline_profile), baseline_profile)

        broken_profile = _profile(
            {
                "categories": {
                    "land": {"combined_mix": {"investment": "most", "lifestyle": 0.5}}
                }
            }
        )
        broken = self._score(_property(broken_profile), broken_profile)

        assert (
            broken.scoring_payload["combined_mix"]
            == baseline.scoring_payload["combined_mix"]
        )

    def test_the_property_path_survives_it_too(self, app):
        """The route calls this one, and it is where the blanket except lives."""
        profile = _profile({"categories": {"land": {"investment": {"price": "high"}}}})
        prop = _property(profile)

        assert PropertyScoringService().calculate_for_property(prop, commit=True)
        assert isinstance(prop.scoring, dict) and prop.scoring.get("details"), (
            "the run produced no breakdown at all, which is what a raised "
            "ValueError looked like from outside"
        )


class TestTheSaveRefusesAConfigTheScorerCannotUse:
    def _save(self, client, profile, config):
        return client.post(
            f"/profiles/{profile.id}/edit",
            data={
                "action": "save_scoring_config",
                "scoring_config_json": json.dumps(config),
            },
            follow_redirects=True,
        ).get_data(as_text=True)

    def test_it_names_the_offending_key(self, app, client):
        profile = _profile()

        body = self._save(
            client,
            profile,
            {"categories": {"land": {"investment": {"price": "high"}}}},
        )

        assert "categories.land.investment.price" in body
        assert "not saved" in body.lower()
        assert profile.scoring_config is None, "a config the scorer cannot use"

    def test_a_valid_config_is_saved(self, app, client):
        profile = _profile()

        body = self._save(
            client,
            profile,
            {"categories": {"land": {"investment": {"price": 0.5}}}},
        )

        assert "Scoring config saved" in body
        assert (
            profile.scoring_config["categories"]["land"]["investment"]["price"] == 0.5
        )

    def test_a_null_is_not_a_bad_number(self, app, client):
        """`None` means "no override", which every reader already handles."""
        profile = _profile()

        body = self._save(
            client,
            profile,
            {"categories": {"land": {"investment": {"price": None}}}},
        )

        assert "Scoring config saved" in body
