"""Repair saved-search profiles that a folded MIME subject split in two.

#101 fixed the cause: a long `Subject` header arrives folded (RFC 5322 2.2.3)
and every saved-search pattern in `extract_search_name()` stops at the CR, so
the same subscription produced a different profile name depending on how long
the subject prefix happened to be. This module repairs the rows already in the
database:

    id 7  | houses at your custom search            | 13 listings
    id 8  | houses at your custom search area norte |  3 listings
    id 9  | houses at your custom search area       | 24 listings
    id 10 | houses at your custom                   |  1 listing

No heuristics are involved, and deliberately so. `properties.email_subject`
holds the subject exactly as it was stored, folds included, so the correct
name is *recomputed* from data already on disk with the same two primitives
ingestion uses -- `unfold_header()` then `extract_search_name()`. Prefix
matching, token similarity and fuzzy merging are all rejected: they cannot
tell a truncated name from a genuinely narrower saved search ("Homes in
Ciudad Quesada" versus "Homes in Ciudad Quesada Norte") and would silently
collapse two real subscriptions into one.

What counts as a fold fragment
------------------------------

Only a *fold fragment* is ever emptied or renamed, and a profile qualifies by
**replaying the bug**, not by resembling its victims. For a saved search N,
profile P is a fold fragment of N when:

1. P's name is not N; and
2. running the **pre-#101 extractor** over every listing of N inside P -- the
   same `extract_search_name()`, on the stored subject, *without* unfolding,
   so its ``[^\\r\\n]`` classes truncate at the CR exactly as they used to --
   returns precisely the name P carries, for all of them.

That is cause and effect: this profile's name was produced by this bug, on
these rows. Weaker signals were tried and are not enough. Two profiles
holding one saved search prove nothing -- `ProfileAssignmentService` files
listings by location, so "Coast" and "City" legitimately split one
subscription. A line break in the subject proves nothing either: it can sit
past the end of the name (``"...: Alpha Beta Gamma!\\r\\n extra"``), where it
truncated nothing at all.

There is deliberately no additional "the name must be a word-boundary prefix"
clause. It was an approximation of the replay, and as a second belt it only
subtracts: a fold landing on punctuation
(``"...: Homes in Ciudad Quesada,\\r\\n Alicante!"``) yields a name the
cleaner strips back to "Homes in Ciudad Quesada", which is *not* a
word-boundary prefix of "Homes in Ciudad Quesada, Alicante" -- yet the replay
proves it is a genuine fragment.

One consequence, and it is the right one: since #101 stores subjects
unfolded, **this repair can only ever act on rows written before that fix**.
Those rows are precisely the damage. Anything ingested afterwards is
untouchable by construction.

What the repair will never do
-----------------------------

Anything that is not a fold fragment is somebody's decision, and is left
alone:

- **It never moves a listing you reassigned by hand.** A listing whose
  ``enrichment.profile_assignment.manual_override`` is set -- what the
  profile-change form writes, and what `ProfileAssignmentService` already
  refuses to override -- stays exactly where you put it, and the profile
  holding it is therefore never empty and never deleted. Pinned listings
  still count towards what a profile holds, so they can stop a rename.
  Pinning is re-read inside the transaction too: a listing pinned or moved
  *after* planning aborts the repair rather than being dragged back.
- **It never renames a profile that is not a fold fragment**, and never
  renames one that holds a second saved search, whether it already did or the
  plan itself moved one in.
- **It never moves listings out of a profile that is not a fold fragment.**
  They stay, and the report lists them under "left alone".
- **It never gives one profile to two saved searches.**
- **It never deletes a profile that still holds a listing**, for any reason:
  a pinned listing, a subject whose name cannot be recomputed, another
  subscription.
- **It never deletes the default profile, and never makes another profile
  the default.** One default goes in and the same one comes out, even if this
  repair empties it -- the app assumes a single default, and which one it is
  is the owner's call.
- **It never deletes a profile carrying a saved-search identity key** (#110),
  and never moves a key between profiles. `merge_duplicate_profiles()` owns
  that decision -- it has a unique index and the default-profile CHECK behind
  it -- so an emptied profile that still holds a key is kept and reported
  rather than removed.
- **It never creates a profile**, and **never adopts a listing that is
  already orphaned** -- both are reported, neither is acted on.
- **It never touches a listing whose saved-search name cannot be recomputed.**

One thing it *does* do, stated plainly so it is not mistaken for a promise:
it moves listings into the profile that already carries their name even when
that profile also holds a stray listing of some other saved search. Ingestion
routes those listings to exactly that profile anyway, so the repair reaches no
state the system would not reach on its own -- and the stray's own group is
reported BLOCKED rather than hidden. The rule being protected is that a
profile is never *renamed* out from under what it holds.

A BLOCKED group means nothing about it was changed. It does not affect the
exit code; read the report.

Deleting a profile is the dangerous half. `properties.search_profile_id` is
``ON DELETE SET NULL``: a listing that lands in a fragment while it is being
removed is not rejected, it is silently orphaned with a NULL profile.

What the code guarantees on its own, with another writer running
--------------------------------------------------------------

- **All or nothing.** Renames, reassignments and deletes are one transaction.
- **No listing is silently orphaned.** Each profile is re-counted immediately
  before its ``DELETE``; the deletes are then *flushed inside* the
  transaction, and every touched profile plus the total number of
  NULL-profile listings is re-counted again afterwards. A row that arrived
  after the zero-check is nullified by that same flush and is caught by the
  re-count that follows it, which aborts and rolls back the whole repair.
  Between that flush and the ``COMMIT`` the database itself closes the
  window: the pending ``DELETE`` holds the ``search_profiles`` row, and an
  ``INSERT`` into ``properties`` referencing it needs a ``FOR KEY SHARE``
  lock on that same row, so a concurrent ingestion waits for this
  transaction and then fails its foreign key rather than slipping in.
- **Every profile the plan touches is locked, then re-checked, before any of
  it is applied.** The rows are taken ``FOR UPDATE`` in id order and held to
  the end of the transaction, so nothing can change them between the check
  and the write. Each is then re-read -- by column, so a stale object from
  planning cannot answer -- and *every* field a decision came from is
  compared with what the plan read: the name, the default flag, and each
  setting `_plan_settings_merge()` used to build its updates. Each
  reassignment also names the profile the row is expected to be in, and every
  planned row's pinned state is read again after the moves. A profile
  renamed, made default or reconfigured, a listing moved or pinned in
  between: each aborts before COMMIT.

  The lock is Postgres semantics. SQLite ignores ``FOR UPDATE`` -- and
  serialises writers anyway -- so the test suite pins that the lock is
  *requested* but cannot demonstrate it, the same honest limitation as the
  foreign-key lock below.
- **The counts in the report were verified**, twice, inside the transaction.
- **The exit code does not overstate what is known** (table below).

What still rests on stopping ingestion
--------------------------------------

Not safety -- *success*. A concurrent write does not corrupt anything, it
makes the repair abort with exit 1 and no changes, so the run has to be
repeated. Stopping ingestion is also what keeps the concurrent ingestion
itself from failing on a foreign key. And the tool cannot stop the
scheduler for you: it only refuses to start one of its own.

**Apply with ingestion stopped** (`docker compose stop app`, which stops the
in-process scheduler); see the "Repairing search profiles fragmented by
folded subjects" section of MIGRATION_RUNBOOK.md.

Dry run (default) writes nothing::

    docker compose run --rm app python -m services.search_profile_repair_service

Apply::

    docker compose run --rm app python -m services.search_profile_repair_service --apply

Exit codes say exactly what is known about the database:

===  =============================  ======================================
  0  clean / pending / applied      either nothing needed doing, or the
                                    repair committed and verified
  1  mismatch                       it failed **before COMMIT**; nothing
                                    was committed, the database is
                                    untouched
  2  applied_report_unavailable     **the repair was committed** and only
                                    the after-report could not be built
  3  commit_outcome_unknown         COMMIT itself did not complete cleanly,
                                    so the outcome is **unknown**: the
                                    server may have applied it and lost the
                                    connection before acknowledging
===  =============================  ======================================

Only 1 licenses the conclusion "the database is untouched", and it is used
only for failures that happen before COMMIT is sent. 3 is deliberately not
folded into it: a raised COMMIT is unknowable from the client, and telling
the owner it rolled back is exactly the wrong thing to say before they
decide whether to re-run. The report carries a best-effort read-back under
`post_commit_observation`, which is an observation and not a verdict.

One run is the whole run. Planning is done against a running projection of
the renames and moves already decided -- names as they will read, profiles as
they will be -- and groups are re-examined until a whole pass produces
nothing new. So a profile that *gains* a second saved search mid-plan blocks
too, one the plan *empties* stops blocking, and how much gets repaired never
depends on alphabetical order. Re-running only ever finds what genuinely
changed in the database since.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from sqlalchemy import func, select

from app import db
from models import Property, SearchProfile

# `_canonical_profile_name` is private, and imported anyway: it is the exact
# comparison `SearchProfileService.get_or_create_profile_by_name()` uses to
# decide that an incoming name already has a profile. Re-implementing it here
# would let the repair and the ingestion drift apart, which is the whole class
# of bug this module exists to clean up. services/search_profile_service.py is
# deliberately left untouched (issue #103 scope, and #102 owns that file).
from services.search_profile_service import (
    _canonical_profile_name,
    extract_search_name,
    normalize_travel_targets_config,
)
from utils.email_headers import unfold_header

logger = logging.getLogger(__name__)

# Per-profile JSON configuration. Nothing on the four live fragments has any of
# it set, but a future fragment may, and a merge must never be the reason a
# setting disappears.
MERGEABLE_JSON_FIELDS = (
    "email_matchers",
    "classification_rules",
    "scoring_config",
    "ai_config",
    "ui_config",
)

# Every profile column a decision is taken from: the name the plan matched and
# renames, the flag that decides what may be deleted, and everything
# `_plan_settings_merge()` reads to build `group.updates`. All of it is
# snapshotted at planning time and compared again before anything is written,
# because a plan applied from stale settings overwrites whatever was set in
# between.
VERIFIED_PROFILE_FIELDS = (
    "name",
    "is_default",
    "is_active",
    "description",
    "travel_targets",
) + MERGEABLE_JSON_FIELDS

# The saved-search identity from #110, when the model carries it. A profile
# holding one is never deleted here, so a key appearing between planning and
# applying has to abort the run. Looked up rather than assumed, so this module
# still imports in a tree where #110 has not landed.
if hasattr(SearchProfile, "source_search_key"):
    VERIFIED_PROFILE_FIELDS += ("source_search_key",)

# Columns whose stored value may be None but whose meaning is boolean.
BOOLEAN_PROFILE_FIELDS = frozenset({"is_default", "is_active"})

# Bulk UPDATE ... WHERE id IN (...) is chunked so the parameter count stays
# well under every backend's limit regardless of how large a group gets.
UPDATE_CHUNK = 500

# Exit code per status. 1 is reserved for "nothing was committed, the database
# is untouched" -- a rollback is the one thing the operator must be able to
# read straight off the exit code. A repair that committed and then failed to
# report gets its own code, because it is the opposite situation.
EXIT_CODES = {
    "clean": 0,
    "pending": 0,
    "applied": 0,
    "mismatch": 1,
    "applied_report_unavailable": 2,
    "commit_outcome_unknown": 3,
}


def _remaining_property_count(profile_id: Optional[int]) -> int:
    """Properties currently pointing at `profile_id` (None means orphaned).

    Module-level on purpose: this single query is the guard that stands
    between the reassignment and the ``DELETE``, and tests fault-inject it to
    prove the repair aborts rather than orphaning rows.
    """
    return Property.query.filter_by(search_profile_id=profile_id).count()


def _profile_property_counts() -> Dict[Optional[int], int]:
    """`{search_profile_id: listings}` for the whole table, NULL included."""
    rows = (
        db.session.query(Property.search_profile_id, func.count(Property.id))
        .group_by(Property.search_profile_id)
        .all()
    )
    return {profile_id: int(total) for profile_id, total in rows}


def _pre_fix_profile_name(subject: Any) -> Optional[str]:
    """The name the *pre-#101* extractor would have produced from this subject.

    Before #101 the subject was never unfolded, so it reached
    `extract_search_name()` with the fold intact and every pattern's
    ``[^\\r\\n]`` class truncated the name at the CR. Running the same
    extractor on the stored subject *without* unfolding therefore replays the
    bug exactly. Comparing the result with a profile's name answers the only
    question that matters -- was this profile's name produced by this bug, on
    these very rows -- instead of inferring it from the shape of the name.
    """
    return extract_search_name(str(subject or ""), "")


def _is_manually_pinned(enrichment: Any) -> bool:
    """True when the owner reassigned this listing through the profile form.

    `routes/main_routes.py` writes `manual_override` into
    `enrichment.profile_assignment`, and `ProfileAssignmentService` already
    refuses to move such a row. So does this repair.
    """
    if not isinstance(enrichment, dict):
        return False
    assignment = enrichment.get("profile_assignment")
    return bool(isinstance(assignment, dict) and assignment.get("manual_override"))


def _is_set(value: Any) -> bool:
    """True when a JSON config column actually carries a configuration."""
    if value is None:
        return False
    if isinstance(value, (dict, list, str)):
        return bool(value)
    return True


def _chunks(values: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _custom_target_key(item: Dict[str, Any]) -> tuple:
    return (
        str(item.get("name") or "").strip().lower(),
        item.get("lat"),
        item.get("lon"),
    )


@dataclass
class GroupPlan:
    """One saved search: where its listings live now and where they belong."""

    name: str
    target_id: int
    target_name: str
    rename_to: Optional[str]
    fragment_ids: List[int]
    property_ids: Dict[int, List[int]]
    updates: Dict[str, Any] = field(default_factory=dict)
    settings_preserved: List[Dict[str, Any]] = field(default_factory=list)
    settings_conflicts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def moves(self) -> int:
        return sum(len(self.property_ids[pid]) for pid in self.fragment_ids)


@dataclass
class RepairPlan:
    groups: List[GroupPlan] = field(default_factory=list)
    blocked_groups: List[Dict[str, Any]] = field(default_factory=list)
    counts_before: Dict[Optional[int], int] = field(default_factory=dict)
    expected_after: Dict[int, int] = field(default_factory=dict)
    profiles_to_delete: List[int] = field(default_factory=list)
    profiles_retained: List[Dict[str, int]] = field(default_factory=list)
    profile_names: Dict[int, str] = field(default_factory=dict)
    profile_defaults: Dict[int, bool] = field(default_factory=dict)
    profile_snapshot: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    left_in_place: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_properties: int = 0
    orphan_properties: int = 0
    manually_pinned_properties: int = 0

    @property
    def properties_to_move(self) -> int:
        return sum(group.moves for group in self.groups)


def _find_profile_by_name(
    name: str,
    canonical: str,
    profiles: Sequence[SearchProfile],
    effective_names: Dict[int, str],
) -> Optional[SearchProfile]:
    """The profile ingestion would reuse for `name`, or None.

    Mirrors `SearchProfileService.get_or_create_profile_by_name()` lookup
    order -- exact name first, then canonical -- minus the create half, which
    a dry run must never trigger.

    Matches on `effective_names`, not on `profile.name`: an earlier group in
    the same plan may already have renamed a profile, and a rename *frees* the
    name it used to hold. Looking that freed name up in the pre-repair state
    hands the renamed profile to a second saved search and merges the two.
    """
    for profile in profiles:
        if effective_names.get(profile.id) == name:
            return profile
    if not canonical:  # pragma: no cover - callers pass a canonicalized name
        return None
    for profile in profiles:
        current = effective_names.get(profile.id)
        if current and _canonical_profile_name(current) == canonical:
            return profile
    return None


def _plan_settings_merge(
    target: SearchProfile, fragments: Sequence[SearchProfile]
) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Which of the fragments' settings the survivor should adopt.

    Returns `(updates, preserved, conflicts)` and mutates nothing, so the dry
    run can show exactly what an apply would carry over. A value the target
    already has always wins; a fragment value that loses is reported as a
    conflict rather than dropped silently.
    """
    updates: Dict[str, Any] = {}
    preserved: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    for name in MERGEABLE_JSON_FIELDS:
        taken = _is_set(getattr(target, name))
        for fragment in fragments:
            value = getattr(fragment, name)
            if not _is_set(value):
                continue
            if taken:
                conflicts.append({"profile_id": fragment.id, "field": name})
                continue
            # Deep-copied: the survivor must not end up sharing a mutable JSON
            # container with a row that is about to be deleted.
            updates[name] = copy.deepcopy(value)
            preserved.append({"profile_id": fragment.id, "field": name})
            taken = True

    # travel_targets is the one column with a real union: custom destinations
    # are additive, exactly as SearchProfileService.merge_duplicate_profiles()
    # treats them. Presets are per-profile toggles and are left to the target.
    merged = normalize_travel_targets_config(target.travel_targets)
    custom = list(merged.get("custom") or [])
    seen = {_custom_target_key(item) for item in custom}
    donors: List[int] = []
    for fragment in fragments:
        fragment_custom = (
            normalize_travel_targets_config(fragment.travel_targets).get("custom") or []
        )
        for item in fragment_custom:
            key = _custom_target_key(item)
            if key in seen:
                continue
            custom.append(copy.deepcopy(item))
            seen.add(key)
            if fragment.id not in donors:
                donors.append(fragment.id)
    if donors:
        merged["custom"] = custom
        updates["travel_targets"] = normalize_travel_targets_config(merged)
        preserved.extend(
            {"profile_id": donor, "field": "travel_targets"} for donor in donors
        )

    # `is_default` is deliberately NOT carried over. It is one global setting,
    # not a per-profile preference to merge: a default fragment feeding two
    # saved searches would hand the flag to both survivors and
    # get_default_profile() would have two answers. The owner's default is
    # left where it is -- and a default profile is never deleted, so it stays
    # reachable even once this repair has emptied it. (Issue #110 adds a CHECK
    # forbidding a keyed profile from carrying the flag, which this also
    # respects.)
    for fragment in fragments:
        if fragment.is_active and not target.is_active and "is_active" not in updates:
            updates["is_active"] = True
            preserved.append({"profile_id": fragment.id, "field": "is_active"})
        if (
            not (target.description or "").strip()
            and (fragment.description or "").strip()
            and "description" not in updates
        ):
            updates["description"] = fragment.description
            preserved.append({"profile_id": fragment.id, "field": "description"})

    return updates, preserved, conflicts


class _Planner:
    """Decides the whole repair, re-examining groups until nothing more moves.

    Every decision is taken against a *projection* of the decisions already
    made -- names as they will read, contents as they will be -- because a
    rename frees a name and a move changes what a profile holds. And because
    one group's move can unblock another, groups are retried until a whole
    pass produces nothing new; otherwise how much gets repaired would depend
    on alphabetical order.
    """

    def __init__(self, plan: RepairPlan, profiles: Sequence[SearchProfile]):
        self.plan = plan
        self.profiles = list(profiles)
        self.by_id = {profile.id: profile for profile in profiles}
        # {canonical name: {current profile id: [movable property ids]}}
        self.grouped: Dict[str, Dict[int, List[int]]] = {}
        # {canonical name: {exact spelling: how many listings use it}}
        self.spellings: Dict[str, Dict[str, int]] = {}
        self.display_names: Dict[str, str] = {}
        # {profile id: every canonical name inside it}, pinned rows included:
        # they are exactly what makes a rename wrong.
        self.projected_names: Dict[int, set] = {}
        self.effective_names: Dict[int, str] = dict(plan.profile_names)
        # A target belongs to one group. The eligibility rule already makes a
        # second claim impossible, but this operation deletes rows, so the
        # reservation is written down rather than left to hold by argument.
        self.claimed_by: Dict[int, str] = {}
        self.moved_out: Dict[int, int] = {}
        self.moved_in: Dict[int, int] = {}
        # {(profile id, canonical name): every name the pre-#101 extractor
        # would have produced from those listings}. Pinned rows are counted
        # too: the question is what broke this profile's *name*, not what may
        # move. {profile id: listings whose name cannot be recomputed at all}.
        self.replayed: Dict[tuple, set] = {}
        self.unresolved_by_profile: Dict[int, int] = {}
        self.left_in_place: Dict[tuple, Dict[str, Any]] = {}
        # {(profile id, canonical name): listings the profile will still hold
        # once the plan runs}. Pinned rows never leave, so a fragment can give
        # up its movable listings and still hold that saved search -- which is
        # what decides whether the name may drop out of the projection.
        self.remaining: Dict[tuple, int] = {}

    def add_unresolved(self, profile_id: int) -> None:
        """A listing in this profile whose saved-search name cannot be read."""
        self.unresolved_by_profile[profile_id] = (
            self.unresolved_by_profile.get(profile_id, 0) + 1
        )

    def add_listing(
        self,
        property_id: int,
        profile_id: int,
        name: str,
        canonical: str,
        pinned: bool,
        replayed: Optional[str],
    ) -> None:
        self.projected_names.setdefault(profile_id, set()).add(canonical)
        counts = self.spellings.setdefault(canonical, {})
        counts[name] = counts.get(name, 0) + 1
        # Canonical, so the comparison against a profile's name matches the
        # equality ingestion itself uses.
        self.replayed.setdefault((profile_id, canonical), set()).add(
            _canonical_profile_name(replayed) if replayed else None
        )
        key = (profile_id, canonical)
        self.remaining[key] = self.remaining.get(key, 0) + 1
        if pinned:
            # Owner's choice. It still counts towards what the profile holds --
            # so it can stop a rename and keep the profile from being deleted --
            # but nothing in this tool may move it.
            self.plan.manually_pinned_properties += 1
            return
        self.grouped.setdefault(canonical, {}).setdefault(profile_id, []).append(
            property_id
        )

    def is_fold_fragment(self, profile_id: int, canonical: str) -> bool:
        """Is this profile a *fold fragment* of `canonical`, or someone's choice?

        Two profiles holding one saved search prove nothing on their own --
        `ProfileAssignmentService` files listings by location, so "Coast" and
        "City" can legitimately split one subscription between them. Nor does
        a line break somewhere in the subject: it can sit past the end of the
        name, where it truncated nothing.

        So the bug is replayed instead of characterised. Every listing of this
        search inside the profile is run through the pre-#101 extractor, and
        the profile qualifies only when all of them come back with exactly the
        name it carries. That is cause and effect -- this profile's name was
        produced by this bug on these rows -- rather than a family resemblance.

        Deliberately *not* also requiring the name to be a word-boundary
        prefix. It was an approximation of this check, and as a second belt it
        only subtracts: a fold landing on punctuation ("Homes in Ciudad
        Quesada,\\r\\n Alicante!") leaves a name the cleaner strips back to
        "Homes in Ciudad Quesada", which is not a prefix of "Homes in Ciudad
        Quesada, Alicante" at a word boundary -- yet the replay proves it is a
        real fragment.
        """
        current = self.effective_names.get(profile_id)
        if not current:
            return False
        current_canonical = _canonical_profile_name(current)
        if not current_canonical or current_canonical == canonical:
            return False
        replayed = self.replayed.get((profile_id, canonical))
        return bool(replayed) and replayed == {current_canonical}

    def run(self) -> None:
        # The spelling most of a group's listings use, ties alphabetical.
        self.display_names = {
            canonical: min(counts.items(), key=lambda item: (-item[1], item[0]))[0]
            for canonical, counts in self.spellings.items()
        }

        pending = sorted(self.grouped)
        blocked: Dict[str, Dict[str, Any]] = {}
        while pending:
            still_blocked: List[str] = []
            for canonical in pending:
                reason = self._attempt(canonical)
                if reason is None:
                    blocked.pop(canonical, None)
                else:
                    blocked[canonical] = reason
                    still_blocked.append(canonical)
            if len(still_blocked) == len(pending):
                break  # a whole pass changed nothing: this is the fixed point
            pending = still_blocked

        self.plan.blocked_groups = [blocked[key] for key in sorted(blocked)]
        self.plan.left_in_place = [
            self.left_in_place[key] for key in sorted(self.left_in_place)
        ]

    def _block(
        self, canonical: str, property_ids: Dict[int, List[int]], reason: str
    ) -> Dict[str, Any]:
        return {
            "name": self.display_names[canonical],
            "candidate_ids": sorted(property_ids),
            "other_names": sorted(
                {
                    self.display_names.get(other, other)
                    for pid in property_ids
                    for other in self.projected_names.get(pid, set())
                    if other != canonical
                }
            ),
            "reason": reason,
        }

    def _attempt(self, canonical: str) -> Optional[Dict[str, Any]]:
        """Plan one group. Returns None when planned, else why it is blocked."""
        property_ids = self.grouped[canonical]
        name = self.display_names[canonical]
        target = _find_profile_by_name(
            name, canonical, self.profiles, self.effective_names
        )
        rename_to: Optional[str] = None

        # Only a proven fold fragment may give up its listings or its name.
        # Everything else holding this saved search was filled deliberately,
        # and is left exactly as it is.
        fragments_here = [
            pid for pid in property_ids if self.is_fold_fragment(pid, canonical)
        ]

        if target is None:
            if not fragments_here:
                return self._block(
                    canonical,
                    property_ids,
                    "no profile carries this name, and none of the profiles "
                    "holding its listings is a fold fragment of it -- replaying "
                    "the pre-#101 extractor over their subjects does not produce "
                    "the name any of them carries, so this bug did not name "
                    "them. These look like deliberate placements, not damage",
                )
            # Promote the fragment with the most listings rather than create a
            # profile, so its settings survive; the ordering is the one
            # merge_duplicate_profiles() already uses.
            #
            # A rename must prove the profile will hold *nothing but* this
            # saved search: every row it keeps resolves to this name
            # (projected_names) and none of its rows is unreadable
            # (unresolved_by_profile). Together those say its total row count
            # equals its count for this name -- listings whose name cannot be
            # recomputed never enter the projection, so without the second
            # clause a fragment carrying one looked pure and got renamed while
            # still holding somebody else's row.
            candidates = [
                self.by_id[pid]
                for pid in fragments_here
                if pid in self.by_id
                and self.projected_names.get(pid) == {canonical}
                and not self.unresolved_by_profile.get(pid)
            ]
            if not candidates:
                return self._block(
                    canonical,
                    property_ids,
                    "no profile carries this name, and every fold fragment of "
                    "it would still hold something else afterwards -- another "
                    "saved search, or a listing whose name cannot be read. "
                    "Renaming one would mislabel what stays behind",
                )
            target = sorted(
                candidates,
                key=lambda p: (not bool(p.is_default), -len(property_ids[p.id]), p.id),
            )[0]
            rename_to = name
        # A profile that does exist keeps its own spelling: ingestion's
        # get_or_create_profile_by_name() reuses a canonical match without
        # renaming it, and this repair follows ingestion rather than tidying
        # names behind the owner's back.

        owner = self.claimed_by.get(target.id)
        if owner is not None and owner != canonical:
            return self._block(  # pragma: no cover - defensive
                canonical,
                property_ids,
                f"profile {target.id} is already the survivor of another saved "
                f"search in this plan; refusing to give one profile to two "
                f"subscriptions",
            )

        fragment_ids = sorted(pid for pid in fragments_here if pid != target.id)
        for profile_id in sorted(property_ids):
            if profile_id == target.id or profile_id in fragment_ids:
                continue
            # Holds this saved search but is not a fold fragment of it. Its
            # listings stay; the operator is told rather than left guessing
            # why the counts do not add up to the whole subscription.
            self.left_in_place[(profile_id, canonical)] = {
                "profile_id": profile_id,
                "name": self.effective_names.get(profile_id),
                "saved_search": name,
                "listings": len(property_ids[profile_id]),
            }
        if not fragment_ids and not rename_to:
            return None  # nothing here is repairable

        # Book this group's effects before the next group is decided.
        self.claimed_by[target.id] = canonical
        if rename_to:
            self.effective_names[target.id] = rename_to
        for fragment_id in fragment_ids:
            leaving = len(property_ids[fragment_id])
            key = (fragment_id, canonical)
            self.remaining[key] = self.remaining.get(key, 0) - leaving
            # The name drops out of the projection only when nothing of this
            # saved search is left. Pinned listings do not move, so a fragment
            # that had one still holds the search afterwards -- and renaming it
            # after some *other* search would strand that listing under a name
            # that is not its own.
            if self.remaining[key] <= 0:
                self.projected_names.get(fragment_id, set()).discard(canonical)
            # ...while the target gains them, and from here on counts as
            # holding this saved search even if it held none a moment ago.
            target_key = (target.id, canonical)
            self.remaining[target_key] = self.remaining.get(target_key, 0) + leaving
        self.projected_names.setdefault(target.id, set()).add(canonical)

        group = GroupPlan(
            name=name,
            target_id=target.id,
            target_name=target.name,
            rename_to=rename_to,
            fragment_ids=fragment_ids,
            property_ids={pid: sorted(ids) for pid, ids in property_ids.items()},
        )
        fragments = [self.by_id[pid] for pid in fragment_ids if pid in self.by_id]
        (
            group.updates,
            group.settings_preserved,
            group.settings_conflicts,
        ) = _plan_settings_merge(target, fragments)
        self.plan.groups.append(group)

        for fragment_id in fragment_ids:
            leaving = len(property_ids[fragment_id])
            self.moved_out[fragment_id] = self.moved_out.get(fragment_id, 0) + leaving
            self.moved_in[target.id] = self.moved_in.get(target.id, 0) + leaving
        return None


def build_plan() -> RepairPlan:
    """Work out the whole repair without touching a single row."""
    plan = RepairPlan()
    profiles = SearchProfile.query.order_by(SearchProfile.id.asc()).all()
    # Deep-copied: the JSON columns are mutable containers, and a snapshot that
    # aliases them would compare equal to itself no matter what changed.
    plan.profile_snapshot = {
        profile.id: {
            field_name: copy.deepcopy(getattr(profile, field_name))
            for field_name in VERIFIED_PROFILE_FIELDS
        }
        for profile in profiles
    }
    plan.profile_names = {
        profile_id: snapshot["name"]
        for profile_id, snapshot in plan.profile_snapshot.items()
    }
    plan.profile_defaults = {
        profile_id: bool(snapshot["is_default"])
        for profile_id, snapshot in plan.profile_snapshot.items()
    }
    plan.counts_before = _profile_property_counts()

    # Everything is keyed by *canonical* name. "ALPHA" and "Alpha" are one
    # saved search as far as ingestion is concerned -- get_or_create_profile_
    # by_name() hands both to the same profile -- so planning them as two
    # groups would give one target two independent settings merges, each
    # reading the target's original state, and the second would silently
    # overwrite the first.
    planner = _Planner(plan, profiles)
    rows = (
        db.session.query(
            Property.id,
            Property.search_profile_id,
            Property.email_subject,
            Property.enrichment,
        )
        .order_by(Property.id.asc())
        .all()
    )
    for property_id, profile_id, subject, enrichment in rows:
        name = SearchProfileRepairService.recompute_profile_name(subject)
        if not name:
            plan.unresolved_properties += 1
            if profile_id is not None:
                planner.add_unresolved(profile_id)
            continue
        if profile_id is None:
            # Already orphaned (an earlier DELETE, or a manual edit). Reported,
            # never adopted: re-attaching rows is a separate decision from
            # de-fragmenting profiles, and the operator should see it first.
            plan.orphan_properties += 1
            continue
        canonical = _canonical_profile_name(name)
        if not canonical:  # pragma: no cover - extract_search_name cleans first
            plan.unresolved_properties += 1
            planner.add_unresolved(profile_id)
            continue
        planner.add_listing(
            property_id,
            profile_id,
            name,
            canonical,
            _is_manually_pinned(enrichment),
            _pre_fix_profile_name(subject),
        )

    planner.run()
    moved_out = planner.moved_out
    moved_in = planner.moved_in

    target_ids = {group.target_id for group in plan.groups}
    for profile_id in sorted(set(moved_out) | set(moved_in)):
        plan.expected_after[profile_id] = (
            plan.counts_before.get(profile_id, 0)
            - moved_out.get(profile_id, 0)
            + moved_in.get(profile_id, 0)
        )

    by_id = {profile.id: profile for profile in profiles}
    for profile_id in sorted(moved_out):
        remaining = plan.expected_after.get(profile_id, 0)
        if remaining:
            # A fragment that still holds listings after the move -- pinned
            # ones, a name that could not be recomputed, another saved search
            # -- is kept. Only an empty profile is safe to delete.
            plan.profiles_retained.append(
                {
                    "profile_id": profile_id,
                    "remaining": remaining,
                    "reason": "still holds listings",
                }
            )
        elif profile_id in target_ids:
            plan.profiles_retained.append(
                {
                    "profile_id": profile_id,
                    "remaining": remaining,
                    "reason": "is the survivor of another saved search",
                }
            )
        elif getattr(by_id.get(profile_id), "source_search_key", None):
            # #110 identity. merge_duplicate_profiles() carries a key over to
            # the survivor when it removes a duplicate; this repair does not
            # move keys around -- that decision has a unique index and the
            # default-profile CHECK behind it -- so it keeps the row instead of
            # destroying the identity.
            plan.profiles_retained.append(
                {
                    "profile_id": profile_id,
                    "remaining": remaining,
                    "reason": "carries a saved-search identity key",
                }
            )
        elif bool(getattr(by_id.get(profile_id), "is_default", False)):
            # The app assumes exactly one default profile. Emptying the
            # owner's default is fine; removing it is not this tool's call.
            plan.profiles_retained.append(
                {
                    "profile_id": profile_id,
                    "remaining": remaining,
                    "reason": "is the default profile",
                }
            )
        else:
            plan.profiles_to_delete.append(profile_id)

    return plan


def _counts_report(
    plan: RepairPlan, counts: Dict[Optional[int], int], names: Dict[int, str]
) -> List[Dict[str, Any]]:
    involved = set(plan.expected_after) | {group.target_id for group in plan.groups}
    report = [
        {
            "profile_id": profile_id,
            "name": names.get(profile_id),
            "properties": counts.get(profile_id, 0),
        }
        for profile_id in sorted(involved)
    ]
    if counts.get(None):
        report.append(
            {"profile_id": None, "name": None, "properties": counts.get(None, 0)}
        )
    return report


def _profile_field(field: str, value: Any) -> Any:
    """Normalise a column for comparison (a NULL boolean means False)."""
    return bool(value) if field in BOOLEAN_PROFILE_FIELDS else value


def _involved_profile_ids(plan: RepairPlan) -> List[int]:
    return sorted(
        {group.target_id for group in plan.groups}
        | {fid for group in plan.groups for fid in group.fragment_ids}
        | set(plan.profiles_to_delete)
    )


def _lock_profiles_statement(profile_ids: Sequence[int]):
    """``SELECT id FROM search_profiles WHERE id IN (...) FOR UPDATE``."""
    return (
        select(SearchProfile.id)
        .where(SearchProfile.id.in_(list(profile_ids)))
        .with_for_update()
    )


def _lock_profiles(profile_ids: Sequence[int]) -> None:
    """Hold every profile row the plan touches for the rest of the transaction.

    Without it the verification below is only a snapshot comparison: under
    READ COMMITTED another transaction can rename a fragment, or make it the
    default, in the gap between the check and the ``DELETE``, and the repair
    would remove it and report success -- contradicting two of the guarantees
    this module states. The lock closes that gap, and as a side effect closes
    the orphan window from the start rather than only from the delete flush,
    because an ``INSERT`` referencing a locked profile needs ``FOR KEY SHARE``
    on the same row.

    Locked in id order so two runs cannot deadlock against each other.

    Postgres semantics. SQLite ignores ``FOR UPDATE`` (and serialises writers
    anyway), so the test suite can pin that the lock is *requested* but cannot
    demonstrate it -- the same honest limitation as the foreign-key lock.
    """
    for chunk in _chunks(sorted(profile_ids), UPDATE_CHUNK):
        db.session.execute(_lock_profiles_statement(chunk)).all()


def _verify_profile_metadata(plan: RepairPlan) -> List[str]:
    """Re-read the profiles the plan touches and compare them with the snapshot.

    Every column a decision was taken from, not just the name: `is_default`
    decides what may be deleted, and the settings columns are what
    `_plan_settings_merge()` turned into `group.updates`. Applying a plan built
    from stale settings silently overwrites whatever was set in between, which
    no count or pin check would notice.

    Reads columns rather than entities on purpose: an ORM `get()` can hand back
    the instance loaded during planning, which is exactly the stale answer this
    is trying to catch.
    """
    involved = _involved_profile_ids(plan)
    columns = [getattr(SearchProfile, field) for field in VERIFIED_PROFILE_FIELDS]
    problems: List[str] = []
    for chunk in _chunks(involved, UPDATE_CHUNK):
        rows = (
            db.session.query(SearchProfile.id, *columns)
            .filter(SearchProfile.id.in_(chunk))
            .all()
        )
        found = {row[0] for row in rows}
        for row in rows:
            profile_id = row[0]
            planned = plan.profile_snapshot.get(profile_id, {})
            for field_name, value in zip(VERIFIED_PROFILE_FIELDS, row[1:]):
                current = _profile_field(field_name, value)
                expected = _profile_field(field_name, planned.get(field_name))
                if current != expected:
                    problems.append(
                        f"profile {profile_id} had its {field_name} changed "
                        f"after planning: the plan read {expected!r}, the "
                        f"database now says {current!r}"
                    )
        for profile_id in chunk:
            if profile_id not in found:
                problems.append(f"profile {profile_id} disappeared after planning")
    return problems


def _pinned_among(property_ids: Sequence[int]) -> List[int]:
    """Which of these listings are pinned by hand *now*.

    Planning read `enrichment` at snapshot time; the owner can pin a listing
    through the profile form a moment later, without moving it, and no
    portable SQL predicate expresses that. So it is re-read inside the
    transaction, after the moves and before anything is deleted.
    """
    pinned: List[int] = []
    for chunk in _chunks(list(property_ids), UPDATE_CHUNK):
        rows = (
            db.session.query(Property.id, Property.enrichment)
            .filter(Property.id.in_(chunk))
            .all()
        )
        pinned.extend(
            property_id
            for property_id, enrichment in rows
            if _is_manually_pinned(enrichment)
        )
    return sorted(pinned)


def _verify_counts(plan: RepairPlan, orphans_before: int) -> List[str]:
    """Re-read every touched profile and compare it with the plan.

    Run twice inside the transaction -- once after the reassignment, once
    after the DELETEs are flushed -- because a concurrent writer can land
    between the two and the second pass is the only thing that sees it.
    """
    problems: List[str] = []
    for profile_id, expected in plan.expected_after.items():
        actual = _remaining_property_count(profile_id)
        if actual != expected:
            problems.append(
                f"profile {profile_id}: expected {expected} listings, found {actual}"
            )
    # Every NULL-profile row, not just the ones with a recoverable name: the
    # point is that the repair created none.
    orphans_after = _remaining_property_count(None)
    if orphans_after > orphans_before:
        problems.append(
            f"listings left without a profile: {orphans_before} before, "
            f"{orphans_after} after"
        )
    return problems


def _observe_repair_state(plan: RepairPlan) -> Dict[str, Any]:
    """Look at the database again after a COMMIT whose outcome is unknown.

    Best effort and observation only -- it never decides what the COMMIT did.
    The `rollback()` here resets the *session* so the connection is usable
    again; it cannot undo a commit the server may already have applied.
    """
    try:
        db.session.rollback()
        return {
            "readable": True,
            "profiles_that_should_be_gone_still_present": sorted(
                profile_id
                for profile_id in plan.profiles_to_delete
                if db.session.get(SearchProfile, profile_id) is not None
            ),
            "listings_per_target": {
                group.target_id: _remaining_property_count(group.target_id)
                for group in plan.groups
            },
        }
    except Exception as exc:
        logger.exception("Could not read the database back after an unknown COMMIT")
        return {"readable": False, "error": str(exc)}


def _base_report(plan: RepairPlan, mode: str) -> Dict[str, Any]:
    return {
        "mode": mode,
        "status": "pending" if plan.groups else "clean",
        "groups": [
            {
                "name": group.name,
                "target_id": group.target_id,
                "target_name": group.target_name,
                "rename_to": group.rename_to,
                "fragment_ids": list(group.fragment_ids),
                "properties_to_move": group.moves,
                "settings_preserved": list(group.settings_preserved),
                "settings_conflicts": list(group.settings_conflicts),
            }
            for group in plan.groups
        ],
        "blocked_groups": list(plan.blocked_groups),
        "properties_to_move": plan.properties_to_move,
        "properties_moved": 0,
        "profiles_to_delete": list(plan.profiles_to_delete),
        "profiles_deleted": [],
        "profiles_retained": list(plan.profiles_retained),
        "profiles_before": _counts_report(plan, plan.counts_before, plan.profile_names),
        "profiles_after": [],
        "left_in_place": list(plan.left_in_place),
        "unresolved_properties": plan.unresolved_properties,
        "orphan_properties": plan.orphan_properties,
        "manually_pinned_properties": plan.manually_pinned_properties,
        "post_commit_observation": None,
        "errors": [],
    }


class SearchProfileRepairService:
    """De-fragment saved-search profiles created by folded subjects.

    Distinct from `SearchProfileService.merge_duplicate_profiles()`, which
    groups by exact canonical *profile name* and therefore cannot see these
    four rows at all: their names differ. This service groups by the name
    recomputed from each listing's own stored email subject.
    """

    @staticmethod
    def recompute_profile_name(email_subject: Any) -> Optional[str]:
        """The saved-search name ingestion would derive from this subject.

        Rows written before #101 stored the subject still folded, so unfolding
        first is what makes the full name reappear. Rows written after #101 are
        already unfolded and pass through unchanged.
        """
        return extract_search_name(unfold_header(email_subject), "")

    @staticmethod
    def analyze() -> Dict[str, Any]:
        """Report what a repair would do. Writes nothing."""
        return _base_report(build_plan(), mode="dry-run")

    @staticmethod
    def apply() -> Dict[str, Any]:
        """Perform the repair in one transaction, or leave the database alone.

        Run with ingestion stopped: `properties.search_profile_id` is
        ``ON DELETE SET NULL``, so a listing inserted into a fragment while
        this runs would be orphaned rather than rejected.

        The returned `status` states what is *known* about the database:

        - `"mismatch"` -- failed before COMMIT, nothing was committed.
        - `"applied"` -- committed and verified.
        - `"applied_report_unavailable"` -- committed; only the after-report
          could not be read back.
        - `"commit_outcome_unknown"` -- COMMIT did not complete cleanly and
          the outcome cannot be determined from here. Not a rollback.

        Only `"mismatch"` licenses "the database is untouched".
        """
        plan = build_plan()
        report = _base_report(plan, mode="apply")
        if not plan.groups:
            report["status"] = "clean"
            return report

        errors: List[str] = []

        def abort(messages: List[str]) -> Dict[str, Any]:
            db.session.rollback()
            report["status"] = "mismatch"
            report["errors"] = messages
            logger.error("Profile repair aborted, nothing committed: %s", messages)
            return report

        try:
            # Nothing has been written yet, so this compares the plan against
            # the database as it stands. Take the row locks first, so no other
            # transaction can change these profiles between the comparison and
            # the writes; then expire, so the checks -- and the objects mutated
            # after them -- come from the database rather than from whatever
            # planning happened to load.
            db.session.expire_all()
            _lock_profiles(_involved_profile_ids(plan))
            errors.extend(_verify_profile_metadata(plan))
            if errors:
                return abort(errors)

            for group in plan.groups:
                target = db.session.get(SearchProfile, group.target_id)
                if target is None:  # pragma: no cover - checked one query ago
                    errors.append(f"target profile {group.target_id} disappeared")
                    continue
                for name, value in group.updates.items():
                    setattr(target, name, value)
                if group.rename_to:
                    target.name = group.rename_to
            if errors:  # pragma: no cover - planned one query ago
                return abort(errors)
            db.session.flush()

            moved = 0
            planned_ids: List[int] = []
            for group in plan.groups:
                for fragment_id in group.fragment_ids:
                    ids = group.property_ids[fragment_id]
                    planned_ids.extend(ids)
                    for chunk in _chunks(ids, UPDATE_CHUNK):
                        moved += (
                            db.session.query(Property)
                            .filter(Property.id.in_(chunk))
                            # The plan is a snapshot. Naming the profile the row
                            # is expected to be in means a row somebody moved in
                            # the meantime is not dragged back -- the UPDATE
                            # simply misses it, and the count below says so.
                            .filter(Property.search_profile_id == fragment_id)
                            .update(
                                {"search_profile_id": group.target_id},
                                synchronize_session=False,
                            )
                        )
            db.session.flush()
            # The bulk UPDATE bypassed the identity map; force every later read
            # in this transaction to come from the database.
            db.session.expire_all()

            if moved != plan.properties_to_move:
                errors.append(
                    f"expected to move {plan.properties_to_move} listings, "
                    f"moved {moved} (a listing was reassigned after planning)"
                )
            # The other half of the same race: pinned in place rather than
            # moved away, which no SQL predicate here can express portably.
            pinned_now = _pinned_among(planned_ids)
            if pinned_now:
                errors.append(
                    f"listings {pinned_now} were reassigned by hand after "
                    f"planning; refusing to move them"
                )
            orphans_before = plan.counts_before.get(None, 0)
            errors.extend(_verify_counts(plan, orphans_before))

            deleted: List[int] = []
            if not errors:
                for profile_id in plan.profiles_to_delete:
                    # Re-checked immediately before the DELETE, not reused from
                    # the verification above: an empty profile is the only one
                    # whose removal cannot orphan a listing.
                    remaining = _remaining_property_count(profile_id)
                    if remaining:
                        errors.append(
                            f"profile {profile_id} still holds {remaining} "
                            f"listings; refusing to delete it"
                        )
                        break
                    profile = db.session.get(SearchProfile, profile_id)
                    if profile is None:  # pragma: no cover - planned above
                        errors.append(f"profile {profile_id} disappeared")
                        break
                    db.session.delete(profile)
                    deleted.append(profile_id)

            if deleted and not errors:
                # Send the DELETEs now instead of letting commit() flush them.
                # Two things follow, and both matter:
                #
                # 1. Nothing looks at the database between the zero-check above
                #    and the DELETE if the DELETE only leaves as part of the
                #    commit. A listing inserted into a fragment in that window
                #    is not rejected -- the FK is ON DELETE SET NULL, and
                #    SQLAlchemy's own nullify pass hits it first -- so it is
                #    silently orphaned. Flushing here puts the check and the
                #    DELETE in the same inspectable step, and the re-count
                #    below catches exactly that row.
                # 2. Once the DELETE is on the wire the parent row is locked,
                #    so a concurrent INSERT referencing it (which needs a
                #    FOR KEY SHARE lock on that row) waits for us rather than
                #    slipping in, and fails the FK if we commit.
                #
                # A failure in this flush is also a pre-COMMIT failure, which
                # is an honest rollback, rather than an unknown COMMIT outcome.
                db.session.flush()
                db.session.expire_all()
                errors.extend(_verify_counts(plan, orphans_before))
                for profile_id in deleted:
                    if db.session.get(SearchProfile, profile_id) is not None:
                        errors.append(  # pragma: no cover - defensive
                            f"profile {profile_id} survived its own DELETE"
                        )

            if errors:
                return abort(errors)
        except Exception as exc:
            # Everything above happens before COMMIT is sent, so this really is
            # a rollback: the transaction never reached the server's commit
            # point and nothing of it survives.
            db.session.rollback()
            logger.exception("Profile repair failed before COMMIT, nothing committed")
            report["status"] = "mismatch"
            report["errors"] = [f"repair failed before COMMIT: {exc}"]
            return report

        try:
            db.session.commit()
        except Exception as exc:
            # A COMMIT that raises is *not* a rollback. The server may have
            # applied it and lost the connection before the acknowledgement
            # got back, so the only honest answer is that the outcome is
            # unknown -- and the operator must not read "untouched" into it.
            logger.exception("COMMIT did not complete cleanly; outcome UNKNOWN")
            report["status"] = "commit_outcome_unknown"
            report["errors"] = [
                "COMMIT did not complete cleanly, so the outcome is UNKNOWN: "
                "the server may have applied it and lost the connection before "
                f"acknowledging. Do not assume a rollback. Cause: {exc}"
            ]
            report["properties_moved"] = moved
            report["profiles_deleted"] = deleted
            report["post_commit_observation"] = _observe_repair_state(plan)
            return report

        # Past this point the destructive half is durable. Nothing below may
        # turn into a "nothing was committed" verdict: reading the database
        # back for the after-report can still fail, and the operator has to be
        # told which of the two happened.
        report["properties_moved"] = moved
        report["profiles_deleted"] = deleted
        logger.info(
            "Profile repair applied: %s listings moved, profiles deleted: %s",
            moved,
            deleted,
        )
        try:
            names = {profile.id: profile.name for profile in SearchProfile.query.all()}
            report["profiles_after"] = _counts_report(
                plan, _profile_property_counts(), names
            )
        except Exception as exc:
            logger.exception("Profile repair committed, but the report failed")
            report["status"] = "applied_report_unavailable"
            report["errors"] = [
                f"the repair was COMMITTED; building the after-report failed: {exc}"
            ]
            return report

        report["status"] = "applied"
        return report


def _count_label(entry: Dict[str, Any]) -> str:
    profile_id = entry["profile_id"]
    if profile_id is None:
        return "listings with no profile"
    if entry["name"] is None:
        return f"#{profile_id} (deleted)"
    return f"#{profile_id} {entry['name']!r}"


def _format_report(report: Dict[str, Any]) -> Iterable[str]:
    yield f"mode:   {report['mode']}"
    yield f"status: {report['status']}"
    if report["status"] == "mismatch":
        yield "        it failed before COMMIT; the database is untouched"
    elif report["status"] == "applied_report_unavailable":
        yield "        the repair WAS COMMITTED; only the after-report failed"
        yield "        do not re-run blindly - inspect the database first"
    elif report["status"] == "commit_outcome_unknown":
        yield "        COMMIT did not complete cleanly - the outcome is UNKNOWN"
        yield "        this is NOT a rollback; the change may or may not be there"
        yield "        inspect the database before doing anything else"
    if report["blocked_groups"]:
        # "clean" means there was nothing for this tool to do, not that the
        # database is tidy. Say so next to the status, not only further down.
        yield (
            f"        {len(report['blocked_groups'])} saved search(es) were "
            f"deliberately NOT repaired - see BLOCKED below"
        )
    yield ""
    observation = report.get("post_commit_observation")
    if observation:
        yield "read back afterwards (observation only, proves nothing):"
        if observation.get("readable"):
            yield (
                "  profiles that should be gone, still present: "
                f"{observation['profiles_that_should_be_gone_still_present']}"
            )
            yield f"  listings per target: {observation['listings_per_target']}"
        else:
            yield f"  could not read the database back: {observation.get('error')}"
        yield ""
    for group in report["groups"]:
        yield f"saved search: {group['name']!r}"
        yield (
            f"  survivor:  #{group['target_id']} {group['target_name']!r}"
            + (f" -> renamed to {group['rename_to']!r}" if group["rename_to"] else "")
        )
        yield f"  fragments: {group['fragment_ids'] or 'none'}"
        yield f"  listings to move: {group['properties_to_move']}"
        for entry in group["settings_preserved"]:
            yield f"  keeps {entry['field']} from profile #{entry['profile_id']}"
        for entry in group["settings_conflicts"]:
            yield (
                f"  NOT copied: {entry['field']} from profile "
                f"#{entry['profile_id']} (survivor already has one)"
            )
        yield ""
    for blocked in report["blocked_groups"]:
        yield f"BLOCKED: saved search {blocked['name']!r} was not repaired"
        yield f"  {blocked['reason']}"
        yield f"  profiles holding its listings: {blocked['candidate_ids']}"
        yield f"  other saved searches in them: {blocked['other_names']}"
        yield ""
    if report["profiles_before"]:
        yield "listings per profile before:"
        for entry in report["profiles_before"]:
            yield f"  {_count_label(entry)}: {entry['properties']}"
    if report["profiles_after"]:
        yield "listings per profile after:"
        for entry in report["profiles_after"]:
            yield f"  {_count_label(entry)}: {entry['properties']}"
    if report["profiles_before"] or report["profiles_after"]:
        yield ""
    # Past tense would be a claim, and after an unknown COMMIT there is none
    # to make: those rows were staged in the transaction, nothing more.
    done = "attempted" if report["status"] == "commit_outcome_unknown" else "done"
    yield f"listings to move:   {report['properties_to_move']}"
    yield f"listings moved ({done}):   {report['properties_moved']}"
    yield f"profiles to delete: {report['profiles_to_delete'] or 'none'}"
    yield f"profiles deleted ({done}): {report['profiles_deleted'] or 'none'}"
    if report["profiles_retained"]:
        yield f"profiles kept (not empty): {report['profiles_retained']}"
    if report["unresolved_properties"]:
        yield (
            f"listings whose saved-search name could not be recomputed: "
            f"{report['unresolved_properties']} (left untouched)"
        )
    if report["orphan_properties"]:
        yield (
            f"listings already without a profile: {report['orphan_properties']} "
            f"(left untouched)"
        )
    if report["manually_pinned_properties"]:
        yield (
            f"listings you reassigned by hand: "
            f"{report['manually_pinned_properties']} (left exactly where they "
            f"are, and their profile is kept)"
        )
    for entry in report["left_in_place"]:
        yield (
            f"left alone: {entry['listings']} listing(s) of "
            f"{entry['saved_search']!r} in #{entry['profile_id']} "
            f"{entry['name']!r} - not a fold fragment, so somebody put them there"
        )
    for message in report["errors"]:
        yield f"ERROR: {message}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.search_profile_repair_service",
        description=(
            "Merge saved-search profiles that a folded email subject split "
            "apart. Dry run by default. Apply with ingestion stopped."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repair. Without it nothing is modified.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the report as JSON instead of text."
    )
    return parser


def run_repair_cli(argv: Optional[Sequence[str]] = None) -> int:
    """Run the repair inside an existing app context. Returns the exit code."""
    args = _build_parser().parse_args(argv)
    report = (
        SearchProfileRepairService.apply()
        if args.apply
        else SearchProfileRepairService.analyze()
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        for line in _format_report(report):
            print(line)
    # An unknown status is treated as a rollback only if the code says so;
    # default to 1 so a status added later cannot silently pass as success.
    return EXIT_CODES.get(report["status"], 1)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Never let the repair process start a scheduler of its own: a one-off
    # `docker compose run` inherits AUTO_START_SCHEDULER=true from the compose
    # file, which would have this container ingesting mail while it repairs.
    # It does not stop the *long-running* container -- that is the operator's
    # step, and the runbook says so.
    os.environ["AUTO_START_SCHEDULER"] = "false"

    from app import create_app

    app = create_app()
    with app.app_context():
        return run_repair_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
