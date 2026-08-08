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


def test_planning_reruns_until_no_further_group_can_be_repaired(app):
    """Alphabetical order must not decide how much gets repaired.

    Alpha is spread over two profiles, but both also hold Beta listings, so
    on the first pass neither can be promoted. Beta then leaves for the
    profile that already carries its name -- and *that* is what makes the two
    Alpha profiles promotable. Planning has to come back for Alpha instead of
    leaving it to a second run the runbook does not promise.
    """
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="Mixed A", is_active=True),
                SearchProfile(id=2, name="Mixed B", is_active=True),
                SearchProfile(id=3, name="Beta", is_active=True),
            ]
        )
        _add_listings(1, "New home in your search: Alpha!", 2, "a1")
        _add_listings(1, "New home in your search: Beta!", 3, "b1")
        _add_listings(2, "New home in your search: Alpha!", 4, "a2")
        _add_listings(2, "New home in your search: Beta!", 1, "b2")
        _add_listings(3, "New home in your search: Beta!", 2, "b3")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert report["blocked_groups"] == []
        assert db.session.get(SearchProfile, 2).name == "Alpha", "the richer of the two"
        assert _count(2) == 6
        assert db.session.get(SearchProfile, 3).name == "Beta"
        assert _count(3) == 6
        assert db.session.get(SearchProfile, 1) is None, "emptied by both groups"
        _assert_every_listing_matches_its_profile_name()


def test_a_single_profile_with_a_mismatched_name_is_not_renamed(app):
    """One profile, one saved search, a name someone chose. Not fragmentation.

    A profile filled by ProfileAssignmentService's location matching, or
    named by hand, holds a subscription's listings under a name of its own.
    Nothing here is broken, so renaming it would only overwrite the owner's
    decision.
    """
    with app.app_context():
        db.session.add(SearchProfile(id=5, name="Norte hand-picked", is_active=True))
        _add_listings(5, "New home in your search: Alpha!", 4, "alpha")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 5).name == "Norte hand-picked"
        assert _count(5) == 4
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert [g["name"] for g in report["blocked_groups"]] == ["Alpha"]


def test_a_manually_reassigned_listing_is_never_moved_back(fragmented):
    """The profile-change form pins a listing; the repair must leave it pinned.

    `ProfileAssignmentService` already refuses to move these. A repair that
    dragged them back to the profile their subject names -- and deleted the
    profile the owner chose, once it looked empty -- would quietly undo a
    decision made through the UI.
    """
    with fragmented.app_context():
        pinned = Property.query.filter_by(search_profile_id=7).first()
        pinned.enrichment = {
            "profile_assignment": {
                "method": "manual_override",
                "profile_id": 7,
                "manual_override": True,
            }
        }
        db.session.commit()
        pinned_id = pinned.id

        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied"
        assert report["manually_pinned_properties"] == 1
        assert report["properties_moved"] == TOTAL_LISTINGS - FRAGMENTS[8][2] - 1

        assert db.session.get(Property, pinned_id).search_profile_id == 7
        assert db.session.get(SearchProfile, 7) is not None, (
            "the profile the owner chose must not be deleted out from under it"
        )
        assert _count(7) == 1
        assert _count(TARGET_ID) == TOTAL_LISTINGS - 1
        assert sorted(report["profiles_deleted"]) == [9, 10]


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


def _assert_every_listing_matches_its_profile_name():
    """The invariant the whole repair exists to establish."""
    for prop in Property.query.all():
        expected = SearchProfileRepairService.recompute_profile_name(prop.email_subject)
        if expected is None:
            continue
        profile = db.session.get(SearchProfile, prop.search_profile_id)
        assert profile is not None, f"{prop.source_email_id} lost its profile"
        assert profile.name == expected, (
            f"{prop.source_email_id} resolves to {expected!r} but sits in "
            f"profile #{profile.id} {profile.name!r}"
        )


def test_a_rename_frees_a_name_that_another_group_must_not_inherit(app):
    """Profile 1 is called "Beta" but holds only Alpha listings.

    Promoting it renames it to "Alpha", which frees the name "Beta". The group
    for Beta then looks the name up -- and against a pre-repair snapshot it
    finds that very profile, merging two subscriptions under one row and
    calling it applied. The lookup has to see the names the plan has already
    changed.
    """
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="Beta", is_active=True),
                SearchProfile(id=2, name="scratch", is_active=True),
                SearchProfile(id=3, name="other", is_active=True),
                SearchProfile(id=4, name="another", is_active=True),
            ]
        )
        # Both saved searches are genuinely fragmented, so both may be repaired.
        _add_listings(1, "New home in your search: Alpha!", 3, "a1")
        _add_listings(2, "New home in your search: Alpha!", 1, "a2")
        _add_listings(3, "New home in your search: Beta!", 2, "b3")
        _add_listings(4, "New home in your search: Beta!", 1, "b4")
        db.session.commit()

        SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 1).name == "Alpha"
        assert _count(1) == 4
        survivor = db.session.get(SearchProfile, 3)
        assert survivor is not None, "the Beta listings must keep a home of their own"
        assert survivor.name == "Beta"
        assert _count(3) == 3
        assert db.session.get(SearchProfile, 2) is None
        assert db.session.get(SearchProfile, 4) is None
        _assert_every_listing_matches_its_profile_name()


def test_a_profile_that_gains_a_saved_search_while_planning_is_blocked(app):
    """Ambiguity the plan creates itself must block just like pre-existing.

    Zeta is fragmented across profiles 1 and 2, and both start out holding
    nothing but Zeta -- so on the pre-repair snapshot both look promotable.
    The Alpha and Beta groups then legitimately move their own listings into
    them, and *that* is what makes both ambiguous. Zeta must end up blocked,
    not renaming a profile it has just come to share.
    """
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="Alpha", is_active=True),
                SearchProfile(id=2, name="Beta", is_active=True),
                SearchProfile(id=3, name="scratch a", is_active=True),
                SearchProfile(id=4, name="scratch b", is_active=True),
            ]
        )
        _add_listings(1, "New home in your search: Zeta!", 3, "z1")
        _add_listings(2, "New home in your search: Zeta!", 2, "z2")
        _add_listings(3, "New home in your search: Alpha!", 2, "a3")
        _add_listings(4, "New home in your search: Beta!", 1, "b4")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert [g["name"] for g in report["blocked_groups"]] == ["Zeta"]
        assert db.session.get(SearchProfile, 1).name == "Alpha"
        assert db.session.get(SearchProfile, 2).name == "Beta"
        assert _count(1) == 5  # 3 Zeta squatters + the 2 Alpha listings moved in
        assert _count(2) == 3  # 2 Zeta squatters + the 1 Beta listing moved in
        assert _profile_ids() == [1, 2], "the two emptied fragments were removed"


def _commit_raises(monkeypatch):
    """A COMMIT that never returns cleanly, the outcome unknowable from here."""

    def boom():
        raise RuntimeError("server closed the connection during COMMIT")

    monkeypatch.setattr(db.session, "commit", boom)


def test_a_commit_that_raises_is_unknown_not_a_confirmed_rollback(
    fragmented, monkeypatch
):
    """A COMMIT can be applied by the server and lost on the way back.

    Calling that a rollback tells the owner the database is untouched at the
    exact moment it may not be.
    """
    _commit_raises(monkeypatch)

    with fragmented.app_context():
        report = SearchProfileRepairService.apply()

        assert report["status"] == "commit_outcome_unknown"
        assert report["status"] != "mismatch"
        assert "UNKNOWN" in " ".join(report["errors"])
        assert report["post_commit_observation"]["readable"] is True


def test_cli_exit_code_marks_an_unknown_commit_outcome(fragmented, monkeypatch):
    _commit_raises(monkeypatch)

    with fragmented.app_context():
        # Not 1: 1 promises the database is untouched, which nobody can promise
        # once COMMIT has been sent.
        assert run_repair_cli(["--apply"]) == 3


def test_a_listing_landing_after_the_zero_check_aborts_the_repair(
    fragmented, monkeypatch
):
    """The ON DELETE SET NULL race, fired at the one instant it can bite.

    A listing inserted into a fragment *after* its pre-delete zero-check and
    *before* the DELETE reaches the database is not rejected: the FK nullifies
    it and the row is orphaned. Leaving the DELETE pending until `commit()`
    flushes it means nothing looks at the database between the two, so the
    deletes have to be flushed and re-verified inside the pre-COMMIT phase.
    """
    from services import search_profile_repair_service as module

    real = module._remaining_property_count
    state = {"orphan_check_seen": False, "fired": False}
    racing_fragment = FRAGMENT_IDS[0]

    def racing(profile_id):
        count = real(profile_id)
        if profile_id is None:
            # The orphan check closes the verification pass; every call after
            # it belongs to the delete loop.
            state["orphan_check_seen"] = True
            return count
        if (
            state["orphan_check_seen"]
            and profile_id == racing_fragment
            and not state["fired"]
        ):
            state["fired"] = True
            db.session.add(
                Property(
                    source_email_id="imap_race",
                    email_subject=FRAGMENTS[racing_fragment][1],
                    search_profile_id=profile_id,
                    title="arrived mid-repair",
                )
            )
            db.session.flush()
        # The caller sees the count from *before* the insert -- that is the race.
        return count

    monkeypatch.setattr(module, "_remaining_property_count", racing)

    with fragmented.app_context():
        report = SearchProfileRepairService.apply()

        assert state["fired"], "the race never fired; the test proves nothing"
        assert report["status"] == "mismatch"
        assert any("without a profile" in message for message in report["errors"])

        # Nothing was committed, and above all nothing was orphaned.
        db.session.expire_all()
        assert db.session.get(SearchProfile, racing_fragment) is not None
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2]
        assert Property.query.filter(Property.search_profile_id.is_(None)).count() == 0


def test_canonically_equal_names_are_one_group_and_keep_one_setting(app):
    """ "ALPHA" and "Alpha" are the same saved search to ingestion.

    Planned as two groups they resolve to the same target, each merging
    settings against the target's *original* state -- so both donors report
    "preserved", the later one silently overwrites the earlier, and both get
    deleted. One canonical group means one merge and one honest conflict.
    """
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="Alpha", is_active=True),
                SearchProfile(
                    id=2, name="frag upper", is_active=True, ai_config={"who": "two"}
                ),
                SearchProfile(
                    id=3, name="frag lower", is_active=True, ai_config={"who": "three"}
                ),
            ]
        )
        _add_listings(2, "New home in your search: ALPHA!", 2, "upper")
        _add_listings(3, "New home in your search: Alpha!", 3, "lower")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert len(report["groups"]) == 1, "one saved search, one group"
        group = report["groups"][0]
        assert group["name"] == "Alpha", "the spelling most of the listings use"
        assert group["target_id"] == 1
        assert sorted(group["fragment_ids"]) == [2, 3]

        target = db.session.get(SearchProfile, 1)
        assert target.name == "Alpha"
        assert target.ai_config == {"who": "two"}, "the first donor wins"
        assert {"profile_id": 2, "field": "ai_config"} in group["settings_preserved"]
        assert {"profile_id": 3, "field": "ai_config"} in group["settings_conflicts"]
        assert {"profile_id": 3, "field": "ai_config"} not in group[
            "settings_preserved"
        ]

        assert _count(1) == 5
        assert _profile_ids() == [1]
