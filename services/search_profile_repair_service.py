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

A saved search whose listings sit in a profile that also holds *another*
saved search is reported as BLOCKED and left completely alone -- renaming
such a profile would mislabel the other listings. That does not change the
exit code; read the report. This covers ambiguity the plan creates as much
as ambiguity it finds: planning is done against a running projection of the
renames and moves already decided, so a profile that *gains* a second saved
search mid-plan blocks too, and one the plan *empties* stops blocking within
the same run. Whatever put genuinely mixed rows together
(ProfileAssignmentService reassigns by location, and profiles can be edited
by hand) is an owner decision, not this tool's.
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

from sqlalchemy import func

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
    unresolved_properties: int = 0
    orphan_properties: int = 0

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

    # Flags and description follow merge_duplicate_profiles(): the survivor
    # inherits a role none of its own columns claim. First fragment wins, so
    # the outcome does not depend on how many fragments share the flag.
    for fragment in fragments:
        if (
            fragment.is_default
            and not target.is_default
            and "is_default" not in updates
        ):
            updates["is_default"] = True
            preserved.append({"profile_id": fragment.id, "field": "is_default"})
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


def build_plan() -> RepairPlan:
    """Work out the whole repair without touching a single row."""
    plan = RepairPlan()
    profiles = SearchProfile.query.order_by(SearchProfile.id.asc()).all()
    plan.profile_names = {profile.id: profile.name for profile in profiles}
    by_id = {profile.id: profile for profile in profiles}
    plan.counts_before = _profile_property_counts()

    # Keyed by *canonical* name throughout. "ALPHA" and "Alpha" are one saved
    # search as far as ingestion is concerned -- get_or_create_profile_by_name()
    # hands both to the same profile -- so planning them as two groups gives
    # one target two independent settings merges, each reading the target's
    # original state, and the second silently overwrites the first.
    #
    # {canonical name: {current profile id: [property ids]}}
    grouped: Dict[str, Dict[int, List[int]]] = {}
    # {canonical name: {exact spelling: how many listings use it}}, so a
    # promotion renames to the spelling most of the listings actually use.
    spellings: Dict[str, Dict[str, int]] = {}
    # {profile id: every canonical name found inside it}. A profile holding
    # more than one is not a fold fragment -- something else put those rows
    # together (ProfileAssignmentService reassigns by location, and profiles
    # can be edited by hand) -- and renaming it after one of those names would
    # mislabel the others.
    names_by_profile: Dict[int, set] = {}
    rows = (
        db.session.query(
            Property.id, Property.search_profile_id, Property.email_subject
        )
        .order_by(Property.id.asc())
        .all()
    )
    for property_id, profile_id, subject in rows:
        name = SearchProfileRepairService.recompute_profile_name(subject)
        if not name:
            plan.unresolved_properties += 1
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
            continue
        grouped.setdefault(canonical, {}).setdefault(profile_id, []).append(property_id)
        counts = spellings.setdefault(canonical, {})
        counts[name] = counts.get(name, 0) + 1
        names_by_profile.setdefault(profile_id, set()).add(canonical)

    moved_out: Dict[int, int] = {}
    moved_in: Dict[int, int] = {}

    # Planning one group changes the ground the next group stands on: a rename
    # frees a name, and a move gives a profile listings it did not have. Both
    # are tracked as the plan is built, because deciding a later group against
    # the pre-repair snapshot is how two saved searches end up in one profile.
    effective_names: Dict[int, str] = dict(plan.profile_names)
    projected_names: Dict[int, set] = {
        profile_id: set(names) for profile_id, names in names_by_profile.items()
    }

    # A target belongs to one group. The eligibility rule below already makes
    # a second claim impossible, but this is a destructive operation, so the
    # reservation is written down rather than left to hold by argument.
    claimed_by: Dict[int, str] = {}

    # The spelling most of a group's listings use, ties broken alphabetically.
    display_names: Dict[str, str] = {
        canonical: min(counts.items(), key=lambda item: (-item[1], item[0]))[0]
        for canonical, counts in spellings.items()
    }

    for canonical in sorted(grouped):
        property_ids = grouped[canonical]
        name = display_names[canonical]
        target = _find_profile_by_name(name, canonical, profiles, effective_names)
        rename_to: Optional[str] = None
        if target is None:
            # Nobody carries the correct name yet. Promote the fragment with
            # the most listings instead of creating a fresh profile, so its
            # settings survive; the ordering is the one merge_duplicate_
            # profiles() already uses. A rename cannot collide here, because
            # _find_profile_by_name() just proved no profile holds the name.
            #
            # Only a profile whose *entire* resolvable content is this one name
            # may be promoted. That is also what keeps two groups from claiming
            # the same profile: if everything in it resolves to N, no other
            # group has it among its candidates, so no reservation bookkeeping
            # is needed and no group can rename it out from under another.
            candidates = [
                by_id[pid]
                for pid in property_ids
                if pid in by_id and projected_names.get(pid) == {canonical}
            ]
            if not candidates:
                plan.blocked_groups.append(
                    {
                        "name": name,
                        "candidate_ids": sorted(property_ids),
                        "other_names": sorted(
                            display_names.get(other, other)
                            for pid in property_ids
                            for other in projected_names.get(pid, set())
                            if other != canonical
                        ),
                        "reason": (
                            "no profile carries this name, and every profile "
                            "holding its listings also holds another saved "
                            "search; renaming one would mislabel the rest"
                        ),
                    }
                )
                continue
            target = sorted(
                candidates,
                key=lambda p: (not bool(p.is_default), -len(property_ids[p.id]), p.id),
            )[0]
            rename_to = name
        # A profile that does exist keeps its own spelling: ingestion's
        # get_or_create_profile_by_name() reuses a canonical match without
        # renaming it, and this repair follows ingestion rather than tidying
        # names behind the owner's back.

        owner = claimed_by.get(target.id)
        if owner is not None and owner != canonical:
            plan.blocked_groups.append(  # pragma: no cover - defensive
                {
                    "name": name,
                    "candidate_ids": sorted(property_ids),
                    "other_names": [display_names.get(owner, owner)],
                    "reason": (
                        f"profile {target.id} is already the survivor of another "
                        f"saved search in this plan; refusing to give one profile "
                        f"to two subscriptions"
                    ),
                }
            )
            continue

        fragment_ids = sorted(pid for pid in property_ids if pid != target.id)
        if not fragment_ids and not rename_to:
            continue

        # Book the plan's own effects before moving on to the next group.
        claimed_by[target.id] = canonical
        if rename_to:
            effective_names[target.id] = rename_to
        for fragment_id in fragment_ids:
            # Every listing of this name leaves the fragment...
            projected_names.get(fragment_id, set()).discard(canonical)
        # ...and arrives at the target, which from here on counts as holding
        # this saved search even if it held none of it a moment ago.
        projected_names.setdefault(target.id, set()).add(canonical)

        group = GroupPlan(
            name=name,
            target_id=target.id,
            target_name=target.name,
            rename_to=rename_to,
            fragment_ids=fragment_ids,
            property_ids={pid: sorted(ids) for pid, ids in property_ids.items()},
        )
        fragments = [by_id[pid] for pid in fragment_ids if pid in by_id]
        (
            group.updates,
            group.settings_preserved,
            group.settings_conflicts,
        ) = _plan_settings_merge(target, fragments)
        plan.groups.append(group)

        for fragment_id in fragment_ids:
            leaving = len(property_ids[fragment_id])
            moved_out[fragment_id] = moved_out.get(fragment_id, 0) + leaving
            moved_in[target.id] = moved_in.get(target.id, 0) + leaving

    target_ids = {group.target_id for group in plan.groups}
    for profile_id in sorted(set(moved_out) | set(moved_in)):
        plan.expected_after[profile_id] = (
            plan.counts_before.get(profile_id, 0)
            - moved_out.get(profile_id, 0)
            + moved_in.get(profile_id, 0)
        )

    for profile_id in sorted(moved_out):
        remaining = plan.expected_after.get(profile_id, 0)
        if remaining == 0 and profile_id not in target_ids:
            plan.profiles_to_delete.append(profile_id)
        else:
            # A fragment that still holds listings after the move -- listings
            # whose name could not be recomputed, or another saved search --
            # is kept. Only an empty profile is safe to delete.
            plan.profiles_retained.append(
                {"profile_id": profile_id, "remaining": remaining}
            )

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
        "unresolved_properties": plan.unresolved_properties,
        "orphan_properties": plan.orphan_properties,
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
            for group in plan.groups:
                target = db.session.get(SearchProfile, group.target_id)
                if target is None:  # pragma: no cover - planned one query ago
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
            for group in plan.groups:
                for fragment_id in group.fragment_ids:
                    ids = group.property_ids[fragment_id]
                    for chunk in _chunks(ids, UPDATE_CHUNK):
                        moved += (
                            db.session.query(Property)
                            .filter(Property.id.in_(chunk))
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
                    f"moved {moved}"
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
