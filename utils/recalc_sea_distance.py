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
import logging
import time
from typing import Any, Dict, List

from app import create_app, db
from models import Property
from services.property_scoring_service import PropertyScoringService
from services.sea_distance_service import SeaDistanceService
from utils import score_snapshot
from utils.enrich_scope import log_scope
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
    return score_snapshot.snapshot_row(prop, json_columns=("scoring", "enrichment"))


def _write_snapshot(rows: List[Dict[str, Any]], path: str) -> None:
    score_snapshot.write_rows(rows, path)


def _restore(path: str) -> int:
    restored, _missing = score_snapshot.restore_file(path)
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
        log_scope(
            logger,
            properties,
            label="sea_distance_recalc_queue",
            notes=(
                "free: the coastline comes from the sea-view client's cached cells",
            ),
        )
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
                        # The row's own accuracy, exactly as the real arm reads
                        # it below. Omitted here since #358, this preview called
                        # every row a locality centroid, precise ones included —
                        # the number an operator reads before authorising the
                        # rewrite this flag exists to hold back.
                        result = sea_service.measure(
                            float(prop.location_lat),
                            float(prop.location_lon),
                            prop.location_accuracy,
                        )
                    else:
                        # `commit=True` so the write happens under FOR UPDATE:
                        # this tool and any other writer of `enrichment` were
                        # overwriting each other's blocks (#339/#352). It owns
                        # and ends its own transaction, so scoring follows in a
                        # second one.
                        result = sea_service.update_property(prop, commit=True)
                        scoring_service.calculate_for_property(prop, commit=True)
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
