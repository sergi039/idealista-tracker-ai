"""Recalculate `Property.travel` (and the scores that read it) with Google.

`utils/recalc_travel_times.py` only knows the legacy `Land`. Universal
properties -- every listing ingested since `INGESTION_TARGET` became
`properties` -- had no bulk path at all, only the per-listing Enrich button.

**This run spends real money.** Places Nearby Search resolves each preset
target and Distance Matrix measures the route to it, so a full pass over the
owner's listings is thousands of billable calls. Never run it without the
owner asking for it in as many words (CLAUDE.md, "Hard rules").

It exists because #171 taught the airport preset to refuse helipads and
businesses that merely carry Google's `airport` tag: the code no longer picks
them, but the rows already stored still name them. Only a recalculation
rewrites those.

A run overwrites `travel`, `score_total`, `score_investment`,
`score_lifestyle` and `scoring`, and rolling the application back does not
roll a data rewrite back, so it writes a snapshot of exactly those fields
first:

    python -m utils.recalc_property_travel --snapshot data/travel_backfill.json
    python -m utils.recalc_property_travel --restore data/travel_backfill.json
"""

import argparse
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app import create_app, db
from models import Property
from services.property_scoring_service import PropertyScoringService
from services.property_travel_service import PropertyTravelService
from utils.inflight import inflight

logger = logging.getLogger(__name__)

SNAPSHOT_FIELDS = (
    "travel",
    "score_total",
    "score_investment",
    "score_lifestyle",
    "scoring",
)


def _decimal_str(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _snapshot_row(prop: Property) -> Dict[str, Any]:
    return {
        "id": prop.id,
        "travel": prop.travel,
        "score_total": _decimal_str(prop.score_total),
        "score_investment": _decimal_str(prop.score_investment),
        "score_lifestyle": _decimal_str(prop.score_lifestyle),
        "scoring": prop.scoring,
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
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)

    restored = 0
    for row in rows:
        prop = db.session.get(Property, row["id"])
        if not prop:
            logger.warning("Property %s from snapshot no longer exists", row["id"])
            continue
        prop.travel = row["travel"]
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
        restored += 1
    db.session.commit()
    return restored


def _target_places(travel: Any) -> Dict[str, Optional[str]]:
    """`{target key: resolved place name}`, for reporting what actually moved."""
    if not isinstance(travel, dict):
        return {}
    targets = travel.get("targets")
    if not isinstance(targets, dict):
        return {}
    out: Dict[str, Optional[str]] = {}
    for key, value in targets.items():
        place = value.get("place") if isinstance(value, dict) else None
        out[key] = place.get("name") if isinstance(place, dict) else None
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recalculate travel targets for universal properties. "
            "Spends Google Places and Distance Matrix quota."
        )
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number processed (0 = all)."
    )
    parser.add_argument(
        "--ids", help="Comma-separated property ids, instead of every property."
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.1,
        help="Pause between properties, in seconds.",
    )
    parser.add_argument(
        "--snapshot",
        help="Path for the rollback snapshot. Required unless --restore.",
    )
    parser.add_argument(
        "--restore",
        help="Restore travel and scores from a snapshot file, then exit.",
    )
    parser.add_argument(
        "--report",
        help="Write a per-property before/after report of resolved places here.",
    )
    args = parser.parse_args()

    if not args.restore and not args.snapshot:
        parser.error("--snapshot is required (or use --restore)")

    app = create_app()
    with app.app_context():
        if args.restore:
            restored = _restore(args.restore)
            logger.info("Restored %s properties from %s", restored, args.restore)
            print(f"restored={restored}")
            return

        query = Property.query.filter(
            Property.location_lat.isnot(None), Property.location_lon.isnot(None)
        )
        if args.ids:
            wanted = [int(x) for x in args.ids.split(",") if x.strip()]
            query = query.filter(Property.id.in_(wanted))
        query = query.order_by(Property.id.asc())
        if args.limit:
            query = query.limit(args.limit)

        properties = query.all()
        logger.info("Recalculating travel for %s properties", len(properties))

        _write_snapshot([_snapshot_row(p) for p in properties], args.snapshot)

        travel_service = PropertyTravelService()
        scoring_service = PropertyScoringService()

        processed = 0
        failed = 0
        changed_places: List[Dict[str, Any]] = []

        # Never resumable: the scope is every matching property on each run,
        # with no "already answered" filter, so an interrupted run re-bills
        # Places and Distance Matrix for everything it finished - and the
        # --report file is only written at the end, so that is lost outright.
        # Narrow a restart with --ids by hand (#283).
        with inflight("recalc_property_travel", resumable=False):
            for prop in properties:
                before = _target_places(prop.travel)
                try:
                    ok = travel_service.calculate_for_property(prop, commit=False)
                except Exception:
                    logger.exception(
                        "Travel recalculation failed for property %s", prop.id
                    )
                    db.session.rollback()
                    failed += 1
                    continue

                if not ok:
                    # Every target refused or unanswerable.
                    # `calculate_for_property` has already recorded that on the
                    # row; it is not a crash.
                    failed += 1

                try:
                    scoring_service.calculate_for_property(prop, commit=False)
                except Exception:
                    logger.exception("Rescoring failed for property %s", prop.id)

                db.session.commit()
                processed += 1

                after = _target_places(prop.travel)
                moved = {
                    key: {"before": before.get(key), "after": after.get(key)}
                    for key in sorted(set(before) | set(after))
                    if before.get(key) != after.get(key)
                }
                if moved:
                    changed_places.append({"id": prop.id, "targets": moved})

                if args.sleep:
                    time.sleep(args.sleep)

        summary = {
            "processed": processed,
            "failed": failed,
            "properties_with_a_changed_place": len(changed_places),
        }
        logger.info("Done: %s", summary)
        print(json.dumps(summary))

        if args.report:
            with open(args.report, "w", encoding="utf-8") as handle:
                json.dump(changed_places, handle, ensure_ascii=False, indent=2)
            logger.info("Wrote change report to %s", args.report)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    main()
