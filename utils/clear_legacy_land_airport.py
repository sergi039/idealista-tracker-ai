"""Drop the airport a legacy `Land` never had.

Until the fix in `EnrichmentService._airport_candidates`, the legacy Places
lookup took `min(type=airport results, key=distance)` with no name rule, so a
hospital helipad six kilometres away was written down as the nearest airport.
Measured on 2026-08-11 across the owner's 168 lands, the stored
`transport["airport_distance"]` sat at a median **0.27x** the straight-line
distance to the real airport, while `Land.distance_airport` — filled by
Distance Matrix on another path — sat at 1.53x, a genuine road distance to the
genuine airport. 145 of the 168 disagreed by more than 3 km, and `/lands/15`
showed both at once: "Airport Distance 56min 85km" above "Airport Distance
8min".

Fixing the code does not fix those rows. Enrichment `.update()`s the JSON
rather than replacing it, and the bulk enrich endpoint only selects lands whose
enrichment is empty — so every one of the 168 keeps its helipad until someone
re-enriches it by hand. This clears the three keys instead, which costs
nothing: no Google call, no Overpass call, just a delete. The next enrichment
run of a land fills them back in correctly.

    python -m utils.clear_legacy_land_airport --dry-run
    python -m utils.clear_legacy_land_airport --snapshot data/land_airport.json
    python -m utils.clear_legacy_land_airport --restore data/land_airport.json

`ScoringService._score_transport` reads `airport_available` and
`airport_distance`, so a cleared land's stored score still embeds the helipad
until it is recomputed. `--rescore` does that (also free — Land scoring reads
only stored JSON). It is opt-in because it recomputes *every* criterion, not
just transport, and the lands have not been scored since 2026-02-18, so the
two effects arrive together. Measured on 2026-08-11: clearing the airport keys
alone moves 158 of the 168 by a median +1.26 points of `score_total` (removing
a mediocre option raises the average of the rest), while unrelated scoring
changes since February move 78 of them on their own. `--dry-run --rescore`
reports the combined per-land delta first, so that is a decision and not a
surprise.
"""

import argparse
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app import create_app, db
from models import Land
from services.scoring_service import ScoringService

logger = logging.getLogger(__name__)

# The three keys the unfiltered airport search wrote. Nothing else in
# `transport` came from it.
AIRPORT_KEYS = ("airport_available", "airport_distance", "airport_travel_time")


def _snapshot_row(land: Land) -> Dict[str, Any]:
    return {
        "id": land.id,
        "transport": land.transport,
        "environment": land.environment,
        "score_total": str(land.score_total) if land.score_total is not None else None,
        "score_investment": str(land.score_investment)
        if land.score_investment is not None
        else None,
        "score_lifestyle": str(land.score_lifestyle)
        if land.score_lifestyle is not None
        else None,
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
    logger.info("Wrote rollback snapshot for %s lands to %s", len(rows), path)


def _decimal(value: Optional[str]) -> Optional[Decimal]:
    return Decimal(value) if value is not None else None


def _restore(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)

    restored = 0
    for row in rows:
        land = db.session.get(Land, row["id"])
        if not land:
            logger.warning("Land %s from snapshot no longer exists", row["id"])
            continue
        land.transport = row["transport"]
        land.environment = row["environment"]
        land.score_total = _decimal(row["score_total"])
        land.score_investment = _decimal(row["score_investment"])
        land.score_lifestyle = _decimal(row["score_lifestyle"])
        restored += 1
    db.session.commit()
    return restored


def _cleared(transport: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """`transport` without the airport keys.

    Returns a fresh dict rather than mutating in place: `transport` is a JSON
    column, and SQLAlchemy only notices the top-level value being replaced.
    """
    return {k: v for k, v in (transport or {}).items() if k not in AIRPORT_KEYS}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove the unfiltered airport measurement from legacy lands."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Recompute land scores after clearing (free; recomputes every criterion).",
    )
    parser.add_argument(
        "--snapshot",
        help="Path for the rollback snapshot. Required unless --dry-run/--restore.",
    )
    parser.add_argument(
        "--restore",
        help="Restore transport, environment and scores from a snapshot, then exit.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.restore and not args.snapshot:
        parser.error("--snapshot is required (or use --dry-run / --restore)")

    app = create_app()
    with app.app_context():
        if args.restore:
            logger.info(
                "Restored %s lands from %s", _restore(args.restore), args.restore
            )
            return

        lands = [
            land
            for land in Land.query.order_by(Land.id.asc()).all()
            if any(key in (land.transport or {}) for key in AIRPORT_KEYS)
        ]
        logger.info("Lands carrying an unfiltered airport measurement: %s", len(lands))
        if not lands:
            return

        if not args.dry_run:
            _write_snapshot([_snapshot_row(land) for land in lands], args.snapshot)

        scoring = ScoringService() if args.rescore else None
        moved = 0
        failed = 0

        for land in lands:
            before = land.score_total
            try:
                if args.dry_run:
                    if scoring is not None:
                        # Score the cleared shape without persisting it.
                        # `calculate_score` runs queries of its own, so this
                        # holds autoflush off rather than trusting the final
                        # rollback: a preview must not reach the database at
                        # all, least of all a production one.
                        with db.session.no_autoflush:
                            land.transport = _cleared(land.transport)
                            after = scoring.calculate_score(land)
                        if after != before:
                            moved += 1
                            logger.info(
                                "land %s score %s -> %s", land.id, before, after
                            )
                    continue

                land.transport = _cleared(land.transport)
                if scoring is not None:
                    after = scoring.calculate_score(land)
                    if after != before:
                        moved += 1
                db.session.commit()
            except Exception as e:
                failed += 1
                logger.warning("Failed for land %s: %s", land.id, e)
                db.session.rollback()

        if args.dry_run:
            db.session.rollback()

        logger.info(
            "Done. lands=%s scores_moved=%s failed=%s dry_run=%s rescore=%s",
            len(lands),
            moved,
            failed,
            args.dry_run,
            args.rescore,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
