"""Count nearby amenities for stored properties, from OpenStreetMap.

Like `backfill_sea_view.py` and unlike `bulk_ai_analysis.py` or
`recalc_travel_times.py`, this tool spends **no money**: Overpass is free and
keyless. That is also the only reason a backfill over hundreds of rows is on
the table at all.

    python -m utils.backfill_osm_amenities --only-missing --dry-run
    python -m utils.backfill_osm_amenities --only-missing --limit 20

It calls `EnrichmentService.enrich_osm_amenities` directly and never
`PropertyEnrichmentService.enrich_property`: that one also runs the Google
travel and Places calls, so looping it over the amenity-less rows would turn a
free fix into a paid sweep nobody asked for.

Pacing is the shared `utils.http.OVERPASS_GATE` -- overpass-api.de grants two
query slots per IP and answers 504 while both are busy -- plus `--sleep` on
top. Rows whose coordinates round to the same 4 decimals answer from the
enrichment cache without a query at all.

The legacy `lands` rows need no separate pass: they are mirrored into
`properties`, so backfilling properties is what puts amenities on the page.
"""

import argparse
import logging
import time
from collections import Counter

from app import create_app
from models import Property
from services.enrichment_service import (
    OSM_STATE_OK,
    OSM_STATUS_KEY,
    EnrichmentService,
)
from utils.enrich_scope import log_scope
from utils.inflight import inflight

logger = logging.getLogger(__name__)


def _osm_state(prop) -> str:
    """The state of the last amenity lookup for this property, or "" if none.

    Reads `Property.infrastructure_extended`, so a row mirrored from `lands`
    counts as measured when the legacy pass measured it -- re-querying those
    would spend the pacing budget on answers already on the page.
    """
    status = (prop.infrastructure_extended or {}).get(OSM_STATUS_KEY)
    if not isinstance(status, dict):
        # Counts with no status are a legacy write from before #144. They are
        # an answer, so leave them alone.
        if isinstance((prop.infrastructure_extended or {}).get("osm_amenities"), dict):
            return OSM_STATE_OK
        return ""
    return str(status.get("state") or "")


def backfill(
    properties,
    service: EnrichmentService,
    *,
    only_missing: bool = False,
    dry_run: bool = False,
    sleep_s: float = 0.0,
    sleep: object = time.sleep,
) -> Counter:
    """Run the amenity lookup over `properties`, and report what happened.

    `dry_run` still queries Overpass -- there is no way to learn what is nearby
    without asking -- but rolls the write back, so it is a rehearsal of the
    pacing and the refusal handling rather than of the database write.
    """
    outcome: Counter = Counter()
    total = len(properties)

    for index, prop in enumerate(properties, start=1):
        state = _osm_state(prop)
        if only_missing and state == OSM_STATE_OK:
            outcome["skipped"] += 1
            continue

        try:
            failure = service.enrich_osm_amenities(prop, commit=not dry_run)
        except Exception:
            outcome["error"] += 1
            logger.error("Amenity lookup failed for %s", prop.id, exc_info=True)
        else:
            outcome["refused" if failure is not None else "measured"] += 1
            if failure is not None:
                outcome[f"reason:{failure.reason}"] += 1

        if index % 25 == 0 or index == total:
            logger.info("%s/%s processed", index, total)
        if sleep_s:
            sleep(sleep_s)

    return outcome


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Count nearby amenities for properties (OpenStreetMap, free)."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit properties processed (0 = all)."
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Skip properties whose last lookup already answered.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and report, but roll the write back.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Extra pause between properties, on top of the shared Overpass gate.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = create_app()
    with app.app_context():
        from app import db

        properties = Property.query.order_by(Property.id.asc()).all()
        if args.limit:
            properties = properties[: args.limit]

        logger.info(
            "Amenity backfill over %s properties (only_missing=%s, dry_run=%s)",
            len(properties),
            args.only_missing,
            args.dry_run,
        )
        log_scope(
            logger,
            properties,
            label="osm_amenity_backfill_queue",
            notes=(
                "every stored row, not the auto-enrich window",
                "free: OpenStreetMap through the shared Overpass gate",
            ),
        )

        # Free, but paced at OVERPASS_MIN_INTERVAL_S: a restart without
        # --only-missing re-queries every row it already answered. Only the
        # scoped form is honestly resumable (#283).
        with inflight("backfill_osm_amenities", resumable=bool(args.only_missing)):
            outcome = backfill(
                properties,
                EnrichmentService(),
                only_missing=args.only_missing,
                dry_run=args.dry_run,
                sleep_s=args.sleep,
            )

        if args.dry_run:
            db.session.rollback()

        logger.info("--- outcome ---")
        for key, count in sorted(outcome.items()):
            logger.info("%-28s %s", key, count)


if __name__ == "__main__":
    main()
