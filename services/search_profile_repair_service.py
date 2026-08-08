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
``ON DELETE SET NULL``: a listing that lands in a fragment between the
zero-check and the ``DELETE`` is not blocked, it is silently orphaned with a
NULL profile. The repair therefore reassigns, re-counts and deletes inside one
transaction, and aborts the whole thing if a single count disagrees -- but a
transaction cannot stop a concurrent writer from inserting a new row, only
notice it. **Apply with ingestion stopped** (`docker compose stop app`, which
stops the in-process scheduler); see the "Repairing fragmented search
profiles" section of MIGRATION_RUNBOOK.md.

Dry run (default) writes nothing::

    docker compose run --rm app python -m services.search_profile_repair_service

Apply::

    docker compose run --rm app python -m services.search_profile_repair_service --apply

Exit codes say exactly what happened to the database:

===  =============================  ======================================
  0  clean / pending / applied      either nothing needed doing, or the
                                    repair committed and verified
  1  mismatch                       **nothing was committed**; the database
                                    is untouched
  2  applied_report_unavailable     **the repair was committed** and only
                                    the after-report could not be built;
                                    inspect the database before re-running
===  =============================  ======================================

A saved search whose listings sit in a profile that also holds *another*
saved search is reported as BLOCKED and left completely alone -- renaming
such a profile would mislabel the other listings. That does not change the
exit code; read the report. Repairing whatever put those rows together
(ProfileAssignmentService reassigns by location, and profiles can be edited
by hand) is a separate decision for the owner. Once the ambiguity is gone a
later run picks the group up normally.
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
    name: str, profiles: Sequence[SearchProfile]
) -> Optional[SearchProfile]:
    """The profile ingestion would reuse for `name`, or None.

    Mirrors `SearchProfileService.get_or_create_profile_by_name()` lookup
    order -- exact name first, then canonical -- minus the create half, which
    a dry run must never trigger.
    """
    for profile in profiles:
        if profile.name == name:
            return profile
    canonical = _canonical_profile_name(name)
    if not canonical:
        return None
    for profile in profiles:
        if _canonical_profile_name(profile.name) == canonical:
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

    # {recomputed name: {current profile id: [property ids]}}
    grouped: Dict[str, Dict[int, List[int]]] = {}
    # {profile id: every recomputed name found inside it}. A profile holding
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
        grouped.setdefault(name, {}).setdefault(profile_id, []).append(property_id)
        names_by_profile.setdefault(profile_id, set()).add(name)

    moved_out: Dict[int, int] = {}
    moved_in: Dict[int, int] = {}

    for name in sorted(grouped):
        property_ids = grouped[name]
        target = _find_profile_by_name(name, profiles)
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
                if pid in by_id and names_by_profile.get(pid) == {name}
            ]
            if not candidates:
                plan.blocked_groups.append(
                    {
                        "name": name,
                        "candidate_ids": sorted(property_ids),
                        "other_names": sorted(
                            other
                            for pid in property_ids
                            for other in names_by_profile.get(pid, set())
                            if other != name
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

        fragment_ids = sorted(pid for pid in property_ids if pid != target.id)
        if not fragment_ids and not rename_to:
            continue

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

        The returned `status` states what happened to the database, and only
        `"mismatch"` means nothing was committed. `"applied"` and
        `"applied_report_unavailable"` both mean the repair is durable; the
        second one only says the after-report could not be read back.
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
            for profile_id, expected in plan.expected_after.items():
                actual = _remaining_property_count(profile_id)
                if actual != expected:
                    errors.append(
                        f"profile {profile_id}: expected {expected} listings "
                        f"after the move, found {actual}"
                    )
            # Every NULL-profile row, not just the ones with a recoverable
            # name: the point is that the repair created none.
            orphans_before = plan.counts_before.get(None, 0)
            orphans_after = _remaining_property_count(None)
            if orphans_after > orphans_before:
                errors.append(
                    f"listings left without a profile: {orphans_before} "
                    f"before, {orphans_after} after"
                )

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

            if errors:
                return abort(errors)

            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.exception("Profile repair failed, nothing committed")
            report["status"] = "mismatch"
            report["errors"] = [f"repair failed: {exc}"]
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
        yield "        nothing was committed; the database is untouched"
    elif report["status"] == "applied_report_unavailable":
        yield "        the repair WAS COMMITTED; only the after-report failed"
        yield "        do not re-run blindly - inspect the database first"
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
    yield f"listings to move:   {report['properties_to_move']}"
    yield f"listings moved:     {report['properties_moved']}"
    yield f"profiles to delete: {report['profiles_to_delete'] or 'none'}"
    yield f"profiles deleted:   {report['profiles_deleted'] or 'none'}"
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
