"""Scoped quality-of-life backfill (Phase 2; free, like backfill_sea_view).

Fills ``enrichment["quality_of_life"]`` for the owner's auto-enrich scope —
last 30 days plus favorites (utils/enrich_scope.py); everything older stays
manual via the per-property Enrich button, which runs the same service.

Free by construction: INE and CNH come from local reference files, and the
supermarket lookup goes through the shared Overpass client — gated at
`OVERPASS_MIN_INTERVAL_S`, so a full pass is paced by the transport, not by
this loop. No score reads the block, so there is no score snapshot to write;
the block is additive JSON and removable without a migration.

    python -m utils.backfill_quality_of_life --dry-run
    python -m utils.backfill_quality_of_life
"""

import argparse
import logging
import time
from typing import Dict

from app import create_app, db
from models import Property
from services.quality_of_life_service import RETRYABLE_STATUSES, QualityOfLifeService
from utils.enrich_scope import log_scope, scoped_properties, window_note
from utils.inflight import inflight

logger = logging.getLogger(__name__)


def needs_quality_of_life(prop: Property) -> bool:
    """No block yet, or any part a rerun could actually improve.

    `not_matched`/`osm_empty`/`no_municipality` are answers — re-asking does
    not change them, so they never pull a row back into scope (this pass is
    not a way to re-query Overpass weekly for free). A part kept from a
    previous measurement with a stamped failed attempt is an answer too.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    block = enrichment.get("quality_of_life")
    if not isinstance(block, dict):
        return True
    for part in ("municipality", "supermarkets", "hospitals"):
        entry = block.get(part)
        if not isinstance(entry, dict):
            return True
        if entry.get("status") in RETRYABLE_STATUSES:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill the quality-of-life block for recent + favorites."
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Ignore the recent+favorites scope (free, but still say why).",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 = all in scope.")
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        properties = scoped_properties(
            days=args.days, include_all=args.all, needs=needs_quality_of_life
        )
        if args.limit:
            properties = properties[: args.limit]
        logger.info("Scope: %s properties (days=%s)", len(properties), args.days)
        log_scope(
            logger,
            properties,
            label="quality_of_life_backfill_queue",
            notes=(
                window_note(args.days, args.all),
                "free: INE and CNH read local files, supermarkets go through the shared Overpass gate",
            ),
        )
        if args.dry_run or not properties:
            return

        service = QualityOfLifeService()
        status_counts: Dict[str, Dict[str, int]] = {}
        failed = 0
        # Resumable: per-row commit, and `needs_quality_of_life` drops a row
        # once every part answered, so a killed run repeats one property at
        # most (#283). It keeps no ledger, so the marker names none.
        with inflight("backfill_quality_of_life", resumable=True):
            for idx, prop in enumerate(properties, start=1):
                try:
                    # `commit=True` so the write happens under FOR UPDATE: this
                    # tool and any other writer of `enrichment` were
                    # overwriting each other's blocks (#339/#352).
                    payload = service.enrich(prop, commit=True)
                    for part in ("municipality", "supermarkets", "hospitals"):
                        status = (payload.get(part) or {}).get("status") or "missing"
                        per_part = status_counts.setdefault(part, {})
                        per_part[status] = per_part.get(status, 0) + 1
                except Exception as exc:
                    db.session.rollback()
                    failed += 1
                    logger.warning("QoL backfill failed for %s: %s", prop.id, exc)

                if idx % 20 == 0:
                    logger.info(
                        "Progress %s/%s statuses=%s failed=%s",
                        idx,
                        len(properties),
                        status_counts,
                        failed,
                    )
                if args.sleep:
                    time.sleep(max(0.0, float(args.sleep)))

        logger.info(
            "Done. total=%s statuses=%s failed=%s",
            len(properties),
            status_counts,
            failed,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
