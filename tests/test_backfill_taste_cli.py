"""The taste backfill CLI: honest counters, honest scope (#498).

The stop counts bridge CALLS the bridge actually saw: a batch gated away
before the call proves nothing about the bridge and must neither count as a
call nor clear the refusal streak — the codex reproduction was two refusals,
one no-call batch, one more refusal, and no stop. And the scope is the
page's own reader: a row `read_taste` calls `ok` leaves it, a facts-stale
row comes back in.
"""

import contextlib
from argparse import Namespace

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from services import taste_service
from tests import setup_test_environment
from utils import backfill_taste


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


_SEQ = iter(range(1, 10_000))


def _mk_property(profile_id, **overrides):
    values = dict(
        source_email_id=f"taste-cli:{next(_SEQ)}",
        title=f"Row {next(_SEQ)}",
        price=100000,
        area=100,
        search_profile_id=profile_id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


def _ledger_row():
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


def test_the_scope_is_the_readers_answer(app):
    profile = SearchProfile(name="G", is_active=True)
    db.session.add(profile)
    db.session.commit()
    version = _ledger_row()

    current = _mk_property(profile.id, title="current")
    current.taste = {
        "status": "ok",
        "score": 50.0,
        "reasons_ru": ["ok"],
        "profile_version": version,
        "scorer_version": taste_service.TASTE_SCORER_VERSION,
        "facts_fingerprint": taste_service.facts_fingerprint(
            taste_service.gather_facts(current)
        ),
    }
    current.taste_score = 50.0

    facts_stale = _mk_property(profile.id, title="facts moved")
    facts_stale.taste = dict(current.taste, facts_fingerprint="not this row")
    facts_stale.taste_score = 50.0

    unscored = _mk_property(profile.id, title="unscored")
    db.session.commit()

    args = Namespace(ids=[], profiles=[profile.id], force=False, limit=0)
    in_scope = {p.id for p in backfill_taste._scope(args, version)}
    assert current.id not in in_scope, "an ok row must leave the scope"
    assert facts_stale.id in in_scope, "a facts-stale row must come back in"
    assert unscored.id in in_scope

    args_force = Namespace(ids=[], profiles=[profile.id], force=True, limit=0)
    assert current.id in {p.id for p in backfill_taste._scope(args_force, version)}


def test_only_calls_the_bridge_saw_count_toward_the_stop(app, capsys, monkeypatch):
    """Two real refusals, a no-call batch, a third refusal → stop at three,
    and the no-call batch neither counted as a call nor cleared the streak."""
    profile = SearchProfile(name="G", is_active=True)
    db.session.add(profile)
    db.session.commit()
    _ledger_row()
    for _ in range(6):
        _mk_property(profile.id)

    outcomes = iter(
        [
            {"status": "failed", "error": "down", "bridge_called": True},
            {"status": "failed", "error": "down", "bridge_called": True},
            {"status": "ok", "rows": {}, "bridge_called": False},
            {"status": "failed", "error": "down", "bridge_called": True},
            # Must never be reached: the stop fires on the previous one.
            {"status": "ok", "rows": {}, "bridge_called": True},
        ]
    )
    calls = []

    def _fake_score_batch(batch, *args, **kwargs):
        outcome = next(outcomes)
        calls.append(outcome["bridge_called"])
        return outcome

    monkeypatch.setattr(backfill_taste.taste_service, "score_batch", _fake_score_batch)
    monkeypatch.setattr(
        backfill_taste, "inflight", lambda *a, **k: contextlib.nullcontext()
    )
    monkeypatch.setattr(backfill_taste, "create_app", lambda: app)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_taste", "--profiles", str(profile.id), "--apply", "--batch", "1"],
    )

    backfill_taste.main()
    out = capsys.readouterr().out
    assert "Stopping: 3 failed bridge calls" in out
    assert len(calls) == 4, "the run must stop on the third REAL refusal"
    # 3 calls reached the bridge; the gated batch is not one of them.
    assert "3 bridge calls total" in out
    assert "3 failed calls" in out


@pytest.mark.parametrize(
    "argv_tail",
    [["--limit", "-5"], ["--max-refusals", "0"], ["--batch", "0"]],
)
def test_nonsense_arguments_are_refused(app, monkeypatch, argv_tail):
    monkeypatch.setattr("sys.argv", ["backfill_taste", "--profiles", "1", *argv_tail])
    with pytest.raises(SystemExit) as excinfo:
        backfill_taste.main()
    assert excinfo.value.code == 2
