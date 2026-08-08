"""One saved search, four SearchProfile rows: repairing what the fold broke.

#101 stops new fragments arriving; this suite pins the repair of the ones
already in the database. The fixture reproduces the live state exactly -- the
four profiles, their real folded subjects and their real listing counts:

    id 7  | houses at your custom search            | 13
    id 8  | houses at your custom search area norte |  3
    id 9  | houses at your custom search area       | 24
    id 10 | houses at your custom                   |  1

`properties.email_subject` is stored folded for every row written before #101,
so the correct name is recoverable from data already on disk: unfold the
subject, then run the *same* extractor ingestion runs. No prefix matching, no
token similarity -- those would happily collapse two genuinely different
subscriptions, which is why the fixture also carries "Homes in Ciudad Quesada"
and "Homes in Ciudad Quesada Norte" side by side. A prefix heuristic merges
them; this repair must not.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.search_profile_repair_service import (
    SearchProfileRepairService,
    run_repair_cli,
)

CORRECT_NAME = "houses at your custom search area norte"

# id -> (profile name as stored today, subject exactly as the server folded it,
#        number of listings sitting in that profile)
FRAGMENTS = {
    7: (
        "houses at your custom search",
        "New detached house in your search: houses at your custom search\r\n area norte!",
        13,
    ),
    8: (
        CORRECT_NAME,
        "New caseron in your search: houses at your custom search area norte!",
        3,
    ),
    9: (
        "houses at your custom search area",
        "Price reduction in your search: houses at your custom search area\r\n norte!",
        24,
    ),
    10: (
        "houses at your custom",
        "New semi-detached house in your search: houses at your custom\r\n search area norte!",
        1,
    ),
}

TARGET_ID = 8
FRAGMENT_IDS = [7, 9, 10]
TOTAL_LISTINGS = 41

# Untouched neighbours. 3 and 4 are two *different* saved searches whose names
# happen to share a prefix; 6 is an unrelated subscription entirely.
NEIGHBOURS = {
    3: (
        "Homes in Ciudad Quesada",
        "New home in your search: Homes in Ciudad Quesada!",
        2,
    ),
    4: (
        "Homes in Ciudad Quesada Norte",
        "New home in your search: Homes in Ciudad Quesada Norte!",
        2,
    ),
    6: (
        "Land at Norte",
        "New plot of land in your search: Land at Norte!",
        5,
    ),
}


@pytest.fixture
def app():
    from tests import setup_test_environment

    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _seed(rows):
    """Create the given {profile_id: (name, subject, count)} rows."""
    counter = 0
    for profile_id, (name, subject, count) in sorted(rows.items()):
        db.session.add(
            SearchProfile(
                id=profile_id,
                name=name,
                is_active=True,
                is_default=False,
                travel_targets={"presets": {}, "custom": []},
            )
        )
        for _ in range(count):
            counter += 1
            db.session.add(
                Property(
                    source_email_id=f"imap_{profile_id}_{counter}",
                    idealista_property_id=100000 + counter,
                    email_subject=subject,
                    search_profile_id=profile_id,
                    title=f"Listing {counter}",
                    url=f"https://www.idealista.com/inmueble/{100000 + counter}/",
                )
            )
    db.session.commit()


@pytest.fixture
def fragmented(app):
    """The live state: four fragments plus three unrelated profiles."""
    with app.app_context():
        _seed({**FRAGMENTS, **NEIGHBOURS})
        yield app


def _rows():
    """Every property as (source_email_id, search_profile_id), sorted."""
    return sorted(
        (p.source_email_id, p.search_profile_id) for p in Property.query.all()
    )


def _profile_ids():
    return sorted(p.id for p in SearchProfile.query.all())


def _count(profile_id):
    return Property.query.filter_by(search_profile_id=profile_id).count()


def _add_listings(profile_id, subject, count, tag):
    for index in range(count):
        db.session.add(
            Property(
                source_email_id=f"imap_{tag}_{index}",
                email_subject=subject,
                search_profile_id=profile_id,
                title=f"{tag} {index}",
            )
        )


def test_name_is_recomputed_from_the_stored_folded_subject(fragmented):
    """No heuristics: every fragment's own subject already holds the full name."""
    with fragmented.app_context():
        for _, (_, subject, _) in FRAGMENTS.items():
            assert (
                SearchProfileRepairService.recompute_profile_name(subject)
                == CORRECT_NAME
            )


def test_dry_run_changes_no_rows(fragmented):
    with fragmented.app_context():
        before_rows = _rows()
        before_profiles = _profile_ids()

        report = SearchProfileRepairService.analyze()

        db.session.expire_all()
        assert _rows() == before_rows
        assert _profile_ids() == before_profiles
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2]

        assert report["mode"] == "dry-run"
        assert report["status"] == "pending"
        assert report["properties_to_move"] == TOTAL_LISTINGS - FRAGMENTS[8][2]
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert sorted(report["profiles_to_delete"]) == FRAGMENT_IDS


def test_apply_merges_the_four_fragments_into_one_profile(fragmented):
    with fragmented.app_context():
        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied"
        assert report["properties_moved"] == TOTAL_LISTINGS - FRAGMENTS[8][2]
        assert sorted(report["profiles_deleted"]) == FRAGMENT_IDS

        target = db.session.get(SearchProfile, TARGET_ID)
        assert target is not None
        assert target.name == CORRECT_NAME
        assert _count(TARGET_ID) == TOTAL_LISTINGS

        for fragment_id in FRAGMENT_IDS:
            assert db.session.get(SearchProfile, fragment_id) is None

        assert Property.query.filter(Property.search_profile_id.is_(None)).count() == 0


def test_report_carries_the_counts_before_and_after(fragmented):
    with fragmented.app_context():
        report = SearchProfileRepairService.apply()

        before = {e["profile_id"]: e["properties"] for e in report["profiles_before"]}
        after = {e["profile_id"]: e["properties"] for e in report["profiles_after"]}

        assert before == {pid: FRAGMENTS[pid][2] for pid in FRAGMENTS}
        assert after == {7: 0, 8: TOTAL_LISTINGS, 9: 0, 10: 0}
        assert sum(before.values()) == sum(after.values()) == TOTAL_LISTINGS


def test_apply_promotes_a_fragment_when_no_profile_holds_the_correct_name(app):
    """Renaming the richest fragment keeps its settings; creating one would not."""
    with app.app_context():
        _seed({7: FRAGMENTS[7], 9: FRAGMENTS[9], 10: FRAGMENTS[10]})

        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied"
        survivor = db.session.get(SearchProfile, 9)  # 24 listings, the richest
        assert survivor is not None
        assert survivor.name == CORRECT_NAME
        assert _count(9) == 13 + 24 + 1
        assert sorted(report["profiles_deleted"]) == [7, 10]
        assert _profile_ids() == [9]


def test_apply_leaves_unrelated_profiles_alone(fragmented):
    """Including two real searches whose names share a prefix."""
    with fragmented.app_context():
        SearchProfileRepairService.apply()

        for profile_id, (name, _, count) in NEIGHBOURS.items():
            profile = db.session.get(SearchProfile, profile_id)
            assert profile is not None, f"profile {profile_id} was wrongly deleted"
            assert profile.name == name
            assert _count(profile_id) == count


def test_second_apply_is_a_successful_no_op(fragmented):
    with fragmented.app_context():
        SearchProfileRepairService.apply()
        after_first = _rows()

        report = SearchProfileRepairService.apply()

        assert report["status"] == "clean"
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert _rows() == after_first
        assert _profile_ids() == sorted([TARGET_ID, *NEIGHBOURS])


def test_already_repaired_database_reports_clean_not_an_error(app):
    """The "already applied" state is a valid success, not a missing-work error."""
    with app.app_context():
        _seed({8: FRAGMENTS[8], **NEIGHBOURS})

        report = SearchProfileRepairService.analyze()

        assert report["status"] == "clean"
        assert report["properties_to_move"] == 0
        assert report["groups"] == []
        assert run_repair_cli([]) == 0


def test_cli_exit_codes(fragmented):
    with fragmented.app_context():
        assert run_repair_cli([]) == 0
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "dry-run must not write"

        assert run_repair_cli(["--apply", "--json"]) == 0
        assert _count(TARGET_ID) == TOTAL_LISTINGS


def test_apply_preserves_fragment_settings_on_the_surviving_profile(fragmented):
    with fragmented.app_context():
        fragment = db.session.get(SearchProfile, 9)
        fragment.classification_rules = [{"category": "house", "pattern": "casa"}]
        fragment.travel_targets = {
            "presets": {},
            "custom": [
                {"name": "Office", "lat": 43.36, "lon": -5.85, "mode": "driving"}
            ],
        }
        db.session.commit()

        SearchProfileRepairService.apply()

        target = db.session.get(SearchProfile, TARGET_ID)
        assert target.classification_rules == [{"category": "house", "pattern": "casa"}]
        custom = (target.travel_targets or {}).get("custom") or []
        assert [item["name"] for item in custom] == ["Office"]


def test_apply_never_overwrites_settings_the_target_already_has(fragmented):
    with fragmented.app_context():
        db.session.get(SearchProfile, TARGET_ID).ai_config = {
            "market_context": "target"
        }
        db.session.get(SearchProfile, 7).ai_config = {"market_context": "fragment"}
        db.session.commit()

        report = SearchProfileRepairService.apply()

        target = db.session.get(SearchProfile, TARGET_ID)
        assert target.ai_config == {"market_context": "target"}

        group = next(g for g in report["groups"] if g["name"] == CORRECT_NAME)
        assert {"profile_id": 7, "field": "ai_config"} in group["settings_conflicts"]


def test_apply_rolls_back_when_a_fragment_is_not_empty_before_delete(
    fragmented, monkeypatch
):
    """`search_profile_id` is ON DELETE SET NULL.

    A row inserted into a fragment between the zero-check and the DELETE is
    silently orphaned, so the check has to guard the DELETE inside the same
    transaction and abort the whole repair when it fails. Simulated here by
    making the remaining-count report a straggler.
    """
    from services import search_profile_repair_service as module

    monkeypatch.setattr(module, "_remaining_property_count", lambda profile_id: 1)

    with fragmented.app_context():
        before_rows = _rows()
        before_profiles = _profile_ids()

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"
        assert report["errors"]

        db.session.expire_all()
        assert _rows() == before_rows, "a failed repair must not move properties"
        assert _profile_ids() == before_profiles, "no profile may be deleted"


def test_cli_exits_non_zero_on_a_mismatch(fragmented, monkeypatch):
    from services import search_profile_repair_service as module

    monkeypatch.setattr(module, "_remaining_property_count", lambda profile_id: 1)

    with fragmented.app_context():
        assert run_repair_cli(["--apply"]) == 1


def test_properties_without_a_recoverable_name_are_reported_not_touched(app):
    """A fragment that still holds a listing after the moves is never deleted."""
    with app.app_context():
        _seed({**FRAGMENTS, **NEIGHBOURS})
        db.session.add(
            Property(
                source_email_id="imap_no_subject",
                email_subject="Idealista newsletter",
                search_profile_id=7,
                title="No search name here",
            )
        )
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied"
        assert report["unresolved_properties"] == 1
        assert sorted(report["profiles_deleted"]) == [9, 10]
        assert report["profiles_retained"] == [{"profile_id": 7, "remaining": 1}]

        stray = Property.query.filter_by(source_email_id="imap_no_subject").first()
        assert stray.search_profile_id == 7, "a name we cannot recompute stays put"
        assert db.session.get(SearchProfile, 7) is not None, (
            "a fragment still holding listings must survive the repair"
        )
        assert _count(TARGET_ID) == TOTAL_LISTINGS
        assert Property.query.filter(Property.search_profile_id.is_(None)).count() == 0


def test_a_profile_holding_two_recoverable_names_is_never_promoted(app):
    """One profile, two saved searches inside it, no profile carrying either.

    Both groups would independently pick the same profile as their promotion
    target -- nothing reserves it -- so the last one to run renames it and the
    other group's listings silently keep pointing at a profile named after
    someone else's saved search. A profile is only promotable when everything
    it holds resolves to the one name.
    """
    with app.app_context():
        db.session.add(
            SearchProfile(id=5, name="Mixed bag", is_active=True, is_default=False)
        )
        _add_listings(5, "New home in your search: Alpha!", 3, "alpha")
        _add_listings(5, "New home in your search: Beta!", 5, "beta")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        profile = db.session.get(SearchProfile, 5)
        assert profile is not None
        assert profile.name == "Mixed bag", (
            "a profile holding two saved searches must not be renamed after one"
        )
        assert _count(5) == 8
        assert _profile_ids() == [5]
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert sorted(g["name"] for g in report["blocked_groups"]) == ["Alpha", "Beta"]


def test_a_pure_profile_is_still_promoted_next_to_a_mixed_one(app):
    """Blocking the ambiguous group must not block the unambiguous one."""
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=5, name="pure alpha", is_active=True),
                SearchProfile(id=6, name="Mixed bag", is_active=True),
            ]
        )
        _add_listings(5, "New home in your search: Alpha!", 3, "alpha_pure")
        _add_listings(6, "New home in your search: Alpha!", 2, "alpha_mixed")
        _add_listings(6, "New home in your search: Beta!", 4, "beta")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 5).name == "Alpha"
        assert _count(5) == 5, "both runs of Alpha listings land on the promoted row"
        assert db.session.get(SearchProfile, 6).name == "Mixed bag"
        assert _count(6) == 4
        assert [g["name"] for g in report["blocked_groups"]] == ["Beta"]
        assert report["profiles_deleted"] == [], "profile 6 still holds Beta listings"


def _flaky_counts(monkeypatch):
    """Make the *post-commit* count query fail; the planning one still works."""
    from services import search_profile_repair_service as module

    real = module._profile_property_counts
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:  # the first call builds the plan, before the commit
            raise RuntimeError("server closed the connection unexpectedly")
        return real()

    monkeypatch.setattr(module, "_profile_property_counts", flaky)


def test_a_failure_after_the_commit_reports_committed_not_rolled_back(
    fragmented, monkeypatch
):
    """A non-zero exit must never imply "rolled back" once the commit landed.

    The destructive half is committed by then and only the after-report is
    missing. Reporting that as a plain failure would send the owner looking
    for a rollback that never happened.
    """
    _flaky_counts(monkeypatch)

    with fragmented.app_context():
        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied_report_unavailable"
        assert report["errors"]
        assert report["properties_moved"] == TOTAL_LISTINGS - FRAGMENTS[8][2]

        # The repair really did commit; the report is what failed.
        assert _count(TARGET_ID) == TOTAL_LISTINGS
        for fragment_id in FRAGMENT_IDS:
            assert db.session.get(SearchProfile, fragment_id) is None


def test_cli_exit_code_separates_a_rollback_from_a_committed_repair(
    fragmented, monkeypatch
):
    _flaky_counts(monkeypatch)

    with fragmented.app_context():
        # 1 is reserved for "nothing was committed"; this is not that.
        assert run_repair_cli(["--apply"]) == 2
        assert _count(TARGET_ID) == TOTAL_LISTINGS
