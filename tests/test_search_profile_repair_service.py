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


def _folded_at(name, truncated_to):
    """A subject for `name` whose fold cuts the name down to `truncated_to`.

    The fold is the mechanism: the pre-#101 extractor stopped at the CR, so
    replaying it on this subject yields exactly `truncated_to` -- the name the
    fragment profile ended up carrying. Rows written after #101 are stored
    unfolded and can never be that evidence, which is why this repair only
    ever acts on pre-fix data.
    """
    assert name.startswith(truncated_to + " "), "the fold must land inside the name"
    tail = name[len(truncated_to) :].lstrip()
    return f"New home in your search: {truncated_to}\r\n {tail}!"


def _folded(name):
    """A subject folded before the last word of `name`."""
    head, _, _tail = name.rpartition(" ")
    assert head, "a one-word name cannot fold mid-name"
    return _folded_at(name, head)


def _plain(name):
    """A subject that was never folded -- what ingestion stores since #101."""
    return f"New home in your search: {name}!"


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
        assert report["profiles_retained"] == [
            {"profile_id": 7, "remaining": 1, "reason": "still holds listings"}
        ]

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
    it holds resolves to the one name. "alpha" is a fold-shaped name for both
    searches, so only the ambiguity stands between it and a rename.
    """
    with app.app_context():
        db.session.add(
            SearchProfile(id=5, name="alpha", is_active=True, is_default=False)
        )
        _add_listings(5, _folded("alpha beta"), 3, "beta")
        _add_listings(5, _folded("alpha gamma"), 5, "gamma")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        profile = db.session.get(SearchProfile, 5)
        assert profile is not None
        assert profile.name == "alpha", (
            "a profile holding two saved searches must not be renamed after one"
        )
        assert _count(5) == 8
        assert _profile_ids() == [5]
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert sorted(g["name"] for g in report["blocked_groups"]) == [
            "alpha beta",
            "alpha gamma",
        ]


def test_planning_reruns_until_no_further_group_can_be_repaired(app):
    """Alphabetical order must not decide how much gets repaired.

    Alpha is spread over two profiles, but both also hold Beta listings, so
    on the first pass neither can be promoted. Beta then leaves for the
    profile that already carries its name -- and *that* is what makes the two
    Alpha profiles promotable. Planning has to come back for Alpha instead of
    leaving it to a second run the runbook does not promise.
    """
    first = "alpha beta gamma delta"
    second = "alpha beta zeta eta"

    with app.app_context():
        db.session.add_all(
            [
                # Both names fold to these two shorter ones, so both profiles
                # are fold fragments of both saved searches.
                SearchProfile(id=1, name="alpha", is_active=True),
                SearchProfile(id=2, name="alpha beta", is_active=True),
                SearchProfile(id=3, name=second, is_active=True),
            ]
        )
        _add_listings(1, _folded_at(first, "alpha"), 2, "a1")
        _add_listings(1, _folded_at(second, "alpha"), 3, "b1")
        _add_listings(2, _folded_at(first, "alpha beta"), 4, "a2")
        _add_listings(2, _folded_at(second, "alpha beta"), 1, "b2")
        _add_listings(3, _plain(second), 2, "b3")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert report["blocked_groups"] == []
        assert db.session.get(SearchProfile, 2).name == first, "the richer of the two"
        assert _count(2) == 6
        assert db.session.get(SearchProfile, 3).name == second
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
        _add_listings(5, _plain("Alpha"), 4, "alpha")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 5).name == "Norte hand-picked"
        assert _count(5) == 4
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert [g["name"] for g in report["blocked_groups"]] == ["Alpha"]


def test_a_geo_split_of_one_subscription_across_two_profiles_survives(app):
    """Two profiles, one saved search, and nothing folded about the names.

    `ProfileAssignmentService` files listings by location, so one
    subscription legitimately lives in "Coast" and "City". Two profiles
    holding one saved search is therefore *not* evidence of fold
    fragmentation, and treating it as such would rename one, empty the other
    and delete it -- destroying a split the owner set up on purpose.

    The subjects here are deliberately folded: only the word-boundary prefix
    test tells these apart from real fragments, and it has to carry that
    weight on its own.
    """
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="Coast", is_active=True),
                SearchProfile(id=2, name="City", is_active=True),
            ]
        )
        _add_listings(1, _folded("Homes in Alicante"), 3, "coast")
        _add_listings(2, _folded("Homes in Alicante"), 2, "city")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 1).name == "Coast"
        assert db.session.get(SearchProfile, 2).name == "City"
        assert _count(1) == 3
        assert _count(2) == 2
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert [g["name"] for g in report["blocked_groups"]] == ["Homes in Alicante"]


def test_a_line_break_outside_the_name_is_not_evidence_of_a_fold(app):
    """The break has to be what truncated the name, not merely present.

    Here it lands after the whole name, so the pre-#101 extractor would have
    returned the name in full and no profile was ever damaged. Treating any
    line break as evidence hands a rename and a deletion to profiles that the
    bug never touched.
    """
    full = "Alpha Beta Gamma"
    subject = f"New home in your search: {full}!\r\n extra"

    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="Alpha Beta", is_active=True),
                SearchProfile(id=2, name="Alpha", is_active=True),
            ]
        )
        _add_listings(1, subject, 3, "one")
        _add_listings(2, subject, 2, "two")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 1).name == "Alpha Beta"
        assert db.session.get(SearchProfile, 2).name == "Alpha"
        assert _count(1) == 3
        assert _count(2) == 2
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert [g["name"] for g in report["blocked_groups"]] == [full]


def test_a_fold_landing_on_punctuation_is_still_recognised(app):
    """Replaying the bug beats reasoning about the shape of the name.

    "Homes in Ciudad Quesada, Alicante" folded after the comma leaves
    "Homes in Ciudad Quesada" once trailing punctuation is cleaned -- which is
    *not* a word-boundary prefix of the full name, because the next character
    is a comma. It is a real fragment all the same, which is why the prefix
    test was dropped rather than kept as a second belt.
    """
    full = "Homes in Ciudad Quesada, Alicante"
    folded = "New home in your search: Homes in Ciudad Quesada,\r\n Alicante!"

    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name=full, is_active=True),
                SearchProfile(id=2, name="Homes in Ciudad Quesada", is_active=True),
            ]
        )
        _add_listings(1, _plain(full), 1, "home")
        _add_listings(2, folded, 3, "frag")
        db.session.commit()

        SearchProfileRepairService.apply()

        assert _count(1) == 4
        assert db.session.get(SearchProfile, 2) is None
        _assert_every_listing_matches_its_profile_name()


def test_an_unrecognisable_listing_blocks_a_promotion(app):
    """A rename must prove the profile holds nothing but that saved search.

    Listings whose name cannot be recomputed never enter the projection, so a
    fragment carrying one looked pure and got renamed -- leaving a profile
    named after a saved search while holding somebody else's row.
    """
    full = "alpha beta gamma"

    with app.app_context():
        db.session.add(SearchProfile(id=1, name="alpha beta", is_active=True))
        _add_listings(1, _folded_at(full, "alpha beta"), 4, "frag")
        db.session.add(
            Property(
                source_email_id="imap_junk",
                email_subject="Idealista newsletter",
                search_profile_id=1,
                title="not a saved-search alert",
            )
        )
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 1).name == "alpha beta", (
            "a profile still holding an unrecognisable listing must not be renamed"
        )
        assert _count(1) == 5
        assert report["properties_moved"] == 0
        assert report["profiles_deleted"] == []
        assert [g["name"] for g in report["blocked_groups"]] == [full]


def test_a_pinned_listing_keeps_its_saved_search_in_the_projection(app):
    """Moving a fragment's listings out does not empty it if some are pinned.

    Profile 2 is a fold fragment of both saved searches. The first group
    takes its movable listings, but a hand-pinned one stays -- so the profile
    still holds that search. Dropping the name from the projection makes the
    second group see a clean profile, rename it, and leave the pinned listing
    sitting under somebody else's saved search.
    """
    first = "aaa bbb"
    second = "aaa ccc"

    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name=first, is_active=True),
                SearchProfile(id=2, name="aaa", is_active=True),
            ]
        )
        _add_listings(1, _plain(first), 1, "home")
        _add_listings(2, _folded_at(first, "aaa"), 2, "movable")
        _add_listings(2, _folded_at(second, "aaa"), 3, "second")
        db.session.add(
            Property(
                source_email_id="imap_pinned",
                email_subject=_folded_at(first, "aaa"),
                search_profile_id=2,
                enrichment={"profile_assignment": {"manual_override": True}},
                title="pinned by hand",
            )
        )
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert db.session.get(SearchProfile, 2).name == "aaa", (
            "a profile still holding a pinned listing of another saved search "
            "must not be renamed after this one"
        )
        pinned = Property.query.filter_by(source_email_id="imap_pinned").first()
        assert pinned.search_profile_id == 2
        assert _count(1) == 3, "only the movable listings moved"
        assert _count(2) == 4, "the pinned one plus the second saved search"
        assert [g["name"] for g in report["blocked_groups"]] == [second]


def test_a_deliberate_profile_keeps_its_listings_when_a_real_fragment_is_merged(app):
    """A fold fragment is repaired; the geo-placed profile beside it is not."""
    full = "alpha beta gamma"
    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name=full, is_active=True),
                SearchProfile(id=2, name="alpha beta", is_active=True),
                SearchProfile(id=3, name="Coast", is_active=True),
            ]
        )
        _add_listings(1, _plain(full), 1, "home")
        _add_listings(2, _folded(full), 4, "fragment")
        _add_listings(3, _folded(full), 2, "coast")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert _count(1) == 5, "the fold fragment's listings were merged in"
        assert db.session.get(SearchProfile, 2) is None, "the fragment was emptied"
        assert db.session.get(SearchProfile, 3).name == "Coast"
        assert _count(3) == 2, "the geo-placed listings stayed where they were put"
        assert {
            "profile_id": 3,
            "name": "Coast",
            "saved_search": full,
            "listings": 2,
        } in report["left_in_place"]


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


def test_profile_lookup_uses_the_names_the_plan_has_already_changed():
    """A rename frees a name; the lookup must not still hand that name out.

    Against a pre-repair snapshot the freed name resolves to the very profile
    that gave it up, which merges two subscriptions into one row and reports
    success. Pinned at the unit here: since a fragment's name is always a
    word-boundary prefix of the full one, it always sorts first, so a plan
    can no longer reach this ordering through promotion -- but the lookup
    must stay correct regardless of what makes it reachable.
    """
    from services.search_profile_repair_service import _find_profile_by_name

    one = SearchProfile(id=1, name="Beta")
    two = SearchProfile(id=2, name="scratch")
    profiles = [one, two]
    effective = {1: "Beta", 2: "scratch"}

    assert _find_profile_by_name("Beta", "beta", profiles, effective) is one

    # Profile 1 has just been promoted and renamed by an earlier group.
    effective[1] = "Alpha"

    assert _find_profile_by_name("Beta", "beta", profiles, effective) is None, (
        "the freed name must not still resolve to the profile that gave it up"
    )
    assert _find_profile_by_name("Alpha", "alpha", profiles, effective) is one


def test_a_profile_that_gains_a_saved_search_while_planning_is_blocked(app):
    """Ambiguity the plan creates itself must block just like pre-existing.

    The long search is fragmented across profiles 1 and 2, and both start out
    holding nothing but it -- so on the pre-repair snapshot both look
    promotable. The two shorter searches then legitimately move their own
    listings into them, and *that* is what makes both ambiguous. The long one
    must end up blocked, not renaming a profile it has just come to share.
    """
    short = "alpha beta"
    middle = "alpha beta gamma"
    long_name = "alpha beta gamma delta"

    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name=short, is_active=True),
                SearchProfile(id=2, name=middle, is_active=True),
                SearchProfile(id=4, name="alpha", is_active=True),
            ]
        )
        _add_listings(1, _folded_at(long_name, short), 3, "z1")
        _add_listings(2, _folded_at(long_name, middle), 2, "z2")
        _add_listings(4, _folded_at(short, "alpha"), 2, "a4")
        _add_listings(4, _folded_at(middle, "alpha"), 1, "b4")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert [g["name"] for g in report["blocked_groups"]] == [long_name]
        assert db.session.get(SearchProfile, 1).name == short
        assert db.session.get(SearchProfile, 2).name == middle
        assert _count(1) == 5  # 3 squatters + the 2 listings moved in
        assert _count(2) == 3  # 2 squatters + the 1 listing moved in
        assert _profile_ids() == [1, 2], "the emptied fragment was removed"


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
                SearchProfile(id=1, name="Alpha Beta Gamma", is_active=True),
                SearchProfile(
                    id=2, name="Alpha Beta", is_active=True, ai_config={"who": "two"}
                ),
                SearchProfile(
                    id=3, name="Alpha", is_active=True, ai_config={"who": "three"}
                ),
            ]
        )
        _add_listings(2, _folded_at("ALPHA BETA GAMMA", "ALPHA BETA"), 2, "upper")
        _add_listings(3, _folded_at("Alpha Beta Gamma", "Alpha"), 3, "lower")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert len(report["groups"]) == 1, "one saved search, one group"
        group = report["groups"][0]
        assert group["name"] == "Alpha Beta Gamma", "the spelling most listings use"
        assert group["target_id"] == 1
        assert sorted(group["fragment_ids"]) == [2, 3]

        target = db.session.get(SearchProfile, 1)
        assert target.name == "Alpha Beta Gamma"
        assert target.ai_config == {"who": "two"}, "the first donor wins"
        assert {"profile_id": 2, "field": "ai_config"} in group["settings_preserved"]
        assert {"profile_id": 3, "field": "ai_config"} in group["settings_conflicts"]
        assert {"profile_id": 3, "field": "ai_config"} not in group[
            "settings_preserved"
        ]

        assert _count(1) == 5
        assert _profile_ids() == [1]


def _plan_then(monkeypatch, mutate):
    """Let planning finish, then let somebody else change the database."""
    from services import search_profile_repair_service as module

    real = module.build_plan

    def plan_then_mutate():
        plan = real()
        mutate()
        db.session.commit()
        return plan

    monkeypatch.setattr(module, "build_plan", plan_then_mutate)


def test_a_manual_pin_landing_after_planning_aborts_the_repair(fragmented, monkeypatch):
    """The plan is a snapshot; the owner can pin a listing right after it.

    Filtering the UPDATE by property id alone drags such a listing back and
    nothing notices, because its new state is never compared with the plan.
    """
    with fragmented.app_context():
        victim = Property.query.filter_by(search_profile_id=7).first()
        victim_id = victim.id

        def pin_it():
            db.session.get(Property, victim_id).enrichment = {
                "profile_assignment": {"manual_override": True}
            }

        _plan_then(monkeypatch, pin_it)

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"
        assert any("by hand" in message for message in report["errors"])

        db.session.expire_all()
        assert db.session.get(Property, victim_id).search_profile_id == 7
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "nothing was committed"


def test_a_listing_moved_away_after_planning_is_not_dragged_back(
    fragmented, monkeypatch
):
    """Same snapshot problem, the other way round: the row left its fragment."""
    with fragmented.app_context():
        victim = Property.query.filter_by(search_profile_id=9).first()
        victim_id = victim.id

        def move_it():
            prop = db.session.get(Property, victim_id)
            prop.search_profile_id = 6  # the owner filed it under another search
            prop.enrichment = {"profile_assignment": {"manual_override": True}}

        _plan_then(monkeypatch, move_it)

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"

        db.session.expire_all()
        assert db.session.get(Property, victim_id).search_profile_id == 6
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "nothing was committed"


def test_a_profile_renamed_after_planning_aborts_the_repair(fragmented, monkeypatch):
    """The plan decided what to merge from the names it read.

    Counts and pins are already re-checked before anything is applied; the
    name was not, so a profile renamed in between sailed through and the
    listings landed in a profile that is no longer the one the plan chose.
    """
    with fragmented.app_context():

        def rename_the_survivor():
            db.session.get(SearchProfile, TARGET_ID).name = "renamed by someone else"

        _plan_then(monkeypatch, rename_the_survivor)

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"
        assert any("its name changed after planning" in m for m in report["errors"])

        db.session.expire_all()
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "nothing was committed"
        for fragment_id in FRAGMENT_IDS:
            assert db.session.get(SearchProfile, fragment_id) is not None


def test_settings_changed_after_planning_abort_the_repair(fragmented, monkeypatch):
    """The plan carries the donor settings it read; they can go stale.

    A fragment's `ai_config` is planned onto the survivor because the survivor
    had none. If the owner configures the survivor a moment later, applying
    the plan unchanged overwrites what they just set -- and no count or pin
    check notices, because nothing about the configuration is compared.
    """
    with fragmented.app_context():
        db.session.get(SearchProfile, 7).ai_config = {"market_context": "the fragment"}
        db.session.commit()

        def the_owner_configures_the_survivor():
            db.session.get(SearchProfile, TARGET_ID).ai_config = {
                "market_context": "just set by the owner"
            }

        _plan_then(monkeypatch, the_owner_configures_the_survivor)

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"
        assert any("ai_config" in message for message in report["errors"])

        db.session.expire_all()
        assert db.session.get(SearchProfile, TARGET_ID).ai_config == {
            "market_context": "just set by the owner"
        }, "the setting made after planning must survive"
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "nothing was committed"


def test_a_profile_carrying_a_subscription_key_is_never_deleted(fragmented):
    """#110 gives a profile a saved-search identity; deleting it destroys one.

    `merge_duplicate_profiles()` already refuses to lose a key when it removes
    a duplicate, so the repository has settled that this is not acceptable
    collateral. This repair does not carry keys around -- that is the merge's
    job, and the unique index and the default-profile CHECK make it a decision
    with consequences -- so it declines to delete the profile instead.
    """
    if not hasattr(SearchProfile, "source_search_key"):
        pytest.skip("#110 (saved-search identity) is not present in this tree")

    with fragmented.app_context():
        db.session.get(SearchProfile, 9).source_search_key = "k" * 20
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied"
        assert sorted(report["profiles_deleted"]) == [7, 10]
        assert db.session.get(SearchProfile, 9) is not None, (
            "a profile carrying a subscription key must survive the repair"
        )
        assert {
            "profile_id": 9,
            "remaining": 0,
            "reason": "carries a saved-search identity key",
        } in report["profiles_retained"]

        # Its listings still moved; only the empty row is kept.
        assert _count(9) == 0
        assert _count(TARGET_ID) == TOTAL_LISTINGS


def test_a_subscription_key_appearing_after_planning_aborts_the_repair(
    fragmented, monkeypatch
):
    """A key assigned between planning and applying must not be deleted over."""
    if not hasattr(SearchProfile, "source_search_key"):
        pytest.skip("#110 (saved-search identity) is not present in this tree")

    with fragmented.app_context():

        def ingestion_claims_the_fragment():
            db.session.get(SearchProfile, 9).source_search_key = "k" * 20

        _plan_then(monkeypatch, ingestion_claims_the_fragment)

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"
        assert any("source_search_key" in m for m in report["errors"])

        db.session.expire_all()
        assert db.session.get(SearchProfile, 9) is not None
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "nothing was committed"


def test_the_repair_locks_every_profile_it_touches():
    """Pin that the lock is actually requested.

    SQLite ignores `FOR UPDATE`, so this suite cannot demonstrate the locking
    itself -- only that the statement asks for it. The semantics come from
    Postgres, the same honest limitation as the foreign-key lock.
    """
    from sqlalchemy.dialects import postgresql

    from services.search_profile_repair_service import _lock_profiles_statement

    sql = str(_lock_profiles_statement([7, 8, 9]).compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in sql
    assert "search_profiles" in sql


def test_a_fragment_made_default_after_planning_aborts_the_repair(
    fragmented, monkeypatch
):
    """A profile that became the default must not be deleted as an empty one."""
    with fragmented.app_context():

        def promote_to_default():
            db.session.get(SearchProfile, 9).is_default = True

        _plan_then(monkeypatch, promote_to_default)

        report = SearchProfileRepairService.apply()

        assert report["status"] == "mismatch"
        assert any("default" in m for m in report["errors"])

        db.session.expire_all()
        assert db.session.get(SearchProfile, 9) is not None, (
            "the new default profile must not be deleted"
        )
        assert _count(TARGET_ID) == FRAGMENTS[TARGET_ID][2], "nothing was committed"


def test_the_default_profile_is_never_deleted_or_duplicated(app):
    """One default in, one default out.

    A default fragment feeding two saved searches would hand `is_default` to
    both survivors, and `get_default_profile()` then has two answers. Nor may
    the fragment itself be deleted once emptied: that is the owner's default,
    and issue #110 adds a CHECK that forbids a keyed profile from carrying
    the flag anyway.
    """
    first = "alpha beta"
    second = "alpha gamma"

    with app.app_context():
        db.session.add_all(
            [
                SearchProfile(id=1, name="alpha", is_active=True, is_default=True),
                SearchProfile(id=2, name=first, is_active=True, is_default=False),
                SearchProfile(id=3, name=second, is_active=True, is_default=False),
            ]
        )
        _add_listings(1, _folded(first), 2, "one")
        _add_listings(1, _folded(second), 3, "two")
        db.session.commit()

        report = SearchProfileRepairService.apply()

        assert report["status"] == "applied"
        defaults = sorted(
            profile.id
            for profile in SearchProfile.query.all()
            if bool(profile.is_default)
        )
        assert defaults == [1], "exactly one default, and still the owner's"

        assert db.session.get(SearchProfile, 1) is not None, (
            "the default profile is never deleted, even once it is empty"
        )
        assert _count(1) == 0
        assert _count(2) == 2
        assert _count(3) == 3
        assert report["profiles_deleted"] == []
