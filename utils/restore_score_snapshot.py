"""Put scores — and the subscription weights that produced them — back.

    python -m utils.restore_score_snapshot --snapshot data/pool_weight_enable_snapshot.json
    python -m utils.restore_score_snapshot --snapshot ... --apply --backup data/pre_restore.json
    python -m utils.restore_score_snapshot --snapshot ... --apply --no-backup

Why this exists (2026-08-15): the pool criterion ships weightless and was
turned on in production data — `scoring_config.categories.<cat>.lifestyle.
pool_score = 0.1` on three subscriptions — which re-scored every listing under
them. The rollback point taken first, `data/pool_weight_enable_snapshot.json`,
carries both halves of that change: the three profiles' previous
`scoring_config` **and** the score columns of every property. No tool read
that shape, so the documented rollback was "by hand", and a rollback nobody
has run is a rollback nobody knows works. See CLAUDE.md, "Rolling that back is
a data restore, not a deploy".

What it does, and refuses to do:

* **dry run by default.** It says what it would change and exits; `--apply` is
  the only path that writes. A restore is a rewrite like any other, and the
  #98 rule holds here too — nothing is reported as done that was not done;
* **a backup of the state it is about to overwrite** is written first, in the
  same shape, so a restore aimed at the wrong file is itself reversible.
  `--no-backup` is explicit, never a default;
* **the whole snapshot or none of it.** Every row is parsed before the first
  write (`utils/score_snapshot.py`), and one transaction covers profiles and
  rows together: putting the weights back while the scores stay, or the other
  way round, is a state the app never had;
* **properties the snapshot does not name are named.** Rows ingested after the
  snapshot was taken were scored under the config being rolled back, and this
  tool cannot know what they were before — because they were not there. They
  are listed, and `--rescore-uncovered` recomputes them under the restored
  config, which is the one thing that *is* computable about them.
"""

import argparse
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app import create_app, db
from models import Property, SearchProfile
from utils import score_snapshot
from utils.score_snapshot import JSON_COLUMNS, Snapshot, SnapshotError

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 2


@dataclass
class Plan:
    """What the restore would do, measured against the database as it is now."""

    snapshot: Snapshot
    changed: List[int] = field(default_factory=list)
    unchanged: List[int] = field(default_factory=list)
    missing: List[int] = field(default_factory=list)
    profiles_changed: Dict[int, Tuple[Any, Any]] = field(default_factory=dict)
    profiles_unchanged: List[int] = field(default_factory=list)
    profiles_missing: List[int] = field(default_factory=list)
    uncovered: List[int] = field(default_factory=list)

    @property
    def writes_nothing(self) -> bool:
        return not self.changed and not self.profiles_changed


def build_plan(snapshot: Snapshot) -> Plan:
    """Compare the snapshot with the current rows. Reads only."""
    plan = Plan(snapshot=snapshot)

    for profile_id, config in snapshot.profiles.items():
        profile = db.session.get(SearchProfile, profile_id)
        if profile is None:
            plan.profiles_missing.append(profile_id)
            continue
        current = profile.scoring_config
        if current == config:
            plan.profiles_unchanged.append(profile_id)
        else:
            plan.profiles_changed[profile_id] = (current, config)

    for row in snapshot.rows:
        prop = db.session.get(Property, row["id"])
        if prop is None:
            plan.missing.append(row["id"])
        elif score_snapshot.differs(prop, row):
            plan.changed.append(row["id"])
        else:
            plan.unchanged.append(row["id"])

    # Only a snapshot that carries profile config can leave rows behind: it is
    # the config change that re-scored listings the snapshot never saw. A bare
    # row list changes no weights, so every row outside it is none of its
    # business and warning about them would be noise.
    if snapshot.profiles:
        known = set(snapshot.ids)
        query = Property.query.filter(
            Property.search_profile_id.in_(list(snapshot.profiles))
        )
        plan.uncovered = sorted(p.id for p in query.all() if p.id not in known)

    return plan


def describe(plan: Plan, limit: int = 20) -> List[str]:
    """The report, as lines. The same text in a dry run and in an apply."""
    snapshot = plan.snapshot
    lines = [
        f"Snapshot {snapshot.path}"
        + (f" taken {snapshot.created_at}" if snapshot.created_at else ""),
        f"  rows in snapshot:   {len(snapshot.rows)}",
        f"  profiles in it:     {len(snapshot.profiles)}",
    ]
    for profile_id, (current, restored) in sorted(plan.profiles_changed.items()):
        lines.append(f"  profile {profile_id}: {_short(current)} -> {_short(restored)}")
    if plan.profiles_unchanged:
        lines.append(
            f"  profiles already at the snapshot value: "
            f"{_ids(plan.profiles_unchanged, limit)}"
        )
    if plan.profiles_missing:
        lines.append(f"  profiles GONE: {_ids(plan.profiles_missing, limit)}")

    lines.append(f"  rows that would change:  {len(plan.changed)}")
    lines.append(f"  rows already matching:   {len(plan.unchanged)}")
    if plan.missing:
        lines.append(
            f"  rows GONE (property deleted since): {_ids(plan.missing, limit)}"
        )
    if plan.uncovered:
        lines.append(
            f"  {len(plan.uncovered)} properties under these profiles are NOT in the "
            f"snapshot: {_ids(plan.uncovered, limit)}"
        )
        lines.append(
            "    they were scored under the config being rolled back; this file "
            "cannot say what they were before, because they did not exist. "
            "--rescore-uncovered recomputes them under the restored config."
        )
    return lines


def _ids(ids: List[int], limit: int) -> str:
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, ... (+{len(ids) - limit} more)"


def _short(config: Any) -> str:
    if config is None:
        return "null"
    text = str(config)
    return text if len(text) <= 80 else text[:77] + "..."


def _backup_payload(plan: Plan) -> Dict[str, Any]:
    """The current state of exactly what the restore would overwrite."""
    rows = []
    for row in plan.snapshot.rows:
        prop = db.session.get(Property, row["id"])
        if prop is None:
            continue
        carried = [column for column in JSON_COLUMNS if column in row]
        rows.append(score_snapshot.snapshot_row(prop, json_columns=carried))
    profiles = {}
    for profile_id in plan.snapshot.profiles:
        profile = db.session.get(SearchProfile, profile_id)
        if profile is not None:
            profiles[str(profile_id)] = profile.scoring_config
    return {
        "created_at": _now(),
        "note": f"state replaced by a restore of {plan.snapshot.path}",
        "profiles": profiles,
        "scores": rows,
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def apply_plan(plan: Plan, rescore_uncovered: bool = False) -> Dict[str, int]:
    """Restore profiles and rows in one transaction, and commit."""
    counts: Dict[str, int] = {}
    try:
        restored_profiles, _ = score_snapshot.restore_profiles(plan.snapshot.profiles)
        restored_rows, _ = score_snapshot.apply_rows(plan.snapshot.rows)
        counts["profiles"] = restored_profiles
        counts["rows"] = restored_rows

        rescored = 0
        if rescore_uncovered and plan.uncovered:
            from services.property_scoring_service import PropertyScoringService

            scoring_service = PropertyScoringService()
            for prop_id in plan.uncovered:
                prop = db.session.get(Property, prop_id)
                if prop is not None and scoring_service.calculate_for_property(
                    prop, commit=False
                ):
                    rescored += 1
        counts["rescored"] = rescored

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return counts


def run(argv: Optional[List[str]] = None) -> int:
    """Everything but the app context, so a test can drive it."""
    args = _parse_args(argv)

    try:
        snapshot = score_snapshot.load(args.snapshot)
    except SnapshotError as exc:
        logger.error("%s", exc)
        return EXIT_REFUSED

    if not snapshot.rows and not snapshot.profiles:
        logger.error(
            "%s restores nothing: no rows and no profiles in it", args.snapshot
        )
        return EXIT_REFUSED

    plan = build_plan(snapshot)
    for line in describe(plan):
        logger.info("%s", line)

    if not args.apply:
        # A dry run leaves the session exactly as it found it: `build_plan`
        # only reads, but an autoflush on the way out would still be this
        # tool's write.
        db.session.rollback()
        logger.info("Dry run: nothing written. Add --apply to restore.")
        return EXIT_OK

    if plan.writes_nothing and not (args.rescore_uncovered and plan.uncovered):
        logger.info("Nothing to restore: the database already matches the snapshot.")
        return EXIT_OK

    if args.backup:
        score_snapshot.write(_backup_payload(plan), args.backup)
        logger.info("Wrote pre-restore backup to %s", args.backup)

    counts = apply_plan(plan, rescore_uncovered=args.rescore_uncovered)
    logger.info(
        "Restored %s profile(s) and %s row(s); %s uncovered row(s) rescored.",
        counts["profiles"],
        counts["rows"],
        counts["rescored"],
    )
    if plan.missing:
        logger.warning(
            "%s row(s) in the snapshot no longer exist and were not restored: %s",
            len(plan.missing),
            _ids(plan.missing, 20),
        )
    if plan.uncovered and not args.rescore_uncovered:
        logger.warning(
            "%s propert(ies) were scored under the rolled-back config and were "
            "left as they are: %s",
            len(plan.uncovered),
            _ids(plan.uncovered, 20),
        )
    return EXIT_OK


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore score columns (and subscription weights) from a snapshot."
    )
    parser.add_argument("--snapshot", required=True, help="Snapshot file to restore.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it the run reports and changes nothing.",
    )
    parser.add_argument(
        "--backup",
        help="Where to write the state being overwritten. Required with --apply.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Apply without writing a backup first. Say it out loud.",
    )
    parser.add_argument(
        "--rescore-uncovered",
        action="store_true",
        help="Recompute properties the snapshot does not name, under the restored config.",
    )
    args = parser.parse_args(argv)
    if args.apply and not args.backup and not args.no_backup:
        parser.error("--apply needs --backup PATH (or an explicit --no-backup)")
    if args.backup and args.no_backup:
        parser.error("--backup and --no-backup contradict each other")
    return args


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    app = create_app()
    with app.app_context():
        raise SystemExit(run())


if __name__ == "__main__":
    main()
