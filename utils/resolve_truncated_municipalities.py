"""Resolve the municipalities the alert emails cut off (issue #298).

Idealista alert emails truncate long location strings with an ellipsis and
ingestion stored them verbatim, so `properties.municipality` came to hold
"Ovi..." twice, "Ovied...", "Mieres de..." and six more like them while the
full siblings ("Oviedo", "Mieres Del Camino") sat in neighbouring rows. The
/properties municipality dropdown offered each artifact as a municipality of
its own, filtering by the full name silently missed the truncated rows, and
municipality also feeds the /municipalities comparison and its INE join --
a truncated artifact is a row whose facts can never match.

Ingestion resolves new arrivals now (`PropertyIMAPService`), and the filter
options exclude whatever still carries the marker (`routes/main_routes.py`) --
but neither touches the rows already stored. This does, and it costs nothing:
no Google call, no Overpass call, just a rewrite of one text column against
names already in the table. Every truncated row lands in exactly one bucket:

* **auto** -- exactly one stored full name starts with the stem, and the stem
  does not end at a generic connective: resolved to that name.
* **mapped** -- the operator supplied the full name with `--map`.
* **needs mapping** -- the stem ends at a generic connective (de/del/la/...),
  where a unique prefix match picks whichever sibling ingestion happens to
  know ("San Juan de..." must not become San Juan de Alicante just because
  San Juan de la Arena was never stored). Reported, never auto-resolved.
* **unmatched** -- no unique stored full name starts with the stem. Kept
  verbatim (the #98 rule: missing data stays explicit, never guessed).

    python -m utils.resolve_truncated_municipalities --dry-run
    python -m utils.resolve_truncated_municipalities --snapshot data/municipality_truncation.json
    python -m utils.resolve_truncated_municipalities \
        --map "Mieres de...=Mieres Del Camino" \
        --snapshot data/municipality_truncation.json
    python -m utils.resolve_truncated_municipalities --restore data/municipality_truncation.json

Idempotent by construction: a resolved row no longer ends in the marker, so a
second run does not select it, and a row that could not be resolved resolves
to nothing next time too (this tool adds no new full names). The snapshot
holds only the rows the run will rewrite -- written owner-only in the target
directory, fsynced and atomically renamed before the first row is touched --
and the writes commit one row at a time, so an interrupted run has written
exactly the rows it logged.
"""

import argparse
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app import create_app, db
from models import Property
from utils.idealista_extractors import (
    is_truncated_municipality,
    resolve_truncated_municipality,
    truncation_stem_ends_at_connective,
)

logger = logging.getLogger(__name__)

# How a plan entry got (or did not get) its full name.
KIND_AUTO = "auto"
KIND_MAPPED = "mapped"
KIND_NEEDS_MAPPING = "needs mapping"
KIND_UNMATCHED = "unmatched"


@dataclass
class PlanEntry:
    prop: Property
    full: Optional[str]  # None: the row keeps its marker.
    kind: str


def truncated_rows(properties: List[Property]) -> List[Property]:
    """The rows still holding a municipality the email cut off."""
    return [prop for prop in properties if is_truncated_municipality(prop.municipality)]


def known_municipalities(properties: List[Property]) -> List[str]:
    """Every stored municipality, truncated ones included.

    `resolve_truncated_municipality` refuses truncated entries as resolution
    targets itself, so this stays a plain projection rather than a second
    copy of the detection rule.
    """
    return sorted({prop.municipality for prop in properties if prop.municipality})


def parse_mappings(raw: Optional[List[str]]) -> Dict[str, str]:
    """`--map "TRUNCATED=FULL"` items as a dict, validated loudly."""
    mappings: Dict[str, str] = {}
    for item in raw or []:
        truncated, sep, full = item.partition("=")
        truncated = truncated.strip()
        full = full.strip()
        if not sep or not truncated or not full:
            raise SystemExit(f'--map takes "TRUNCATED=FULL", got {item!r}')
        if not is_truncated_municipality(truncated):
            raise SystemExit(
                f"--map key {truncated!r} does not end in a truncation marker; "
                "this tool only rewrites truncated rows"
            )
        if is_truncated_municipality(full):
            raise SystemExit(
                f"--map value {full!r} is itself truncated; a mapping must "
                "supply the full name"
            )
        mappings[truncated] = full
    return mappings


def plan_changes(
    properties: List[Property], mappings: Optional[Dict[str, str]] = None
) -> List[PlanEntry]:
    """One PlanEntry per truncated row; `full` is None where the row stays."""
    mappings = mappings or {}
    known = known_municipalities(properties)
    plan: List[PlanEntry] = []
    for prop in truncated_rows(properties):
        value = prop.municipality
        if value in mappings:
            plan.append(PlanEntry(prop, mappings[value], KIND_MAPPED))
        elif truncation_stem_ends_at_connective(value):
            # The wrong-pick shape: the universe of stored names is not
            # Spain, so a unique match at a connective proves nothing.
            plan.append(PlanEntry(prop, None, KIND_NEEDS_MAPPING))
        else:
            full = resolve_truncated_municipality(value, known)
            plan.append(PlanEntry(prop, full, KIND_AUTO if full else KIND_UNMATCHED))
    return plan


def apply_plan(plan: List[PlanEntry]) -> Tuple[int, int]:
    """Rewrite the rows planned with a full name, one commit per row.

    Returns (resolved, failed). Entries planned as None are never written --
    they keep their marker.
    """
    resolved = 0
    failed = 0
    for entry in plan:
        if not entry.full:
            continue
        try:
            entry.prop.municipality = entry.full
            db.session.commit()
            resolved += 1
        except Exception as e:
            failed += 1
            logger.warning("Failed for property %s: %s", entry.prop.id, e)
            db.session.rollback()
    return resolved, failed


def _snapshot_row(prop: Property) -> Dict[str, Any]:
    return {"id": prop.id, "municipality": prop.municipality}


def _write_snapshot(rows: List[Dict[str, Any]], path: str) -> None:
    """Write the rollback point durably: temp file, fsync, atomic rename."""
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(target):
        raise SystemExit(
            f"Snapshot {path} already exists; refusing to overwrite a rollback point."
        )
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=directory or ".",
        prefix=f".{os.path.basename(target)}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass
        raise
    logger.info("Wrote rollback snapshot for %s properties to %s", len(rows), path)


def _restore(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        try:
            rows = json.load(handle)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"Snapshot {path} is not valid JSON ({e}); refusing to restore "
                "from a corrupt rollback point."
            ) from e
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and "id" in row and "municipality" in row for row in rows
    ):
        raise SystemExit(
            f"Snapshot {path} is not a municipality snapshot "
            '(expected a list of {"id", "municipality"} rows); refusing to '
            "restore from it."
        )

    restored = 0
    for row in rows:
        prop = db.session.get(Property, row["id"])
        if not prop:
            logger.warning("Property %s from snapshot no longer exists", row["id"])
            continue
        prop.municipality = row["municipality"]
        restored += 1
    db.session.commit()
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve email-truncated municipalities against stored full names."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--map",
        action="append",
        dest="mappings",
        metavar='"TRUNCATED=FULL"',
        help=(
            "Explicit mapping for a row the tool refuses to auto-resolve "
            "(stem ending at de/del/la/...). Repeatable."
        ),
    )
    parser.add_argument(
        "--snapshot",
        help="Path for the rollback snapshot. Required unless --dry-run/--restore.",
    )
    parser.add_argument(
        "--restore",
        help="Restore municipality values from a snapshot, then exit.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.restore and not args.snapshot:
        parser.error("--snapshot is required (or use --dry-run / --restore)")

    mappings = parse_mappings(args.mappings)

    app = create_app()
    with app.app_context():
        if args.restore:
            logger.info(
                "Restored %s properties from %s", _restore(args.restore), args.restore
            )
            return

        properties = Property.query.order_by(Property.id.asc()).all()
        plan = plan_changes(properties, mappings)
        resolvable = [entry for entry in plan if entry.full]
        for entry in plan:
            if entry.full:
                logger.info(
                    "property %s: %r -> %r (%s)",
                    entry.prop.id,
                    entry.prop.municipality,
                    entry.full,
                    entry.kind,
                )
            elif entry.kind == KIND_NEEDS_MAPPING:
                logger.warning(
                    "property %s: %r needs an explicit mapping -- its stem "
                    'ends at a generic connective; supply --map "%s=<full name>"',
                    entry.prop.id,
                    entry.prop.municipality,
                    entry.prop.municipality,
                )
            else:
                logger.info(
                    "property %s: %r stays -- no unique stored full name",
                    entry.prop.id,
                    entry.prop.municipality,
                )
        matched = {
            entry.prop.municipality for entry in plan if entry.kind == KIND_MAPPED
        }
        for unused in sorted(set(mappings) - matched):
            logger.warning("--map %r matched no truncated row (typo?)", unused)
        logger.info(
            "Truncated municipalities: %s of %s rows "
            "(%s auto, %s mapped, %s need mapping, %s unmatched)",
            len(plan),
            len(properties),
            sum(1 for e in plan if e.kind == KIND_AUTO),
            sum(1 for e in plan if e.kind == KIND_MAPPED),
            sum(1 for e in plan if e.kind == KIND_NEEDS_MAPPING),
            sum(1 for e in plan if e.kind == KIND_UNMATCHED),
        )
        if args.dry_run or not resolvable:
            return

        _write_snapshot(
            [_snapshot_row(entry.prop) for entry in resolvable], args.snapshot
        )

        resolved, failed = apply_plan(plan)

        logger.info(
            "Done. truncated=%s resolved=%s kept=%s failed=%s",
            len(plan),
            resolved,
            len(plan) - len(resolvable),
            failed,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
