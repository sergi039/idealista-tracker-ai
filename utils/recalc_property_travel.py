"""Recalculate `Property.travel` (and the scores that read it) in bulk.

`utils/recalc_travel_times.py` only knows the legacy `Land`. Universal
properties -- every listing ingested since `INGESTION_TARGET` became
`properties` -- had no bulk path at all, only the per-listing Enrich button.

**What a run bills depends on the deployment, and the estimate says which.**
The presets stopped billing on 2026-08-18 -- OpenStreetMap and the national
hospital register answer them, and a refusal never falls through to the paid
search -- so the one billed leg left is Distance Matrix, ~26 elements a
listing (~$0.13). With `OSRM_URL` set (#416) the local routing engine answers
that leg too, and a run bills nothing at all. The estimate reads
`osrm_routing.is_enabled()`, the same predicate the transport branches on,
rather than restating a price this deployment may not pay. A free run still
overwrites `travel` and every score column, so it still needs the owner's
say-so (CLAUDE.md, "Hard rules").

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

`--clear-orphaned` is the one mode that spends nothing. It drops the travel
block of rows that have **no coordinates** and rescores them: every number in
such a block was measured from a point the row no longer has, and cannot be
re-measured until it gets one. Issue #331 produced four of those deliberately
-- their geocode resolved to the country and was refused -- and their six
preset durations went on rendering from the fabricated origin:

    python -m utils.recalc_property_travel --clear-orphaned \
        --snapshot data/travel_orphaned.json
"""

import argparse
import json
import logging
import time
from typing import Any, Dict, List, Optional

from app import create_app, db
from models import Property
from services import osrm_routing
from services.property_scoring_service import PropertyScoringService
from services.property_travel_service import (
    PropertyTravelService,
)
from utils import score_snapshot
from utils.enrich_scope import log_scope
from utils.google_spend import add_spend_arguments, cli_authorization
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
    return score_snapshot.snapshot_row(prop, json_columns=("travel", "scoring"))


def _write_snapshot(rows: List[Dict[str, Any]], path: str) -> None:
    score_snapshot.write_rows(rows, path)


def _restore(path: str) -> int:
    restored, _missing = score_snapshot.restore_file(path)
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


def orphaned_travel_rows(properties) -> List[Property]:
    """Rows carrying a travel block they no longer have an origin for.

    Every number in `travel` -- six preset durations, the beaches list, the
    travel component of the score -- is measured *from* `location_lat/lon`. A
    row with no coordinates therefore holds measurements from a point it does
    not have, and cannot be re-measured until it gets one.

    Issue #331 produced four of these deliberately: their geocode resolved to
    the country and was refused, which is the honest outcome, but the travel
    measured from the fabricated point stayed behind and still renders.
    """
    return [
        p
        for p in properties
        if (p.location_lat is None or p.location_lon is None)
        and isinstance(p.travel, dict)
        and p.travel
    ]


def _clear_orphaned(snapshot_path: Optional[str], ids: Optional[str]) -> int:
    """Drop those blocks and rescore. Spends nothing -- no Google call at all.

    The rescore is not optional: the travel component feeds `score_total`, so
    clearing the block without it would leave the row ranked on a number whose
    evidence has just been deleted.
    """
    query = Property.query.filter(
        (Property.location_lat.is_(None)) | (Property.location_lon.is_(None))
    )
    if ids:
        wanted = [int(x) for x in ids.split(",") if x.strip()]
        query = query.filter(Property.id.in_(wanted))
    rows = orphaned_travel_rows(query.order_by(Property.id.asc()).all())

    logger.info("%s row(s) hold travel measured from a coordinate they lost", len(rows))
    if not rows:
        return 0

    if snapshot_path:
        _write_snapshot([_snapshot_row(p) for p in rows], snapshot_path)
    else:
        logger.warning("No --snapshot: this clears travel and scores with no way back")

    scoring_service = PropertyScoringService()
    cleared = 0
    with inflight("recalc_property_travel_clear", resumable=True):
        for prop in rows:
            prop.travel = None
            try:
                scoring_service.calculate_for_property(prop, commit=False)
            except Exception:
                logger.exception(
                    "Rescoring property %s failed; left untouched", prop.id
                )
                db.session.rollback()
                continue
            db.session.add(prop)
            db.session.commit()
            cleared += 1
            logger.info("Cleared travel for property %s", prop.id)
    logger.info("Cleared %s row(s)", cleared)
    return cleared


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recalculate travel targets for universal properties. Bills "
            "Distance Matrix (~$0.13 a listing) unless OSRM answers the "
            "routing; the presets are free (OSM and the hospital register)."
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
        "--clear-orphaned",
        action="store_true",
        help=(
            "Spend nothing: clear the travel block of rows that have no "
            "coordinates, and rescore them. Every number in such a block was "
            "measured from a point the row no longer has."
        ),
    )
    parser.add_argument(
        "--report",
        help="Write a per-property before/after report of resolved places here.",
    )
    add_spend_arguments(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report the scope, its subscription composition and the worst-case "
            "billed work, then exit without calling any API. The estimate is "
            "this deployment's own: the Distance Matrix leg (~$0.13 a listing) "
            "when Google routes, no billed call at all when OSRM does."
        ),
    )
    args = parser.parse_args()

    if not args.restore and not args.snapshot and not args.dry_run:
        parser.error("--snapshot is required (or use --restore / --dry-run)")

    app = create_app()
    with app.app_context():
        if args.restore:
            restored = _restore(args.restore)
            logger.info("Restored %s properties from %s", restored, args.restore)
            print(f"restored={restored}")
            return

        if args.clear_orphaned:
            cleared = _clear_orphaned(args.snapshot, args.ids)
            print(f"cleared={cleared}")
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
        # Before the snapshot, before the first billed call: what this run
        # covers and what it is worth. 307 of production's 769 located rows
        # sit in retired subscriptions, and a bare count cannot say so
        # (UNIVERSE-001).
        # The cost note reads the predicate the transport itself branches on
        # (`_distance_matrix_batch` in services/property_travel_service.py):
        # since 2026-08-18 the presets are answered by OSM and the hospital
        # register and bill nothing, and with OSRM routing the durations
        # (#416) there is no billed call left in a run at all. An estimate
        # that ignored the deployment's own routing engine would state a
        # price this run is not going to pay.
        if osrm_routing.is_enabled():
            cost_note = (
                "worst case: no billed call -- the presets are answered by "
                "OpenStreetMap and the hospital register, and OSRM routes "
                "the durations locally (OSRM_URL is set)"
            )
        else:
            cost_note = (
                f"worst case: <={len(properties) * 26} Distance Matrix "
                f"elements, about ${len(properties) * 0.13:,.2f} at ~$0.13 "
                "a listing; the presets bill nothing (OSM and the hospital "
                "register answer them)"
            )
        log_scope(
            logger,
            properties,
            label="property_travel_recalc_queue",
            notes=(
                "every located row unless narrowed by --ids or --limit",
                "profile-agnostic on purpose (#410)",
                cost_note,
            ),
        )
        if args.dry_run:
            logger.info("Dry run. No API was called and nothing was written.")
            print(f"scope={len(properties)}")
            return

        _write_snapshot([_snapshot_row(p) for p in properties], args.snapshot)

        travel_service = PropertyTravelService()
        scoring_service = PropertyScoringService()

        processed = 0
        failed = 0
        not_located = 0
        changed_places: List[Dict[str, Any]] = []

        # Never resumable: the scope is every matching property on each run,
        # with no "already answered" filter, so an interrupted run repeats
        # every lookup it finished -- re-billing Distance Matrix where Google
        # still routes -- and the --report file is only written at the end,
        # so that is lost outright. Narrow a restart with --ids by hand
        # (#283).
        with (
            cli_authorization(
                args.reason,
                actor="utils.recalc_property_travel",
                rows=len(properties),
            ),
            inflight("recalc_property_travel", resumable=False),
        ):
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
                    if prop.location_lat is None or prop.location_lon is None:
                        # Not a failure and not a cost: the run stopped before
                        # any request because geocoding could not place the
                        # listing. Counted apart so a run over such rows does
                        # not read as an outage. Until 2026-08-17 a locality
                        # centroid was counted here too; travel measures from
                        # one now, so this is the row that has no point at all.
                        not_located += 1
                    else:
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
            "not_located": not_located,
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
