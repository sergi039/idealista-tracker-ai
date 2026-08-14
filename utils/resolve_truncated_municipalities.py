"""Resolve the municipalities the alert emails cut off (issue #298).

Idealista alert emails truncate long location strings with an ellipsis and
ingestion stored them verbatim, so `properties.municipality` came to hold
"Ovi..." twice, "Ovied...", "Mieres de..." and six more like them while the
full siblings ("Oviedo", "Mieres Del Camino") sat in neighbouring rows. The
/properties municipality dropdown offered each artifact as a municipality of
its own, and filtering by the full name silently missed the truncated rows.

Ingestion resolves new arrivals now (`PropertyIMAPService`), and the filter
options exclude whatever still carries the marker (`routes/main_routes.py`) --
but neither touches the rows already stored. This does, and it costs nothing:
no Google call, no Overpass call, just a rewrite of one text column against
names already in the table. A row whose stem exactly one stored full name
starts with is resolved to that name; every other truncated row is left
verbatim -- the marker stays, explicitly non-canonical, never guessed (the
same rule as #98: missing data stays explicit).

    python -m utils.resolve_truncated_municipalities --dry-run
    python -m utils.resolve_truncated_municipalities --snapshot data/municipality_truncation.json
    python -m utils.resolve_truncated_municipalities --restore data/municipality_truncation.json

Idempotent by construction: a resolved row no longer ends in the marker, so a
second run does not select it, and a row that could not be resolved resolves
to nothing next time too (this tool adds no new full names). The snapshot
holds only the rows the run will rewrite, one commit per row, so an
interrupted run has written exactly the rows it logged.
"""

import argparse
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from app import create_app, db
from models import Property
from utils.idealista_extractors import (
    is_truncated_municipality,
    resolve_truncated_municipality,
)

logger = logging.getLogger(__name__)


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


def plan_changes(
    properties: List[Property],
) -> List[Tuple[Property, Optional[str]]]:
    """(row, full name or None) for every truncated row.

    None means the stem matched no stored full name uniquely and the row
    keeps its marker.
    """
    known = known_municipalities(properties)
    return [
        (prop, resolve_truncated_municipality(prop.municipality, known))
        for prop in truncated_rows(properties)
    ]


def apply_plan(plan: List[Tuple[Property, Optional[str]]]) -> Tuple[int, int]:
    """Rewrite the resolvable rows, one commit per row.

    Returns (resolved, failed). Rows planned as None are never written --
    they keep their marker.
    """
    resolved = 0
    failed = 0
    for prop, full in plan:
        if not full:
            continue
        try:
            prop.municipality = full
            db.session.commit()
            resolved += 1
        except Exception as e:
            failed += 1
            logger.warning("Failed for property %s: %s", prop.id, e)
            db.session.rollback()
    return resolved, failed


def _snapshot_row(prop: Property) -> Dict[str, Any]:
    return {"id": prop.id, "municipality": prop.municipality}


def _write_snapshot(rows: List[Dict[str, Any]], path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        raise SystemExit(
            f"Snapshot {path} already exists; refusing to overwrite a rollback point."
        )
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    logger.info("Wrote rollback snapshot for %s properties to %s", len(rows), path)


def _restore(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)

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

    app = create_app()
    with app.app_context():
        if args.restore:
            logger.info(
                "Restored %s properties from %s", _restore(args.restore), args.restore
            )
            return

        properties = Property.query.order_by(Property.id.asc()).all()
        plan = plan_changes(properties)
        resolvable = [(prop, full) for prop, full in plan if full]
        for prop, full in plan:
            if full:
                logger.info("property %s: %r -> %r", prop.id, prop.municipality, full)
            else:
                logger.info(
                    "property %s: %r stays -- no unique stored full name",
                    prop.id,
                    prop.municipality,
                )
        logger.info(
            "Truncated municipalities: %s of %s rows (%s resolvable, %s kept verbatim)",
            len(plan),
            len(properties),
            len(resolvable),
            len(plan) - len(resolvable),
        )
        if args.dry_run or not resolvable:
            return

        _write_snapshot([_snapshot_row(prop) for prop, _ in resolvable], args.snapshot)

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
