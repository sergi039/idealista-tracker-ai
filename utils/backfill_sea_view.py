"""Compute the sea-view verdict for stored properties.

Unlike `bulk_ai_analysis.py` and `recalc_travel_times.py` this tool spends no
money: OpenStreetMap and OpenTopoData are free and keyless. It is still slow on
purpose -- OpenTopoData's public instance asks for one call per second, and the
`--sleep` default keeps Overpass comfortable too.

    python -m utils.backfill_sea_view --only-missing
    python -m utils.backfill_sea_view --limit 20 --no-ai

`--no-ai` skips the subscription bridge, so the text signal falls back to the
unambiguous keywords only. The verdict records which path it took.
"""

import argparse
import logging
import time
from collections import Counter

import requests

from app import create_app, db
from models import Property
from services import sea_view_service
from utils.inflight import inflight

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute sea-view verdicts for properties (free sources only)."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit properties processed (0 = all)."
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Skip properties that already carry a computed verdict.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Do not call the subscription bridge; keywords only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report without writing to the database.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Extra pause between properties (seconds).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = create_app()
    with app.app_context():
        query = Property.query.order_by(Property.id.asc())
        properties = query.all()

        if args.only_missing:
            properties = [
                prop
                for prop in properties
                if not isinstance((prop.enrichment or {}).get("environment"), dict)
                or "sea_view_detail"
                not in (prop.enrichment or {}).get("environment", {})
            ]
        if args.limit:
            properties = properties[: args.limit]

        total = len(properties)
        logger.info(
            "Evaluating %s properties (ai=%s, dry_run=%s)",
            total,
            not args.no_ai,
            args.dry_run,
        )

        states = Counter()
        reasons = Counter()
        failures = 0
        session = requests.Session()

        # Free, but hours long: a restart without --only-missing repeats every
        # OpenTopoData and Overpass call at one per second. Only the scoped
        # form is honestly resumable (#283).
        with inflight("backfill_sea_view", resumable=bool(args.only_missing)):
            for index, prop in enumerate(properties, start=1):
                try:
                    verdict = sea_view_service.evaluate_property(
                        prop, use_ai=not args.no_ai, session=session
                    )
                    if not args.dry_run:
                        sea_view_service.apply_to_property(prop, verdict, commit=True)
                    states[verdict["sea_view"]] += 1
                    reasons[verdict["sea_view_detail"].get("reason", "")] += 1
                except Exception:
                    failures += 1
                    logger.error(
                        "Sea-view evaluation failed for %s", prop.id, exc_info=True
                    )
                    # `apply_to_property(commit=True)` requires a session with
                    # nothing pending, so one row's failure must not leave
                    # anything in flight for the next row to trip over.
                    db.session.rollback()

                if index % 25 == 0 or index == total:
                    logger.info("%s/%s processed", index, total)
                if args.sleep:
                    time.sleep(args.sleep)

        logger.info("--- verdicts ---")
        for state in sea_view_service.VALID_STATES:
            logger.info("%-8s %s", state, states.get(state, 0))
        logger.info("failed   %s", failures)
        logger.info("--- reasons ---")
        for reason, count in reasons.most_common():
            logger.info("%-40s %s", reason or "(none)", count)


if __name__ == "__main__":
    main()
