"""Backfill the sea distance (and the score that depends on it) for properties.

The Overpass geometry is cached per grid cell, so a full run over every property
costs a handful of network requests rather than one per listing.

A run rewrites `score_total`, `score_investment`, `score_lifestyle`, `scoring`
and `enrichment`. Rolling the application back does not roll those columns back,
so the run writes a snapshot of exactly those fields first and can restore it:

    python -m utils.recalc_sea_distance --snapshot data/sea_backfill.json
    python -m utils.recalc_sea_distance --restore data/sea_backfill.json
"""

import argparse
import json
import logging
import os
import time
from typing import Any, Dict, List

from app import create_app, db
from models import Property
from services.property_scoring_service import PropertyScoringService
from services.sea_distance_service import SeaDistanceService
from utils.inflight import inflight

logger = logging.getLogger(__name__)

SNAPSHOT_FIELDS = (
    "score_total",
    "score_investment",
    "score_lifestyle",
    "scoring",
    "enrichment",
)


def _snapshot_row(prop: Property) -> Dict[str, Any]:
    return {
        "id": prop.id,
        "score_total": str(prop.score_total) if prop.score_total is not None else None,
        "score_investment": str(prop.score_investment)
        if prop.score_investment is not None
        else None,
        "score_lifestyle": str(prop.score_lifestyle)
        if prop.score_lifestyle is not None
        else None,
        "scoring": prop.scoring,
        "enrichment": prop.enrichment,
    }


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
    from decimal import Decimal

    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)

    restored = 0
    for row in rows:
        prop = db.session.get(Property, row["id"])
        if not prop:
            logger.warning("Property %s from snapshot no longer exists", row["id"])
            continue
        prop.score_total = (
            Decimal(row["score_total"]) if row["score_total"] is not None else None
        )
        prop.score_investment = (
            Decimal(row["score_investment"])
            if row["score_investment"] is not None
            else None
        )
        prop.score_lifestyle = (
            Decimal(row["score_lifestyle"])
            if row["score_lifestyle"] is not None
            else None
        )
        prop.scoring = row["scoring"]
        prop.enrichment = row["enrichment"]
        restored += 1
    db.session.commit()
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill sea distance and rescore properties."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number processed (0 = all)."
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only properties with no stored sea measurement.",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.0, help="Extra sleep between properties."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Measure and report without writing anything.",
    )
    parser.add_argument(
        "--snapshot",
        help="Path for the rollback snapshot. Required unless --dry-run.",
    )
    parser.add_argument(
        "--restore",
        help="Restore scores and enrichment from a snapshot file, then exit.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.restore and not args.snapshot:
        parser.error("--snapshot is required (or use --dry-run / --restore)")

    app = create_app()
    with app.app_context():
        if args.restore:
            restored = _restore(args.restore)
            logger.info("Restored %s properties from %s", restored, args.restore)
            return

        q = Property.query.filter(
            Property.location_lat.isnot(None), Property.location_lon.isnot(None)
        )
        q = q.order_by(Property.id.asc())
        if args.limit:
            q = q.limit(args.limit)

        properties = q.all()
        if args.only_missing:
            properties = [
                p
                for p in properties
                if not isinstance(p.enrichment, dict)
                or not isinstance(p.enrichment.get("sea"), dict)
            ]

        total = len(properties)
        logger.info("Selected %s properties", total)
        if not total:
            return

        if not args.dry_run:
            _write_snapshot([_snapshot_row(p) for p in properties], args.snapshot)

        sea_service = SeaDistanceService()
        scoring_service = PropertyScoringService()
        counts: Dict[str, int] = {}
        failed = 0

        # Only `--only-missing` skips rows already measured, so only that form
        # survives a restart without repeating the coastline work (#283).
        with inflight("recalc_sea_distance", resumable=bool(args.only_missing)):
            for idx, prop in enumerate(properties, start=1):
                try:
                    if args.dry_run:
                        result = sea_service.measure(
                            float(prop.location_lat), float(prop.location_lon)
                        )
                    else:
                        result = sea_service.update_property(prop, commit=False)
                        scoring_service.calculate_for_property(prop, commit=False)
                        db.session.commit()
                    status = (result or {}).get("status", "disabled")
                    counts[status] = counts.get(status, 0) + 1
                except Exception as e:
                    failed += 1
                    logger.warning("Failed for property %s: %s", prop.id, e)
                    db.session.rollback()

                if idx % 25 == 0:
                    logger.info(
                        "Progress %s/%s statuses=%s failed=%s",
                        idx,
                        total,
                        counts,
                        failed,
                    )

                if args.sleep:
                    time.sleep(max(0.0, float(args.sleep)))

        logger.info(
            "Done. total=%s statuses=%s failed=%s dry_run=%s",
            total,
            counts,
            failed,
            args.dry_run,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
