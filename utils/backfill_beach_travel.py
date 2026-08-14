"""Scoped beach/travel backfill for properties (issue #271, Phase 2).

The owner's scope rule (2026-08-14): enrich automatically only listings from
the **last N days (default 30) plus favorites** — everything older stays
manual via the per-property Enrich button. Within that scope the tool re-runs
the travel pass (which is what carries the beaches payload) for rows that
have **no beaches key or a refused one** (`status: unavailable`); a measured
answer, including a measured "no beach within the limit", leaves the scope,
which is what makes a rerun resumable and idempotent.

Money and rollback, per the agreed proposal:
- a rollback snapshot of the score/travel columns is written first — rolling
  the app back does not undo a data rewrite;
- `--max-rows` is the hard cap: rows beyond it are *deferred*, reported as
  such, and picked up by the next run (they are still in scope);
- every processed row is appended to a JSONL ledger (outcome + statuses +
  timestamp) so an interrupted run reconciles against a complete one;
- a refusal is recorded, never cached, and never overwrites measured data —
  that is the travel service's own #98 contract, inherited here.

    python -m utils.backfill_beach_travel --dry-run
    python -m utils.backfill_beach_travel --snapshot data/beach_backfill.json
    python -m utils.backfill_beach_travel --restore data/beach_backfill.json
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app import create_app, db
from models import Property
from services.property_scoring_service import PropertyScoringService
from services.property_travel_service import PropertyTravelService
from utils.enrich_scope import scoped_properties
from utils.inflight import inflight

logger = logging.getLogger(__name__)


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
        "travel": prop.travel,
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
        prop.travel = row["travel"]
        restored += 1
    db.session.commit()
    return restored


def needs_beaches(prop: Property) -> bool:
    """No beaches key, or a lookup that never answered. A measured answer —
    items, or a measured absence — is done and must not be re-billed."""
    travel = prop.travel if isinstance(prop.travel, dict) else {}
    beaches = travel.get("beaches")
    if not isinstance(beaches, dict):
        return True
    return beaches.get("status") == "unavailable"


def select_scope(days: int, include_all: bool = False) -> List[Property]:
    """Coordinates present, beaches missing/refused, and — unless --all —
    created in the last `days` days or marked favorite (the owner's rule,
    shared with every Phase-2 backfill via utils/enrich_scope.py)."""
    return scoped_properties(days=days, include_all=include_all, needs=needs_beaches)


def run(
    properties: List[Property],
    ledger_path: str,
    max_rows: int = 0,
    sleep_s: float = 0.2,
    travel_service: Optional[PropertyTravelService] = None,
    scoring_service: Optional[PropertyScoringService] = None,
) -> Dict[str, Any]:
    travel_service = travel_service or PropertyTravelService()
    scoring_service = scoring_service or PropertyScoringService()

    to_process = properties[:max_rows] if max_rows else properties
    deferred = len(properties) - len(to_process)
    counts: Dict[str, int] = {}
    failed = 0

    with open(ledger_path, "a", encoding="utf-8") as ledger:
        for idx, prop in enumerate(to_process, start=1):
            entry: Dict[str, Any] = {
                "id": prop.id,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                travel_service.calculate_for_property(prop, commit=False)
                scoring_service.calculate_for_property(prop, commit=False)
                db.session.commit()

                travel = prop.travel if isinstance(prop.travel, dict) else {}
                api_status = travel.get("api_status") or {}
                beaches = travel.get("beaches") or {}
                entry["outcome"] = api_status.get("state") or "unknown"
                entry["beaches_status"] = beaches.get("status")
                entry["beaches_found"] = len(beaches.get("items") or [])
            except Exception as exc:
                db.session.rollback()
                failed += 1
                entry["outcome"] = "failed"
                entry["error"] = str(exc)[:200]
                logger.warning("Failed for property %s: %s", prop.id, exc)

            counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
            ledger.write(json.dumps(entry, ensure_ascii=False) + "\n")
            ledger.flush()

            if idx % 20 == 0:
                logger.info(
                    "Progress %s/%s outcomes=%s failed=%s",
                    idx,
                    len(to_process),
                    counts,
                    failed,
                )
            if sleep_s:
                time.sleep(max(0.0, float(sleep_s)))

    report = {
        "selected": len(properties),
        "processed": len(to_process),
        "deferred": deferred,
        "outcomes": counts,
        "failed": failed,
    }
    with open(ledger_path, "a", encoding="utf-8") as ledger:
        ledger.write(json.dumps({"report": report}, ensure_ascii=False) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill beach/travel data for recent + favorite properties."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Auto-enrich window in days (owner rule, default 30).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Ignore the recent+favorites scope (needs its own ticket).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Hard cap; rows beyond it are deferred to the next run (0 = all).",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.2, help="Sleep between properties (seconds)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the scope and estimated budget without any API call.",
    )
    parser.add_argument(
        "--snapshot", help="Rollback snapshot path. Required unless --dry-run."
    )
    parser.add_argument(
        "--restore", help="Restore scores and travel from a snapshot, then exit."
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

        properties = select_scope(args.days, include_all=args.all)
        logger.info(
            "Scope: %s properties (days=%s, all=%s)",
            len(properties),
            args.days,
            args.all,
        )
        if args.dry_run:
            # Worst-case list price, cold caches: ≤7 Nearby + ≤25 DM elements
            # per row. The real spend is expected inside the monthly free caps.
            logger.info(
                "Dry run. Worst-case: ≤%s Nearby calls, ≤%s Distance Matrix "
                "elements. No API was called.",
                len(properties) * 7,
                len(properties) * 25,
            )
            return
        if not properties:
            logger.info("Nothing in scope; done.")
            return

        ledger_path = args.snapshot + ".ledger.jsonl"
        # Resumable for the reason stated at the top of this module: per-row
        # commit, an idempotent scope, and a ledger to reconcile against. That
        # claim is what lets the deploy chain kill this run knowingly (#283).
        with inflight("backfill_beach_travel", ledger=ledger_path, resumable=True):
            _write_snapshot([_snapshot_row(p) for p in properties], args.snapshot)
            report = run(
                properties,
                ledger_path,
                max_rows=args.max_rows,
                sleep_s=args.sleep,
            )
        logger.info("Done: %s (ledger: %s)", report, ledger_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
