"""Scoped pool backfill (proposal D17; issue #271's scope rule).

Auto-scope = last 30 days + favorites (utils/enrich_scope.py); older
listings stay manual via Enrich. Paid, but tiny: per property ≤3 Distance
Matrix elements, plus one Places Text Search only on the OSM-empty path —
both inside the monthly free caps at this app's volume, and both counted in
the ledger. A rollback snapshot of the score columns is written first even
though the criterion ships at weight 0: rolling the app back does not undo
a data rewrite, and the snapshot is cheap insurance either way.

    python -m utils.backfill_pool --dry-run
    python -m utils.backfill_pool --snapshot data/pool_backfill.json
    python -m utils.backfill_pool --restore data/pool_backfill.json
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from app import create_app, db
from models import Property
from services.pool_service import PoolService
from services.property_scoring_service import PropertyScoringService
from utils.enrich_scope import log_scope, scoped_properties
from utils import score_snapshot
from utils.inflight import inflight

logger = logging.getLogger(__name__)

RETRYABLE = {"unavailable", "pending_measurement"}


def needs_pool(prop: Property) -> bool:
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    pool = enrichment.get("pool")
    if not isinstance(pool, dict):
        return True
    return pool.get("status") in RETRYABLE


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
        description="Backfill pool data for recent + favorite properties."
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--snapshot")
    parser.add_argument("--restore")
    args = parser.parse_args()

    if not args.dry_run and not args.restore and not args.snapshot:
        parser.error("--snapshot is required (or use --dry-run / --restore)")

    app = create_app()
    with app.app_context():
        if args.restore:
            logger.info("Restored %s properties", _restore(args.restore))
            return

        properties = scoped_properties(
            days=args.days, include_all=args.all, needs=needs_pool
        )
        logger.info("Scope: %s properties (days=%s)", len(properties), args.days)
        log_scope(
            logger,
            properties,
            label="pool_backfill_queue",
            notes=(
                f"auto-enrich window: last {args.days} days or a favorite, located rows only",
                "profile-agnostic on purpose (#410): a hidden subscription keeps ingesting",
                f"worst case: <={len(properties) * 3} Distance Matrix elements + <={len(properties)} Places Text Search",
            ),
        )
        if args.dry_run:
            logger.info(
                "Dry run. Worst-case: ≤%s DM elements + ≤%s Text Search "
                "(absence path only). No API was called.",
                len(properties) * 3,
                len(properties),
            )
            return
        if not properties:
            return

        ledger_path = args.snapshot + ".ledger.jsonl"

        to_process = properties[: args.max_rows] if args.max_rows else properties
        deferred = len(properties) - len(to_process)
        service = PoolService()
        scoring = PropertyScoringService()
        counts: Dict[str, int] = {}
        failed = 0
        # Resumable: the commit below is per property and `needs_pool` drops a
        # measured row from the scope, so a run the deploy chain kills repeats
        # at most the property that was in flight (#283). The marker is taken
        # before the snapshot, so a rerun that stops on the "snapshot exists"
        # guard still reports the run it is a rerun of.
        with inflight("backfill_pool", ledger=ledger_path, resumable=True):
            _write_snapshot([_snapshot_row(p) for p in properties], args.snapshot)
            with open(ledger_path, "a", encoding="utf-8") as ledger:
                for idx, prop in enumerate(to_process, start=1):
                    entry: Dict[str, Any] = {
                        "id": prop.id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    try:
                        # `commit=True` so the write happens under FOR UPDATE:
                        # a second run of this module measuring the same row
                        # overwrote two good measurements with its refusals on
                        # 2026-08-16 (#339). It owns and ends its own
                        # transaction, so scoring follows in a second one --
                        # `pool_score` reads the block this just wrote, and a
                        # row briefly holding new pool data and a score from a
                        # moment earlier is the same state every recompute
                        # passes through.
                        part = service.enrich(prop, commit=True)
                        scoring.calculate_for_property(prop, commit=True)
                        entry["status"] = part.get("status")
                        entry["candidates"] = len(part.get("candidates") or [])
                        entry["cross_check"] = (part.get("cross_check") or {}).get(
                            "ran"
                        )
                    except Exception as exc:
                        db.session.rollback()
                        failed += 1
                        entry["status"] = "failed"
                        entry["error"] = str(exc)[:200]
                        logger.warning("Pool backfill failed for %s: %s", prop.id, exc)
                    counts[entry["status"]] = counts.get(entry["status"], 0) + 1
                    ledger.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    ledger.flush()
                    if idx % 20 == 0:
                        logger.info(
                            "Progress %s/%s statuses=%s failed=%s",
                            idx,
                            len(to_process),
                            counts,
                            failed,
                        )
                    if args.sleep:
                        time.sleep(max(0.0, float(args.sleep)))

        report = {
            "selected": len(properties),
            "processed": len(to_process),
            "deferred": deferred,
            "statuses": counts,
            "failed": failed,
        }
        with open(ledger_path, "a", encoding="utf-8") as ledger:
            ledger.write(json.dumps({"report": report}, ensure_ascii=False) + "\n")
        logger.info("Done: %s (ledger: %s)", report, ledger_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
