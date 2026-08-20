"""Scoped hazardous-neighbour backfill (#437; free, like backfill_sea_view).

Fills ``enrichment["hazards"]`` for the owner's auto-enrich scope -- last 30
days plus favorites (utils/enrich_scope.py); everything older stays manual via
the per-property Enrich button, which runs the same service.

Free by construction: one Overpass query per listing, through the shared
client and its 5 s gate, so a full pass is paced by the transport rather than
by this loop. No Google API is touched and no score reads the block at weight
0, so there is no score snapshot to write; the block is additive JSON and
removable without a migration.

**Announce it before running it on the mini**, and run `tools/
backfill_status.sh` first: `busy` and `unknown` are a stop, not an input to a
judgement (owner decision 2026-08-17). This writer takes the row under
`FOR UPDATE` through `services/enrichment_write.locked_write`, which is what
keeps two runs from writing each other's measurements away (#339) -- and that
protects the *column*, not the hours of somebody else's run it would race.

It also raises this project's Overpass traffic, which is the one thing #434
says is fragile here: on 2026-08-20 all three configured instances were
unusable from the mini at once. A run that walks into that records
`unavailable` per row and changes nothing else -- the rows stay in scope and
the next run picks them up -- but it is worth checking that Overpass answers
this machine at all before starting a long one.

    python -m utils.backfill_hazards --dry-run
    python -m utils.backfill_hazards
"""

import argparse
import logging
import time
from typing import Dict

from app import create_app, db
from services.hazard_service import HazardService, needs_hazards
from utils.enrich_scope import log_scope, scoped_properties, window_note
from utils.inflight import inflight

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill the hazardous-neighbour block for recent + favorites."
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
            days=args.days, include_all=args.all, needs=needs_hazards
        )
        if args.limit:
            properties = properties[: args.limit]
        log_scope(
            logger,
            properties,
            label="hazard_backfill_queue",
            notes=(
                window_note(args.days, args.all),
                "free: one OpenStreetMap query per listing through the shared Overpass gate",
            ),
        )
        if args.dry_run or not properties:
            return

        service = HazardService()
        counts: Dict[str, int] = {}
        failed = 0
        # Resumable: per-row commit, and `needs_hazards` drops a row the
        # moment it holds a measured status, so a killed run repeats one
        # property at most (#283). `unavailable` deliberately stays in scope
        # -- that is a refusal, not an answer. No ledger, so the marker names
        # none.
        with inflight("backfill_hazards", resumable=True):
            for idx, prop in enumerate(properties, start=1):
                try:
                    # `commit=True` so the write happens under FOR UPDATE:
                    # every writer of `enrichment` was overwriting the others'
                    # blocks until it did (#339/#352).
                    payload = service.enrich(prop, commit=True)
                    status = payload.get("status") or "missing"
                    counts[status] = counts.get(status, 0) + 1
                except Exception as exc:
                    db.session.rollback()
                    failed += 1
                    logger.warning("Hazard backfill failed for %s: %s", prop.id, exc)

                if idx % 20 == 0:
                    logger.info(
                        "Progress %s/%s statuses=%s failed=%s",
                        idx,
                        len(properties),
                        counts,
                        failed,
                    )
                if args.sleep:
                    time.sleep(max(0.0, float(args.sleep)))

        logger.info(
            "Done. total=%s statuses=%s failed=%s", len(properties), counts, failed
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
