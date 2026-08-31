"""The taste ranking made findable, and its retrain loop (#498 follow-up).

The owner's complaint of 2026-08-31 was not that the mechanism was missing —
316 rows were scored — but that nothing SHOWED it: the comment field lived
in a collapsed block and the heart mode was one unmarked icon. So: the
comment card is in the open on the detail page and its compact save cannot
erase the outstanding action; the taste chip rides beside the score in
EVERY display mode; the retrain button is a CSRF-protected singleton job
that retrains AND re-scores; and the daily auto-score is capped, visible-
scope only, and dormant without a profile.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from services import taste_service
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


_SEQ = iter(range(1, 10_000))


def _profile(name="Galicia · costa", **overrides):
    values = dict(name=name, is_active=True)
    values.update(overrides)
    row = SearchProfile(**values)
    db.session.add(row)
    db.session.commit()
    return row


def _mk(profile_id, **overrides):
    values = dict(
        source_email_id=f"tste:{next(_SEQ)}",
        title=f"Listing {next(_SEQ)}",
        price=100000,
        area=250,
        search_profile_id=profile_id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _ledger():
    row = TasteProfile(
        provider="claude",
        signals_fingerprint="a" * 64,
        source={
            "signals": [{"property_id": 1, "verdict": "interested", "reason": "x"}]
        },
        profile={
            "likes": [
                {
                    "trait": "sea",
                    "weight": 1,
                    "evidence": "x",
                    "evidence_property_ids": [1],
                }
            ],
            "dislikes": [],
            "dealbreakers": [],
            "summary_ru": "x",
        },
    )
    db.session.add(row)
    db.session.commit()
    return row.id


def _score(prop, version, score):
    prop.taste = {
        "status": "ok",
        "score": score,
        "reasons_ru": ["ok"],
        "profile_version": version,
        "scorer_version": taste_service.TASTE_SCORER_VERSION,
        "facts_fingerprint": taste_service.facts_fingerprint(
            taste_service.gather_facts(prop)
        ),
    }
    prop.taste_score = score
    db.session.commit()


class TestTheChipRidesEveryMode:
    def test_combined_mode_shows_the_taste_chip(self, client, app):
        profile = _profile()
        version = _ledger()
        prop = _mk(profile.id, title="Chipped row")
        _score(prop, version, 87.0)
        html = client.get("/properties").data.decode()
        assert "&hearts; 87" in html or "♥ 87" in html

    def test_no_profile_no_chip(self, client, app):
        profile = _profile()
        _mk(profile.id, title="Plain row")
        html = client.get("/properties").data.decode()
        assert "&hearts;" not in html


class TestTheCommentCard:
    def test_the_card_is_in_the_open_with_a_textarea(self, client, app):
        profile = _profile()
        prop = _mk(profile.id)
        html = client.get(f"/properties/{prop.id}").data.decode()
        assert "owner-comment-card" in html
        assert 'id="owner-comment-reason"' in html
        # In the open — NOT inside the collapsed details block: the card
        # must appear before the <details> that buried the original form.
        assert html.index("owner-comment-card") < html.index('id="owner-review"')

    def test_the_compact_save_does_not_erase_the_outstanding_action(self, client, app):
        """The round-1 plan-gate finding, pinned at the UI: set_review
        writes all four fields, so the compact card carries the current
        action as hidden inputs — posting only a verdict+reason must leave
        the action standing."""
        from services import owner_review

        profile = _profile()
        prop = _mk(profile.id)
        owner_review.set_review(
            prop,
            decision="waiting",
            reason="жду ответа",
            action="позвонить архитектору",
        )
        page = client.get(f"/properties/{prop.id}").data.decode()
        assert 'name="next_action" value="позвонить архитектору"' in page

        response = client.post(
            f"/properties/{prop.id}/review",
            data={
                "verdict": "interested",
                "reason": "нравится участок",
                "next_action": "позвонить архитектору",
                "due_on": "",
            },
        )
        assert response.status_code == 302
        assert prop.owner_verdict == "interested"
        assert prop.owner_verdict_reason == "нравится участок"
        assert prop.next_action == "позвонить архитектору"


class TestRetrain:
    def test_the_button_is_a_singleton_job_behind_csrf(self, client, app):
        _profile()
        _ledger()
        captured = {}

        def _fake_enqueue(fn, *, job_type, meta=None, app=None, dedupe_key=None):
            captured["job_type"] = job_type
            captured["dedupe_key"] = dedupe_key
            return "job-1234567890"

        with patch("services.background_jobs.enqueue_job", side_effect=_fake_enqueue):
            response = client.post("/properties/taste/retrain")
        assert response.status_code == 302
        assert captured == {
            "job_type": "taste_retrain",
            "dedupe_key": "taste_retrain",
        }

    def test_the_button_renders_only_with_a_profile(self, client, app):
        _profile()
        html = client.get("/properties").data.decode()
        assert "retrain" not in html.lower()
        _ledger()
        html = client.get("/properties").data.decode()
        assert "Retrain taste" in html


class TestRescorePending:
    def test_scope_is_visible_rows_the_reader_does_not_call_ok(self, app):
        version = _ledger()
        visible = _profile("Visible")
        hidden = _profile("Hidden", is_hidden=True)
        pending = _mk(visible.id)
        current = _mk(visible.id)
        _score(current, version, 50.0)
        off_screen = _mk(hidden.id)

        seen = []

        def _fake_batch(batch, profile, provider="claude", commit=True, **kw):
            seen.extend(p.id for p in batch)
            return {
                "status": "ok",
                "rows": {p.id: "scored" for p in batch},
                "bridge_called": True,
            }

        with patch.object(taste_service, "score_batch", side_effect=_fake_batch):
            outcome = taste_service.rescore_pending()
        assert outcome["status"] == "ok"
        assert pending.id in seen
        assert current.id not in seen, "an ok row must not be re-bought"
        assert off_screen.id not in seen, "a hidden subscription spends nothing"

    def test_the_cap_bounds_the_calls(self, app):
        _ledger()
        profile = _profile()
        for _ in range(30):
            _mk(profile.id)
        calls = []

        def _fake_batch(batch, *a, **kw):
            calls.append(len(batch))
            return {
                "status": "ok",
                "rows": {p.id: "scored" for p in batch},
                "bridge_called": True,
            }

        with patch.object(taste_service, "score_batch", side_effect=_fake_batch):
            outcome = taste_service.rescore_pending(cap_calls=2)
        assert len(calls) == 2
        assert outcome["calls"] == 2
        assert outcome["pending_left"] > 0

    def test_three_refusals_stop_and_no_profile_is_a_named_no_op(self, app):
        _ledger()
        profile = _profile()
        for _ in range(40):
            _mk(profile.id)

        def _refuse(batch, *a, **kw):
            return {
                "status": "failed",
                "error": "down",
                "bridge_called": True,
            }

        with patch.object(taste_service, "score_batch", side_effect=_refuse):
            outcome = taste_service.rescore_pending()
        assert outcome["calls"] == 3

        TasteProfile.query.delete()
        db.session.commit()
        assert taste_service.rescore_pending()["status"] == "failed"


class TestTheDailyJob:
    def test_the_job_body_reports_and_reraises(self, app):
        from services import scheduler_service

        scheduler_service.flask_app = app
        _profile()
        _ledger()
        with patch.object(
            taste_service,
            "rescore_pending",
            return_value={"status": "ok", "scored": 0, "calls": 0, "pending_left": 0},
        ) as run:
            scheduler_service.run_scheduled_taste_scoring()
        run.assert_called_once()
        (_, kwargs) = run.call_args
        assert kwargs["cap_calls"] == 10

        with patch.object(
            taste_service, "rescore_pending", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError):
                scheduler_service.run_scheduled_taste_scoring()

    def test_the_flag_is_fail_closed_everywhere_it_is_decided(self):
        """The #376 pattern: config default false, compose says false, and
        the dev compose stays silent (its silence resolves to the config
        default)."""
        import re
        from pathlib import Path

        root = Path(__file__).parent.parent
        config_src = (root / "config.py").read_text()
        assert re.search(r'AUTO_TASTE_SCORING",\s*"false"', config_src), (
            "config.py must default the taste scheduler off"
        )
        compose = (root / "docker-compose.yml").read_text()
        assert "AUTO_TASTE_SCORING=${AUTO_TASTE_SCORING:-false}" in compose
        dev = (root / "docker-compose.dev.yml").read_text()
        assert "AUTO_TASTE_SCORING" not in dev
