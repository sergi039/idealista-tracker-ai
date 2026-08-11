"""Issue #241: a partial save said "saved" and dropped the rest in silence.

`edit_profile`'s `save_classification_rules` and `save_email_matchers` validate
each submitted entry independently, skip the ones that fail, and flashed an
unqualified success as long as **one** survived. Only a wholly invalid list was
reported at all.

For email matchers this is what decides which saved search an unrecognised
alert email is routed to, so a silently dropped pattern sends future mail to
another profile or the catch-all — with nothing said anywhere.

Neither path had a test.
"""

import json

import pytest

from app import create_app, db
from models import SearchProfile
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
    return profile


def _post(client, profile, action, field, payload):
    return client.post(
        f"/profiles/{profile.id}/edit",
        data={"action": action, field: json.dumps(payload)},
        follow_redirects=True,
    )


GOOD_RULES = [
    {"category": "land", "subtype": "plot", "pattern": "terreno", "priority": 10},
    {"category": "housing", "subtype": "house", "pattern": "casa", "priority": 5},
]


class TestClassificationRules:
    def test_a_dropped_rule_is_named_in_the_message(self, app, client, profile):
        body = _post(
            client,
            profile,
            "save_classification_rules",
            "classification_rules_json",
            GOOD_RULES + [{"category": "land", "pattern": "parcela"}],  # no subtype
        ).get_data(as_text=True)

        assert "Saved 2 of 3 rules" in body
        assert "dropped 1 invalid (#3)" in body
        assert len(profile.classification_rules) == 2

    def test_a_fully_valid_save_is_still_a_plain_success(self, app, client, profile):
        body = _post(
            client,
            profile,
            "save_classification_rules",
            "classification_rules_json",
            GOOD_RULES,
        ).get_data(as_text=True)

        assert "Rules saved" in body
        assert "dropped" not in body
        assert len(profile.classification_rules) == 2

    def test_a_wholly_invalid_save_still_refuses(self, app, client, profile):
        body = _post(
            client,
            profile,
            "save_classification_rules",
            "classification_rules_json",
            [{"category": "land"}],
        ).get_data(as_text=True)

        assert "No valid rules found" in body
        assert profile.classification_rules is None


class TestEmailMatchers:
    def test_an_unusable_regex_is_named(self, app, client, profile):
        body = _post(
            client,
            profile,
            "save_email_matchers",
            "email_matchers_json",
            ["asturias", "casa[", {"pattern": "gijon", "priority": 3}],
        ).get_data(as_text=True)

        assert "Saved 2 of 3 email matchers" in body
        assert "(#2)" in body, "the owner needs to know which entry vanished"
        assert profile.email_matchers == [
            "asturias",
            {"pattern": "gijon", "priority": 3},
        ]

    def test_an_empty_pattern_is_named(self, app, client, profile):
        body = _post(
            client,
            profile,
            "save_email_matchers",
            "email_matchers_json",
            ["asturias", {"pattern": "   ", "priority": 1}],
        ).get_data(as_text=True)

        assert "Saved 1 of 2 email matchers" in body
        assert "(#2)" in body

    def test_a_fully_valid_save_is_still_a_plain_success(self, app, client, profile):
        body = _post(
            client,
            profile,
            "save_email_matchers",
            "email_matchers_json",
            ["asturias", "gijon"],
        ).get_data(as_text=True)

        assert "Email matchers saved" in body
        assert "dropped" not in body
        assert profile.email_matchers == ["asturias", "gijon"]
