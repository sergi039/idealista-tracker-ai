"""The rollback tool for a score rewrite, and the ways it must refuse.

The pool criterion was enabled in production data, not in code: three
subscriptions got `lifestyle.pool_score = 0.1` and every listing under them
was re-scored. `data/pool_weight_enable_snapshot.json` holds both halves —
the profiles' previous `scoring_config` and the score columns — and until now
nothing read that shape, so the rollback existed only as a paragraph.

What is pinned here is the safety, not the happy path: a dry run that writes
nothing, a backup taken before the overwrite and never replacing one, a
snapshot with one unusable value restoring nothing at all, an absent column
left alone instead of nulled, and the rows the snapshot cannot speak for being
named rather than quietly left inconsistent.
"""

import json
from decimal import Decimal

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from utils import restore_score_snapshot as tool  # noqa: E402
from utils import score_snapshot  # noqa: E402


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _profile(name, config=None):
    profile = SearchProfile(name=name, is_active=True)
    profile.scoring_config = config
    db.session.add(profile)
    db.session.commit()
    return profile


def _property(key, profile, total="50.00", lifestyle="40.00", enrichment=None):
    prop = Property(
        source_email_id=f"restore-{key}",
        title=f"Restore {key}",
        search_profile_id=profile.id,
    )
    prop.score_total = Decimal(total)
    prop.score_lifestyle = Decimal(lifestyle)
    prop.score_investment = Decimal("60.00")
    prop.scoring = {"version": 1, "marker": key}
    prop.enrichment = (
        enrichment if enrichment is not None else {"pool": {"status": "ok"}}
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _snapshot_file(tmp_path, profiles, rows, name="snap.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "created_at": "2026-08-14T17:48:43+00:00",
                "profiles": profiles,
                "scores": rows,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _row(prop_id, total, lifestyle, scoring=None):
    return {
        "id": prop_id,
        "score_total": total,
        "score_investment": "60.00",
        "score_lifestyle": lifestyle,
        "scoring": scoring if scoring is not None else {"version": 1, "marker": "old"},
    }


def test_dry_run_reports_and_writes_nothing(app, tmp_path):
    profile = _profile(
        "Default", {"categories": {"housing": {"lifestyle": {"pool_score": 0.1}}}}
    )
    prop = _property("a", profile)
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(prop.id, "63.37", "67.92")]
    )

    assert tool.run(["--snapshot", path]) == tool.EXIT_OK

    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("50.00")
    assert db.session.get(SearchProfile, profile.id).scoring_config is not None


def test_apply_restores_scores_and_the_weights_that_made_them(app, tmp_path):
    profile = _profile(
        "Default", {"categories": {"housing": {"lifestyle": {"pool_score": 0.1}}}}
    )
    prop = _property("a", profile)
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(prop.id, "63.37", "67.92")]
    )

    assert (
        tool.run(
            ["--snapshot", path, "--apply", "--backup", str(tmp_path / "before.json")]
        )
        == tool.EXIT_OK
    )

    db.session.expire_all()
    restored = db.session.get(Property, prop.id)
    assert restored.score_total == Decimal("63.37")
    assert restored.score_lifestyle == Decimal("67.92")
    assert restored.scoring == {"version": 1, "marker": "old"}
    assert db.session.get(SearchProfile, profile.id).scoring_config is None


def test_the_backup_holds_the_state_that_was_overwritten(app, tmp_path):
    profile = _profile("Default", {"categories": {"housing": {}}})
    prop = _property("a", profile)
    backup = tmp_path / "before.json"
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(prop.id, "63.37", "67.92")]
    )

    tool.run(["--snapshot", path, "--apply", "--backup", str(backup)])

    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert saved["profiles"][str(profile.id)] == {"categories": {"housing": {}}}
    assert saved["scores"][0]["score_total"] == "50.00"
    # And it is itself restorable: feeding it back undoes the restore.
    assert (
        tool.run(["--snapshot", str(backup), "--apply", "--no-backup"]) == tool.EXIT_OK
    )
    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("50.00")
    assert db.session.get(SearchProfile, profile.id).scoring_config == {
        "categories": {"housing": {}}
    }


def test_a_backup_never_replaces_an_existing_rollback_point(app, tmp_path):
    profile = _profile("Default", {"categories": {}})
    prop = _property("a", profile)
    backup = tmp_path / "before.json"
    backup.write_text('{"someone else": "was here"}', encoding="utf-8")
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(prop.id, "63.37", "67.92")]
    )

    with pytest.raises(SystemExit):
        tool.run(["--snapshot", path, "--apply", "--backup", str(backup)])

    assert backup.read_text(encoding="utf-8") == '{"someone else": "was here"}'
    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("50.00")


def test_apply_demands_a_backup_decision(app, tmp_path):
    profile = _profile("Default", {"categories": {}})
    prop = _property("a", profile)
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(prop.id, "63.37", "67.92")]
    )

    with pytest.raises(SystemExit):
        tool.run(["--snapshot", path, "--apply"])

    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("50.00")


def test_one_unusable_value_restores_nothing_at_all(app, tmp_path):
    profile = _profile("Default", {"categories": {}})
    good = _property("a", profile)
    bad = _property("b", profile)
    path = _snapshot_file(
        tmp_path,
        {str(profile.id): None},
        [_row(good.id, "63.37", "67.92"), _row(bad.id, "not a number", "1.00")],
    )

    assert tool.run(["--snapshot", path, "--apply", "--no-backup"]) == tool.EXIT_REFUSED

    db.session.expire_all()
    assert db.session.get(Property, good.id).score_total == Decimal("50.00")
    assert db.session.get(SearchProfile, profile.id).scoring_config == {
        "categories": {}
    }


def test_a_column_the_snapshot_does_not_carry_is_left_alone(app, tmp_path):
    profile = _profile("Default", None)
    prop = _property(
        "a", profile, enrichment={"pool": {"status": "ok", "candidates": [1]}}
    )
    path = _snapshot_file(tmp_path, {}, [_row(prop.id, "63.37", "67.92")])

    tool.run(["--snapshot", path, "--apply", "--no-backup"])

    db.session.expire_all()
    # The pool-weight snapshot carries no enrichment; nulling it here would
    # erase every measurement the rewrite never touched.
    assert db.session.get(Property, prop.id).enrichment == {
        "pool": {"status": "ok", "candidates": [1]}
    }


def test_a_property_that_no_longer_exists_does_not_stop_the_rest(app, tmp_path):
    profile = _profile("Default", None)
    prop = _property("a", profile)
    path = _snapshot_file(
        tmp_path,
        {},
        [_row(prop.id, "63.37", "67.92"), _row(prop.id + 5000, "1.00", "2.00")],
    )

    plan = tool.build_plan(score_snapshot.load(path))
    assert plan.missing == [prop.id + 5000]
    assert any("GONE" in line for line in tool.describe(plan))

    assert tool.run(["--snapshot", path, "--apply", "--no-backup"]) == tool.EXIT_OK
    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("63.37")


def test_rows_the_snapshot_cannot_speak_for_are_named(app, tmp_path):
    profile = _profile(
        "Default", {"categories": {"housing": {"lifestyle": {"pool_score": 0.1}}}}
    )
    covered = _property("a", profile)
    newer = _property("b", profile)
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(covered.id, "63.37", "67.92")]
    )

    plan = tool.build_plan(score_snapshot.load(path))
    assert plan.uncovered == [newer.id]
    assert any("NOT in the" in line for line in tool.describe(plan))

    tool.run(["--snapshot", path, "--apply", "--no-backup"])
    db.session.expire_all()
    # Left as they are, because nothing knows what they were.
    assert db.session.get(Property, newer.id).score_total == Decimal("50.00")


def test_uncovered_rows_are_rescored_only_when_asked(app, tmp_path, monkeypatch):
    profile = _profile(
        "Default", {"categories": {"housing": {"lifestyle": {"pool_score": 0.1}}}}
    )
    covered = _property("a", profile)
    newer = _property("b", profile)
    path = _snapshot_file(
        tmp_path, {str(profile.id): None}, [_row(covered.id, "63.37", "67.92")]
    )

    class _Rescorer:
        def calculate_for_property(self, prop, commit=False):
            prop.score_total = Decimal("11.11")
            return True

    monkeypatch.setattr(
        "services.property_scoring_service.PropertyScoringService", _Rescorer
    )

    tool.run(["--snapshot", path, "--apply", "--no-backup", "--rescore-uncovered"])

    db.session.expire_all()
    assert db.session.get(Property, newer.id).score_total == Decimal("11.11")
    assert db.session.get(Property, covered.id).score_total == Decimal("63.37")


def test_a_bare_row_list_is_a_snapshot_too(app, tmp_path):
    profile = _profile("Default", None)
    prop = _property("a", profile)
    path = tmp_path / "flat.json"
    path.write_text(json.dumps([_row(prop.id, "63.37", "67.92")]), encoding="utf-8")

    assert tool.run(["--snapshot", str(path), "--apply", "--no-backup"]) == tool.EXIT_OK
    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("63.37")


def test_a_snapshot_restoring_nothing_is_refused(app, tmp_path):
    path = _snapshot_file(tmp_path, {}, [])
    assert tool.run(["--snapshot", path]) == tool.EXIT_REFUSED


def test_a_snapshot_that_is_not_one_is_refused(app, tmp_path):
    path = tmp_path / "junk.json"
    path.write_text('{"rows": []}', encoding="utf-8")
    assert tool.run(["--snapshot", str(path)]) == tool.EXIT_REFUSED

    path.write_text("not json at all", encoding="utf-8")
    assert tool.run(["--snapshot", str(path)]) == tool.EXIT_REFUSED


def test_a_row_naming_a_column_this_app_does_not_restore_is_refused(app, tmp_path):
    profile = _profile("Default", None)
    prop = _property("a", profile)
    row = _row(prop.id, "63.37", "67.92")
    row["price"] = 1
    path = _snapshot_file(tmp_path, {}, [row])

    assert tool.run(["--snapshot", path, "--apply", "--no-backup"]) == tool.EXIT_REFUSED
    db.session.expire_all()
    assert db.session.get(Property, prop.id).score_total == Decimal("50.00")


def test_a_failure_half_way_leaves_the_table_as_it_was(app, tmp_path, monkeypatch):
    profile = _profile("Default", {"categories": {}})
    first = _property("a", profile)
    second = _property("b", profile)
    path = _snapshot_file(
        tmp_path,
        {str(profile.id): None},
        [_row(first.id, "63.37", "67.92"), _row(second.id, "10.00", "11.00")],
    )

    real_apply = score_snapshot.apply_rows

    def _boom(rows):
        rows = list(rows)
        real_apply(rows[:1])
        raise RuntimeError("database went away")

    monkeypatch.setattr(score_snapshot, "apply_rows", _boom)

    with pytest.raises(RuntimeError):
        tool.run(["--snapshot", path, "--apply", "--no-backup"])

    # The session itself is the assertion: `expire_all()` below would discard
    # unflushed changes and make a tool that never rolled back look like one
    # that did — which is exactly what a first version of this test proved
    # about nothing (mutation run, 2026-08-15).
    assert not db.session.dirty
    assert not db.session.new

    db.session.expire_all()
    assert db.session.get(Property, first.id).score_total == Decimal("50.00")
    assert db.session.get(SearchProfile, profile.id).scoring_config == {
        "categories": {}
    }


def test_a_restore_that_would_change_nothing_says_so(app, tmp_path):
    profile = _profile("Default", None)
    prop = _property("a", profile, total="63.37", lifestyle="67.92")
    path = _snapshot_file(
        tmp_path,
        {str(profile.id): None},
        [_row(prop.id, "63.37", "67.92", scoring={"version": 1, "marker": "a"})],
    )

    plan = tool.build_plan(score_snapshot.load(path))
    assert plan.changed == []
    assert plan.unchanged == [prop.id]
    assert plan.writes_nothing
