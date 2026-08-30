"""The owner's taste: profile ledger, batch scoring, honest refusals (#498).

What this file pins, in the order the money moves: the profile is an
insert-only ledger whose failed builds insert nothing; a scoring batch is
validated strictly (an answer that misses, duplicates or invents a listing is
rejected whole); a bridge refusal writes nothing on any row; a slow answer
cannot overwrite a newer score or wear a changed row's fingerprint; a row
with nothing to judge costs no bridge call; and none of it can reach a billed
Google API — `utils.google_spend.billed_get` is patched to explode, and every
flow here runs with it armed.
"""

import json
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from services import subscription_transport, taste_service
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def no_billed_google():
    """Every taste flow in this file runs with the Google door booby-trapped."""

    def _explode(*args, **kwargs):  # pragma: no cover - firing IS the failure
        raise AssertionError("taste code reached the billed Google door")

    with patch("utils.google_spend.billed_get", side_effect=_explode):
        yield


@pytest.fixture
def profile_row(app):
    row = SearchProfile(name="Galicia · costa", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


_SEQ = iter(range(1, 10_000))


def _mk_property(profile_row, **overrides):
    values = dict(
        source_email_id=f"taste-test:{next(_SEQ)}",
        title="Casa en Malpica",
        price=290000,
        area=300,
        municipality="Malpica de Bergantiños",
        search_profile_id=profile_row.id,
        description="Casa con finca en la costa.",
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _seed_reference(profile_row, verdict="interested", reason="Нравится: вид на море."):
    prop = _mk_property(profile_row, title="Reference")
    prop.owner_verdict = verdict
    prop.owner_verdict_reason = reason
    db.session.commit()
    return prop


def _bridge_answer(payload):
    return {"text": json.dumps(payload), "model": "claude-test"}


def _profile_payload(reference_ids):
    return {
        "likes": [
            {
                "trait": "sea view",
                "weight": 0.9,
                "evidence": "the owner says so",
                "evidence_property_ids": list(reference_ids),
            }
        ],
        "dislikes": [],
        "dealbreakers": [],
        "summary_ru": "Вам нравится море.",
    }


def _score_result(pid, score=83.0, closest=None):
    return {
        "property_id": pid,
        "score": score,
        "reasons_ru": ["Похоже на эталон."],
        "matched_likes": ["sea view"],
        "matched_dislikes": [],
        "closest_reference_id": closest,
        "confidence": "medium",
    }


class TestProfileLedger:
    def test_build_inserts_a_versioned_row_and_the_newest_wins(self, app, profile_row):
        ref = _seed_reference(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer(_profile_payload([ref.id])),
        ):
            first = taste_service.build_profile()
            second = taste_service.build_profile()
        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert second["data"]["version"] > first["data"]["version"]
        current = taste_service.load_current_profile()
        assert current["version"] == second["data"]["version"]
        assert current["source"]["signals"][0]["property_id"] == ref.id
        # Two positive examples cannot establish aversions; the profile says
        # it is provisional where every reader will see it.
        assert current["source"]["provisional"] is True

    def test_a_failed_build_inserts_nothing(self, app, profile_row):
        ref = _seed_reference(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer(_profile_payload([ref.id])),
        ):
            taste_service.build_profile()
        before = taste_service.current_profile_version()

        with patch.object(
            subscription_transport,
            "complete",
            side_effect=subscription_transport.SubscriptionTransportError("down"),
        ):
            outcome = taste_service.build_profile()
        assert outcome["status"] == "failed"
        assert taste_service.current_profile_version() == before

    def test_a_trait_citing_an_uninvited_listing_rejects_the_build(
        self, app, profile_row
    ):
        ref = _seed_reference(profile_row)
        bad = _profile_payload([ref.id + 999])
        with patch.object(
            subscription_transport, "complete", return_value=_bridge_answer(bad)
        ):
            outcome = taste_service.build_profile()
        assert outcome["status"] == "failed"
        assert "outside the signal set" in outcome["error"]
        assert taste_service.current_profile_version() is None

    def test_signals_exclude_waiting_and_reasonless_verdicts(self, app, profile_row):
        _seed_reference(profile_row, verdict="waiting", reason="ещё думаю")
        silent = _mk_property(profile_row, title="No reason")
        silent.owner_verdict = "interested"
        db.session.commit()
        signals = taste_service.collect_signals()
        usable = [s for s in signals if s["usable"]]
        assert usable == []
        # The reasonless verdict is named, not silently dropped.
        assert [s["property_id"] for s in signals if not s["usable"]] == [silent.id]

    def test_a_malformed_profile_row_reads_as_no_profile(self, app):
        db.session.add(
            TasteProfile(
                provider="claude",
                signals_fingerprint="x" * 64,
                source={"signals": []},
                profile={"likes": []},
            )
        )
        db.session.commit()
        assert taste_service.load_current_profile() is None


class TestScoringABatch:
    def _built_profile(self, profile_row):
        ref = _seed_reference(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer(_profile_payload([ref.id])),
        ):
            taste_service.build_profile()
        return ref, taste_service.load_current_profile()

    def test_a_scored_row_carries_the_number_and_its_provenance(self, app, profile_row):
        ref, profile = self._built_profile(profile_row)
        prop = _mk_property(profile_row)
        answer = {"results": [_score_result(prop.id, score=83.0, closest=ref.id)]}
        with patch.object(
            subscription_transport, "complete", return_value=_bridge_answer(answer)
        ):
            outcome = taste_service.score_batch([prop], profile)
        assert outcome["status"] == "ok"
        assert outcome["rows"] == {prop.id: "scored"}
        assert float(prop.taste_score) == 83.0
        assert prop.taste["score"] == 83.0
        assert prop.taste["profile_version"] == profile["version"]
        assert prop.taste["scorer_version"] == taste_service.TASTE_SCORER_VERSION
        assert prop.taste["closest_reference_id"] == ref.id
        assert prop.taste["reasons_ru"] == ["Похоже на эталон."]

    @pytest.mark.parametrize(
        "mangle, why",
        [
            (lambda r, pid: [], "no result for requested"),
            (lambda r, pid: r + r, "duplicate result"),
            (
                lambda r, pid: r + [_score_result(pid + 999)],
                "uninvited",
            ),
            (
                lambda r, pid: [dict(r[0], score=140)],
                "score is not a number in 0..100",
            ),
            (
                lambda r, pid: [dict(r[0], closest_reference_id=424242)],
                "is not a reference",
            ),
            (
                lambda r, pid: [dict(r[0], reasons_ru=[])],
                "reasons_ru is empty",
            ),
        ],
    )
    def test_a_mangled_answer_rejects_the_whole_call_and_writes_nothing(
        self, app, profile_row, mangle, why
    ):
        ref, profile = self._built_profile(profile_row)
        prop = _mk_property(profile_row)
        results = [_score_result(prop.id)]
        answer = {"results": mangle(results, prop.id)}
        with patch.object(
            subscription_transport, "complete", return_value=_bridge_answer(answer)
        ):
            outcome = taste_service.score_batch([prop], profile)
        assert outcome["status"] == "failed"
        assert why in outcome["error"]
        assert prop.taste_score is None
        assert prop.taste is None

    def test_a_refusal_writes_nothing_and_keeps_a_previous_score(
        self, app, profile_row
    ):
        ref, profile = self._built_profile(profile_row)
        prop = _mk_property(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer({"results": [_score_result(prop.id, 60.0)]}),
        ):
            taste_service.score_batch([prop], profile)
        kept = dict(prop.taste)

        with patch.object(
            subscription_transport,
            "complete",
            side_effect=subscription_transport.SubscriptionTransportError("down"),
        ):
            outcome = taste_service.score_batch([prop], profile)
        assert outcome["status"] == "failed"
        assert float(prop.taste_score) == 60.0
        assert prop.taste == kept

    def test_a_same_version_answer_does_not_overwrite_a_settled_score(
        self, app, profile_row
    ):
        """The codex reproduction: v7 score 90 lands, a late v7 call answers
        10 — the 90 must survive, and only --force may replace it."""
        ref, profile = self._built_profile(profile_row)
        prop = _mk_property(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer({"results": [_score_result(prop.id, 90.0)]}),
        ):
            taste_service.score_batch([prop], profile)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer({"results": [_score_result(prop.id, 10.0)]}),
        ):
            outcome = taste_service.score_batch([prop], profile)
        assert outcome["rows"] == {prop.id: "superseded"}
        assert float(prop.taste_score) == 90.0

        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer({"results": [_score_result(prop.id, 10.0)]}),
        ):
            outcome = taste_service.score_batch([prop], profile, overwrite_current=True)
        assert outcome["rows"] == {prop.id: "scored"}
        assert float(prop.taste_score) == 10.0

    def test_the_outcome_says_whether_the_bridge_was_asked(self, app, profile_row):
        ref, profile = self._built_profile(profile_row)
        empty = _mk_property(
            profile_row, title="Bare", price=None, area=None, description=None
        )
        with patch.object(subscription_transport, "complete") as transport:
            outcome = taste_service.score_batch([empty], profile)
        transport.assert_not_called()
        assert outcome["bridge_called"] is False
        assert taste_service.score_batch([], profile)["bridge_called"] is False

        prop = _mk_property(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            side_effect=subscription_transport.SubscriptionTransportError("down"),
        ):
            outcome = taste_service.score_batch([prop], profile)
        assert outcome["bridge_called"] is True

    def test_a_build_over_an_edited_comment_publishes_nothing(self, app, profile_row):
        """The codex reproduction: the owner edits a reason mid-build and the
        published profile carries the old one. The build must refuse."""
        ref = _seed_reference(profile_row)

        def _answer_and_edit_the_reason(*args, **kwargs):
            ref.owner_verdict_reason = "СОВСЕМ ДРУГАЯ ПРИЧИНА"
            db.session.commit()
            return _bridge_answer(_profile_payload([ref.id]))

        with patch.object(
            subscription_transport, "complete", side_effect=_answer_and_edit_the_reason
        ):
            outcome = taste_service.build_profile()
        assert outcome["status"] == "failed"
        assert outcome.get("failure_kind") == "superseded"
        assert taste_service.current_profile_version() is None

    def test_a_slow_answer_cannot_overwrite_a_newer_profiles_score(
        self, app, profile_row
    ):
        ref, profile_v1 = self._built_profile(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer(_profile_payload([ref.id])),
        ):
            taste_service.build_profile()
        profile_v2 = taste_service.load_current_profile()
        prop = _mk_property(profile_row)
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer({"results": [_score_result(prop.id, 90.0)]}),
        ):
            taste_service.score_batch([prop], profile_v2)

        # The v1 call answers late; the v2 score must survive it.
        with patch.object(
            subscription_transport,
            "complete",
            return_value=_bridge_answer({"results": [_score_result(prop.id, 10.0)]}),
        ):
            outcome = taste_service.score_batch([prop], profile_v1)
        assert outcome["rows"] == {prop.id: "superseded"}
        assert float(prop.taste_score) == 90.0
        assert prop.taste["profile_version"] == profile_v2["version"]

    def test_facts_that_change_under_the_call_discard_the_answer(
        self, app, profile_row
    ):
        ref, profile = self._built_profile(profile_row)
        prop = _mk_property(profile_row)

        def _answer_and_move_the_price(*args, **kwargs):
            # A COMMITTED change, as another session's would be: the locked
            # refresh discards uncommitted in-memory edits, which is exactly
            # why an uncommitted one cannot stand in for this race.
            prop.price = 123456
            db.session.commit()
            return _bridge_answer({"results": [_score_result(prop.id, 70.0)]})

        with patch.object(
            subscription_transport, "complete", side_effect=_answer_and_move_the_price
        ):
            outcome = taste_service.score_batch([prop], profile)
        assert outcome["rows"] == {prop.id: "superseded"}
        assert prop.taste_score is None

    def test_a_row_with_nothing_to_judge_costs_no_bridge_call(self, app, profile_row):
        ref, profile = self._built_profile(profile_row)
        empty = _mk_property(
            profile_row, title="Bare", price=None, area=None, description=None
        )
        with patch.object(subscription_transport, "complete") as transport:
            outcome = taste_service.score_batch([empty], profile)
        transport.assert_not_called()
        assert outcome["rows"] == {empty.id: "insufficient_evidence"}
        assert empty.taste_score is None

    def test_no_profile_means_no_call_and_no_write(self, app, profile_row):
        prop = _mk_property(profile_row)
        with patch.object(subscription_transport, "complete") as transport:
            outcome = taste_service.score_batch([prop], None)
        transport.assert_not_called()
        assert outcome["status"] == "failed"
        assert outcome["failure_kind"] == "no_profile"


class TestReadingAndSorting:
    def _score_block(self, version, score=50.0, scorer=None, prop=None):
        block = {
            "status": "ok",
            "score": score,
            "reasons_ru": ["ok"],
            "profile_version": version,
            "scorer_version": scorer
            if scorer is not None
            else taste_service.TASTE_SCORER_VERSION,
        }
        if prop is not None:
            block["facts_fingerprint"] = taste_service.facts_fingerprint(
                taste_service.gather_facts(prop)
            )
        return block

    def test_read_taste_states(self, app, profile_row):
        prop = _mk_property(profile_row)
        assert taste_service.read_taste(prop, 3)["state"] == "none"

        prop.taste = self._score_block(3, score=0.0, prop=prop)
        prop.taste_score = 0.0
        verdict = taste_service.read_taste(prop, 3)
        # Zero is a measured answer, never an absence.
        assert verdict["state"] == "ok"
        assert verdict["score"] == 0.0

        prop.taste = self._score_block(2, prop=prop)
        assert taste_service.read_taste(prop, 3)["state"] == "stale"

        prop.taste = self._score_block(3, scorer=0, prop=prop)
        assert taste_service.read_taste(prop, 3)["state"] == "stale"

        # A block with no facts fingerprint (a hand write) cannot prove it is
        # about today's row.
        prop.taste = self._score_block(3)
        assert taste_service.read_taste(prop, 3)["state"] == "stale"

        prop.taste = {"status": "ok", "score": "high", "profile_version": 3}
        assert taste_service.read_taste(prop, 3)["state"] == "none"

    def test_facts_that_change_after_scoring_read_as_stale(self, app, profile_row):
        prop = _mk_property(profile_row)
        prop.taste = self._score_block(3, prop=prop)
        prop.taste_score = 50.0
        db.session.commit()
        assert taste_service.read_taste(prop, 3)["state"] == "ok"
        # The price moves after the score was taken — the codex reproduction:
        # 90 at 100k stayed "ok" at 999k.
        prop.price = 999999
        db.session.commit()
        assert taste_service.read_taste(prop, 3)["state"] == "stale"

    def test_a_score_with_an_empty_ledger_is_not_current(self, app, profile_row):
        prop = _mk_property(profile_row)
        prop.taste = self._score_block(1, prop=prop)
        prop.taste_score = 60.0
        db.session.commit()
        # No taste_profile row exists at all: the score describes a profile
        # this database does not know, and must not read as current.
        assert taste_service.read_taste(prop)["state"] == "stale"

    def test_sql_agrees_with_the_reader_about_an_old_scorer(self, app, profile_row):
        prop = _mk_property(profile_row)
        prop.taste = self._score_block(2, scorer=0, prop=prop)
        prop.taste_score = 70.0
        db.session.commit()
        counted = (
            db.session.query(Property)
            .filter(taste_service.scored_current_expression(Property, 2))
            .count()
        )
        assert counted == 0, (
            "an old-scorer score must not count as current in SQL while the "
            "reader calls it stale"
        )

    def test_stale_and_unscored_rows_sort_last_in_both_directions(
        self, app, profile_row
    ):
        current = _mk_property(profile_row, title="current")
        current.taste = self._score_block(2, score=40.0)
        current.taste_score = 40.0
        stale = _mk_property(profile_row, title="stale")
        stale.taste = self._score_block(1, score=99.0)
        stale.taste_score = 99.0
        _mk_property(profile_row, title="unscored")
        db.session.commit()

        expr = taste_service.sortable_score_expression(Property, 2)
        for direction in (expr.desc(), expr.asc()):
            rows = Property.query.order_by(direction.nullslast(), Property.id).all()
            assert rows[0].id == current.id, (
                "the stale 99 must never outrank the current 40"
            )

    def test_with_no_profile_nothing_ranks(self, app, profile_row):
        prop = _mk_property(profile_row)
        prop.taste = self._score_block(1, score=88.0)
        prop.taste_score = 88.0
        db.session.commit()
        expr = taste_service.sortable_score_expression(Property, None)
        row = db.session.query(expr).select_from(Property).first()
        assert row[0] is None
