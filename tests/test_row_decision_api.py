"""The list row's decision buttons and the one endpoint behind them (#630).

The endpoint is not a second writer: it calls `owner_review.set_review`. What
these tests pin is the boundary (closed set of values, 400/404, CSRF), that a
press never erases the reason or action typed elsewhere, that `null` is
`undecided` and not `rejected`, and that the list renders the control once per
row in both views with its endpoint and strings once per page.
"""

import re
from datetime import date

import pytest

from app import create_app, db
from models import Property, PropertyActivity, SearchProfile
from services import owner_review
from tests import setup_test_environment

ROWS = 25


def _make_app(csrf):
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = csrf
    application.config["RATELIMIT_ENABLED"] = False
    return application


@pytest.fixture
def app():
    application = _make_app(csrf=False)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile(app):
    row = SearchProfile(name="Asturias", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _prop(profile, slug, **kwargs):
    row = Property(
        source_email_id=slug,
        title=f"Plot {slug}",
        municipality="Castrillón",
        search_profile_id=profile.id,
        **kwargs,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _post(client, prop_id, body):
    return client.post(f"/api/property/{prop_id}/review", json=body)


def _verdict_events(prop_id):
    return PropertyActivity.query.filter_by(property_id=prop_id, kind="verdict").count()


class TestTheEndpoint:
    @pytest.mark.parametrize("decision", ["interested", "waiting", "rejected"])
    def test_each_decision_is_stored_and_reported(self, app, client, profile, decision):
        row = _prop(profile, f"d-{decision}")
        response = _post(client, row.id, {"decision": decision})
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["state"] == decision
        assert body["changed"] is True
        assert body["hidden"] is (decision == "rejected")
        db.session.expire_all()
        assert db.session.get(Property, row.id).owner_verdict == decision
        assert _verdict_events(row.id) == 1

    def test_null_clears_to_undecided_and_never_to_rejected(self, app, client, profile):
        row = _prop(profile, "clear", owner_verdict="rejected")
        body = _post(client, row.id, {"decision": None}).get_json()
        assert body["state"] == "undecided"
        assert body["changed"] is True
        assert body["hidden"] is False
        db.session.expire_all()
        assert db.session.get(Property, row.id).owner_verdict is None

    def test_a_repeat_writes_no_second_event(self, app, client, profile):
        row = _prop(profile, "repeat")
        assert _post(client, row.id, {"decision": "waiting"}).get_json()["changed"]
        again = _post(client, row.id, {"decision": "waiting"}).get_json()
        assert again["changed"] is False
        assert _verdict_events(row.id) == 1

    def test_clearing_an_undecided_row_writes_nothing(self, app, client, profile):
        row = _prop(profile, "nothing")
        assert _post(client, row.id, {"decision": None}).get_json()["changed"] is False
        assert _verdict_events(row.id) == 0

    def test_a_typed_reason_and_action_survive_a_press(self, app, client, profile):
        row = _prop(profile, "preserved")
        owner_review.set_review(
            row,
            decision="waiting",
            reason="needs a survey",
            action="call the notary",
            due_on=date(2026, 9, 20),
        )
        _post(client, row.id, {"decision": "rejected"})
        _post(client, row.id, {"decision": None})
        db.session.expire_all()
        stored = db.session.get(Property, row.id)
        assert stored.owner_verdict is None
        assert stored.owner_verdict_reason == "needs a survey"
        assert stored.next_action == "call the notary"
        assert stored.next_action_due_on == date(2026, 9, 20)

    def test_a_rejected_row_with_an_open_action_is_not_reported_hidden(
        self, app, client, profile
    ):
        row = _prop(profile, "action", next_action="call")
        assert (
            _post(client, row.id, {"decision": "rejected"}).get_json()["hidden"]
            is False
        )

    @pytest.mark.parametrize(
        "body",
        [
            {"decision": "maybe"},
            {"decision": ""},
            {"decision": 1},
            {"decision": "REJECTED"},
            {},
            [],
            "rejected",
        ],
    )
    def test_anything_else_is_a_400_and_writes_nothing(
        self, app, client, profile, body
    ):
        row = _prop(profile, "bad")
        response = _post(client, row.id, body)
        assert response.status_code == 400
        assert response.get_json()["success"] is False
        db.session.expire_all()
        assert db.session.get(Property, row.id).owner_verdict is None
        assert _verdict_events(row.id) == 0

    def test_a_non_json_body_is_a_400(self, app, client, profile):
        row = _prop(profile, "form")
        response = client.post(
            f"/api/property/{row.id}/review", data={"decision": "rejected"}
        )
        assert response.status_code == 400
        db.session.expire_all()
        assert db.session.get(Property, row.id).owner_verdict is None

    def test_an_unknown_id_is_a_404(self, app, client):
        assert _post(client, 999999, {"decision": "waiting"}).status_code == 404

    def test_an_internal_failure_does_not_leak(self, app, client, profile, monkeypatch):
        row = _prop(profile, "boom")

        def explode(*args, **kwargs):
            raise RuntimeError("secret internals /srv/db")

        monkeypatch.setattr(owner_review, "set_review", explode)
        response = _post(client, row.id, {"decision": "waiting"})
        assert response.status_code == 500
        assert "secret" not in response.get_data(as_text=True)


class TestCsrf:
    def test_a_post_without_a_token_is_refused_and_writes_nothing(self):
        application = _make_app(csrf=True)
        with application.app_context():
            db.create_all()
            try:
                profile = SearchProfile(name="Asturias", is_active=True)
                db.session.add(profile)
                db.session.commit()
                row = _prop(profile, "csrf")
                response = application.test_client().post(
                    f"/api/property/{row.id}/review", json={"decision": "rejected"}
                )
                assert response.status_code == 400
                db.session.expire_all()
                assert db.session.get(Property, row.id).owner_verdict is None
            finally:
                db.session.remove()
                db.drop_all()


def _pressed(html, prop_id):
    block = re.search(
        rf'data-owner-decision data-property-id="{prop_id}">(.*?)data-owner-decision-status',
        html,
        re.S,
    )
    assert block, f"no control for {prop_id}"
    return re.findall(
        r'data-decision="(\w+)"\s+aria-pressed="(true|false)"', block.group(1)
    )


class TestTheList:
    @pytest.mark.parametrize("view_type", ["list", "cards"])
    def test_one_control_per_row_with_the_stored_state_pressed(
        self, app, client, profile, view_type
    ):
        rows = [_prop(profile, f"r{i}") for i in range(3)]
        owner_review.set_review(rows[0], decision="interested")
        owner_review.set_review(rows[1], decision="waiting")
        response = client.get(f"/properties?view_type={view_type}&verdict=all")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert html.count("data-owner-decision data-property-id=") == 3
        assert _pressed(html, rows[0].id) == [
            ("interested", "true"),
            ("waiting", "false"),
            ("rejected", "false"),
        ]
        assert [s for s, p in _pressed(html, rows[1].id) if p == "true"] == ["waiting"]
        assert [s for s, p in _pressed(html, rows[2].id) if p == "true"] == []
        assert html.count("data-owner-decision-status") == 3
        assert html.count('role="status" data-owner-decision-status') == 3

    @pytest.mark.parametrize("view_type", ["list", "cards"])
    def test_the_endpoint_and_strings_are_on_the_page_once(
        self, app, client, profile, view_type
    ):
        for i in range(4):
            _prop(profile, f"c{i}")
        html = client.get(f"/properties?view_type={view_type}").get_data(as_text=True)
        assert html.count('id="owner-decision-config"') == 1
        assert html.count('data-url-template="/api/property/0/review"') == 1
        assert html.count("js/owner-decision.js") == 1
        assert html.count("data-saved=") >= 1
        assert html.count("Decision cleared") == 1

    def test_the_page_renders_with_no_decisions_at_all(self, app, client, profile):
        _prop(profile, "plain")
        for view_type in ("list", "cards"):
            response = client.get(f"/properties?view_type={view_type}")
            assert response.status_code == 200
            assert "data-owner-decision data-property-id=" in response.get_data(
                as_text=True
            )

    def test_spanish_labels_are_rendered(self, app, client, profile):
        _prop(profile, "es")
        with client.session_transaction() as session:
            session["language"] = "es"
        html = client.get("/properties?view_type=cards").get_data(as_text=True)
        assert "Me encaja" in html

    def test_the_html_cost_of_the_control_for_a_25_row_page(self, app, client, profile):
        for i in range(ROWS):
            _prop(profile, f"s{i}")
        html = client.get("/properties?view_type=list").get_data(as_text=True)
        blocks = re.findall(
            r'<div class="owner-decision-buttons".*?data-owner-decision-status></span>\s*</div>',
            html,
            re.S,
        )
        assert len(blocks) == ROWS
        config = re.search(r'<div id="owner-decision-config".*?</div>', html, re.S)
        delta = sum(len(b) for b in blocks) + len(config.group(0))
        print(f"owner-decision HTML delta for {ROWS} rows: {delta} bytes")
        # Per-row markup only; strings and the URL are not repeated.
        assert delta / ROWS < 1100
