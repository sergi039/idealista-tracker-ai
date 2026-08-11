"""Issue #239, option 2: the weights that score /properties get a real page.

`/criteria` configures the legacy land scorer, which is why #250 made it say
so. The weights that actually score the listings on `/properties` live in each
subscription's `scoring_config`, and until now the only way to change them was
a raw JSON textarea — which is why a page for a different scorer looked like
the place to do it.

The form replaces that textarea on the subscription page. It is built from
`PropertyScoringService`'s own vocabulary and defaults, so it cannot drift from
the scoring it configures; an empty field means "no override" rather than a
copy of today's default; and anything stored that the form does not manage is
carried across untouched and named on the page instead of vanishing.
"""

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


@pytest.fixture
def profile(app):
    profile = SearchProfile(
        name="Houses in Asturias",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()
    for index in range(2):
        db.session.add(
            Property(
                source_email_id=f"issue-239-{index}",
                title="Land plot in Siero",
                property_category="land",
                search_profile_id=profile.id,
            )
        )
    db.session.commit()
    return profile


def _save(client, profile, fields):
    data = {"action": "save_scoring_weights"}
    data.update(fields)
    return client.post(
        f"/profiles/{profile.id}/edit", data=data, follow_redirects=True
    ).get_data(as_text=True)


class TestTheDefaultsComeFromTheScorer:
    def test_defaults_for_reads_the_scorer_not_a_copy(self, app):
        from services.property_scoring_service import LandPropertyScorer

        defaults = PropertyScoringService().defaults_for("land")

        assert defaults["investment"] == LandPropertyScorer.DEFAULT_INVESTMENT_WEIGHTS
        assert defaults["lifestyle"] == LandPropertyScorer.DEFAULT_LIFESTYLE_WEIGHTS
        assert defaults["sea_distance"] == LandPropertyScorer.DEFAULT_SEA_DISTANCE

    def test_an_unknown_category_falls_back_rather_than_raising(self, app):
        assert PropertyScoringService().defaults_for("chateau")["combined_mix"]

    def test_every_category_with_a_scorer_is_offered(self, app):
        categories = PropertyScoringService().known_categories()

        assert "land" in categories and "housing" in categories


class TestThePage:
    def test_it_renders_a_field_per_weight_with_the_default_as_placeholder(
        self, client, profile
    ):
        body = client.get(f"/profiles/{profile.id}/edit").get_data(as_text=True)

        assert 'name="scoring__land__investment__value_score"' in body
        assert 'name="scoring__land__combined_mix__investment"' in body
        assert 'name="scoring__land__sea_distance__far_m"' in body
        assert 'placeholder="0.7"' in body, "the scorer's own default, shown"

    def test_the_raw_json_editor_is_gone(self, client, profile):
        """One control for one thing: the textarea is what made the weights
        look like something only /criteria could reach."""
        body = client.get(f"/profiles/{profile.id}/edit").get_data(as_text=True)

        assert "scoring_config_json" not in body
        assert "save_scoring_config" not in body

    def test_a_category_the_subscription_holds_is_shown_first(self, client, profile):
        body = client.get(f"/profiles/{profile.id}/edit").get_data(as_text=True)

        assert body.index("Land") < body.index("Commercial")
        assert "2 listings" in body


class TestSaving:
    def test_a_saved_weight_reaches_the_scorer(self, client, profile):
        _save(
            client,
            profile,
            {
                "scoring__land__combined_mix__investment": "1",
                "scoring__land__combined_mix__lifestyle": "0",
            },
        )

        prop = Property.query.filter_by(search_profile_id=profile.id).first()
        result = PropertyScoringService().scorer_for(prop).calculate(prop, profile)

        assert result.scoring_payload["combined_mix"] == {
            "investment": 1.0,
            "lifestyle": 0.0,
        }

    def test_saving_rescores_the_subscription(self, client, profile):
        body = _save(client, profile, {"scoring__land__investment__value_score": "0.9"})

        assert "2 listings in this subscription rescored" in body

    def test_a_comma_decimal_is_accepted(self, client, profile):
        """The owner's keyboard is Spanish; 0,5 is a number."""
        _save(client, profile, {"scoring__land__investment__value_score": "0,5"})

        assert (
            profile.scoring_config["categories"]["land"]["investment"]["value_score"]
            == 0.5
        )

    def test_it_keeps_what_it_does_not_manage(self, client, profile):
        profile.scoring_config = {
            "categories": {
                "land": {
                    "investment": {"value_score": 0.5},
                    "future_section": {"x": 1},
                },
                "chateau": {"investment": {"value_score": 0.2}},
            },
            "note": "hand written",
        }
        db.session.commit()

        _save(client, profile, {"scoring__land__investment__value_score": "0.6"})

        stored = profile.scoring_config
        assert stored["categories"]["land"]["investment"]["value_score"] == 0.6
        assert stored["categories"]["land"]["future_section"] == {"x": 1}
        assert stored["categories"]["chateau"] == {"investment": {"value_score": 0.2}}
        assert stored["note"] == "hand written"

    def test_what_it_does_not_manage_is_named_on_the_page(self, client, profile):
        profile.scoring_config = {"categories": {"chateau": {"investment": {}}}}
        db.session.commit()

        body = client.get(f"/profiles/{profile.id}/edit").get_data(as_text=True)

        assert "unmanaged-scoring-keys" in body
        assert "categories.chateau" in body

    def test_clearing_every_field_clears_the_override(self, client, profile):
        profile.scoring_config = {
            "categories": {"land": {"investment": {"value_score": 0.5}}}
        }
        db.session.commit()

        _save(client, profile, {"scoring__land__investment__value_score": ""})

        assert profile.scoring_config is None


class TestTheCombinedMixIsAPair:
    """#255: the scorer applies `combined_mix` only when both halves are there.
    A form that saves one of them stores something that looks set and does
    nothing."""

    def test_half_a_mix_is_refused_naming_both_fields(self, client, profile):
        body = _save(
            client, profile, {"scoring__land__combined_mix__investment": "0.9"}
        )

        assert "not saved" in body.lower()
        assert "both investment and lifestyle" in body
        assert profile.scoring_config is None, "a value the scorer would step over"

    def test_the_other_half_alone_is_refused_too(self, client, profile):
        body = _save(client, profile, {"scoring__land__combined_mix__lifestyle": "0.1"})

        assert "not saved" in body.lower()
        assert profile.scoring_config is None

    def test_both_halves_are_applied(self, client, profile):
        _save(
            client,
            profile,
            {
                "scoring__land__combined_mix__investment": "0.4",
                "scoring__land__combined_mix__lifestyle": "0.6",
            },
        )

        assert profile.scoring_config["categories"]["land"]["combined_mix"] == {
            "investment": 0.4,
            "lifestyle": 0.6,
        }

    def test_neither_half_leaves_the_default_alone(self, client, profile):
        body = _save(
            client,
            profile,
            {
                "scoring__land__combined_mix__investment": "",
                "scoring__land__combined_mix__lifestyle": "",
                "scoring__land__investment__value_score": "0.5",
            },
        )

        assert "Scoring saved" in body
        assert "combined_mix" not in profile.scoring_config["categories"]["land"]


class TestTheSaveIsOneTransaction:
    """#256: the config used to commit before the rescore, so a failure inside
    the loop left the weights stored, nothing rescored, and a 500 that told the
    owner the opposite."""

    def test_a_failing_rescore_leaves_the_stored_config_alone(
        self, client, profile, monkeypatch
    ):
        from services import property_scoring_service as module

        def explode(self, prop, commit=False):
            raise RuntimeError("scoring blew up")

        monkeypatch.setattr(
            module.PropertyScoringService, "calculate_for_property", explode
        )

        with pytest.raises(RuntimeError):
            _save(client, profile, {"scoring__land__investment__value_score": "0.9"})

        db.session.rollback()
        assert profile.scoring_config is None, (
            "the weights were committed before the work that failed"
        )

    def test_the_rescore_sees_the_config_being_saved(self, client, profile):
        """It reads the staged value through the session, not a committed one."""
        seen = []
        from services import property_scoring_service as module

        original = module.PropertyScoringService.calculate_for_property

        def record(self, prop, commit=False):
            seen.append(
                (prop.search_profile.scoring_config or {})
                .get("categories", {})
                .get("land", {})
                .get("investment", {})
                .get("value_score")
            )
            return original(self, prop, commit=commit)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            module.PropertyScoringService, "calculate_for_property", record
        )
        try:
            _save(client, profile, {"scoring__land__investment__value_score": "0.9"})
        finally:
            monkeypatch.undo()

        assert seen and set(seen) == {0.9}
