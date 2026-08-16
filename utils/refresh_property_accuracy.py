"""Re-geocode the rows whose `location_accuracy` was a guess, not an answer.

Issue #321. Properties migrated from the legacy `Land` model carry a label that
`services/enrichment_service.py` decided from the *shape of the query string*
before calling anything -- "precise" when the title had two or more
comma-separated parts -- and then kept in place of Google's real
`location_type`. Measured on production 2026-08-15: 141 of the 182 rows
labelled `precise` were labelled that way, and 81 confident sea-view verdicts
rested on them, because `services/sea_view_service.py:580` runs the elevation
profile only for `precise` rows.

**This tool spends money.** It is a bulk backfill against Google's paid
Geocoding API, roughly one call per row (two when the first query finds
nothing), and this repository requires an explicit ticket for that: issue #321
is it. Nothing here runs on a schedule.

The scope is rows the legacy path labelled: `enrichment["legacy_land"]` present
and no `enrichment["geocoding"]` record, the latter being written only by
`services/property_location_service.py`. A finished row gains that record, so it
leaves the scope -- which is what makes a restarted run resume rather than
repeat, and what lets the in-flight marker claim `resumable=True` honestly.

The coordinate moves with the label, deliberately: `location_type` describes the
point Google returned, so storing it against a point from a different query
would be a new version of the same defect. A run therefore rewrites lat/lon,
accuracy and the enrichment record, and writes a snapshot of exactly those
first, because rolling the application back does not roll a data rewrite back:

    python -m utils.refresh_property_accuracy --snapshot data/accuracy_321.json
    python -m utils.refresh_property_accuracy --restore data/accuracy_321.json

`--ids` re-geocodes a named set instead of the default scope, with every other
guarantee unchanged. That is what repairs the rows of issue #331 -- the eight
that sat on Spain's own centroid because their query degraded to ", Spain" --
which the default scope does not select, since they are not legacy-labelled and
already carry a geocoding record:

    python -m utils.refresh_property_accuracy --ids 115,116,117 \
        --snapshot data/accuracy_331.json

Sea-view and sea-distance verdicts are NOT recomputed here; the coordinates
they were derived from have changed, so re-run their own backfills afterwards.
"""

import argparse
import json
import logging
import os
import time
from collections import Counter
from typing import Any, Dict, List

from app import create_app, db
from models import Property
from services.property_location_service import PropertyLocationService
from utils.inflight import inflight

logger = logging.getLogger(__name__)

DEFAULT_SLEEP_S = 0.2


def _is_legacy_labelled(prop: Property) -> bool:
    """Did this row's accuracy come from the legacy guess?

    `enrichment["geocoding"]` is written only by PropertyLocationService, which
    stores Google's own verdict. Its absence alongside a `legacy_land` blob is
    what identifies a label the migration copied off a Land row.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    return "legacy_land" in enrichment and not isinstance(
        enrichment.get("geocoding"), dict
    )


def _parse_ids(raw: str) -> List[int]:
    """Property ids from a comma-separated string, in the order given.

    Anything that is not an integer is refused outright rather than skipped: a
    silently dropped id would make a paid run report success over a smaller set
    than the caller asked for.
    """
    ids: List[int] = []
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            raise SystemExit(f"--ids: {chunk!r} is not a property id")
    if not ids:
        raise SystemExit("--ids was given but names no property")
    return ids


def _was_refused(prop: Property) -> bool:
    """Did the geocoder refuse deliberately, rather than merely fail?

    `PropertyLocationService` writes `refused` into the geocoding record when
    every candidate query resolved to something coarser than a locality -- a
    country, a region (#331). That is an *outcome*: the row is meant to end
    with no coordinates and a record saying why. A transient failure -- the
    geocoder unreachable, an exception, an empty answer -- writes nothing, and
    there the previous coordinates must survive untouched.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    record = enrichment.get("geocoding")
    return isinstance(record, dict) and bool(record.get("refused"))


def _persist_outcome(prop: Property, ok: bool) -> str:
    """Commit or roll back one row, and name what happened to it.

    The refusal branch exists because of a measured defect: the first #331
    repair run rolled back on every `ok is False`, which discarded both the
    nulled coordinates and the refusal record, so four of eight rows kept the
    fabricated coordinate the run was there to remove. The log said
    "4 could not be geocoded" and the rows said 40.463667,-3.749220.
    """
    if ok:
        db.session.add(prop)
        db.session.commit()
        return (prop.location_accuracy or "unknown").lower()

    if _was_refused(prop):
        db.session.add(prop)
        db.session.commit()
        return "refused"

    db.session.rollback()
    return "failed"


def _snapshot_row(prop: Property) -> Dict[str, Any]:
    return {
        "id": prop.id,
        "location_lat": str(prop.location_lat)
        if prop.location_lat is not None
        else None,
        "location_lon": str(prop.location_lon)
        if prop.location_lon is not None
        else None,
        "location_accuracy": prop.location_accuracy,
        "enrichment": prop.enrichment,
    }


def _write_snapshot(rows: List[Dict[str, Any]], path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    logger.info("Snapshot of %d rows written to %s", len(rows), path)


def _restore(path: str) -> int:
    with open(path, "r", encoding="utf-8") as handle:
        rows = json.load(handle)
    restored = 0
    for row in rows:
        prop = db.session.get(Property, row["id"])
        if prop is None:
            logger.warning("Property %s is gone; not restored", row["id"])
            continue
        prop.location_lat = row["location_lat"]
        prop.location_lon = row["location_lon"]
        prop.location_accuracy = row["location_accuracy"]
        prop.enrichment = row["enrichment"]
        db.session.add(prop)
        db.session.commit()
        restored += 1
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace guessed location_accuracy labels with Google's answer (#321)."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Rows to process (0 = all)."
    )
    parser.add_argument(
        "--ids",
        help=(
            "Comma-separated property ids to re-geocode instead of the default "
            "scope. Every other guarantee -- snapshot, per-row commit, in-flight "
            "marker -- applies unchanged."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Call the geocoder and report, without writing. Costs the same as a real run.",
    )
    parser.add_argument(
        "--snapshot", help="Write a rollback snapshot to this path first."
    )
    parser.add_argument(
        "--restore", help="Restore a snapshot and exit. Spends nothing."
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_S,
        help="Pause between rows (seconds).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = create_app()
    with app.app_context():
        if args.restore:
            count = _restore(args.restore)
            logger.info("Restored %d rows from %s", count, args.restore)
            return

        if args.ids:
            wanted = _parse_ids(args.ids)
            rows = [db.session.get(Property, pid) for pid in wanted]
            missing = [pid for pid, row in zip(wanted, rows) if row is None]
            rows = [row for row in rows if row is not None]
            if missing:
                # Naming them matters: a typo'd id must not read as a row that
                # was processed and found nothing to change.
                logger.warning(
                    "No such property: %s", ", ".join(str(m) for m in missing)
                )
            logger.info("Re-geocoding %d row(s) named by --ids", len(rows))
        else:
            rows = [
                p for p in db.session.query(Property).all() if _is_legacy_labelled(p)
            ]

        if args.limit:
            rows = rows[: args.limit]

        before = Counter((p.location_accuracy or "unknown").lower() for p in rows)
        logger.info(
            "%d rows in scope (%s)",
            len(rows),
            ", ".join(f"{k}={v}" for k, v in sorted(before.items())),
        )
        if not rows:
            return

        if args.snapshot and not args.dry_run:
            _write_snapshot([_snapshot_row(p) for p in rows], args.snapshot)
        elif not args.dry_run:
            logger.warning(
                "No --snapshot given: this run rewrites coordinates with no way back."
            )

        service = PropertyLocationService()
        after = Counter()
        moved = 0
        failed = 0
        refused = 0

        # Resumable: each row commits on its own and leaves the scope by gaining
        # an enrichment["geocoding"] record, so a killed run resumes where it
        # stopped instead of paying for the same rows twice.
        with inflight("refresh_property_accuracy", resumable=not args.dry_run):
            for index, prop in enumerate(rows, start=1):
                old = (prop.location_accuracy or "unknown").lower()
                old_lat, old_lon = prop.location_lat, prop.location_lon

                if args.dry_run:
                    # ensure_coordinates writes to the instance; roll it back so
                    # a dry run really is one.
                    ok = service.ensure_coordinates(prop, refresh=True)
                    new = (
                        (prop.location_accuracy or "unknown").lower()
                        if ok
                        else "(failed)"
                    )
                    db.session.rollback()
                else:
                    ok = service.ensure_coordinates(prop, refresh=True)
                    new = _persist_outcome(prop, ok)

                if new == "refused":
                    # Counted apart from a failure on purpose: the row changed,
                    # it lost a coordinate it should never have had, and the
                    # summary must not report that as nothing having happened.
                    refused += 1
                elif not ok:
                    failed += 1
                elif (old_lat, old_lon) != (prop.location_lat, prop.location_lon):
                    moved += 1
                after[new] += 1

                logger.info(
                    "[%d/%d] id=%s %s -> %s", index, len(rows), prop.id, old, new
                )
                if args.sleep:
                    time.sleep(args.sleep)

        logger.info(
            "Done. %d rows, %d moved, %d refused (coordinates removed), "
            "%d could not be geocoded. Labels now: %s",
            len(rows),
            moved,
            refused,
            failed,
            ", ".join(f"{k}={v}" for k, v in sorted(after.items())),
        )
        logger.info(
            "Coordinates changed, so sea view and sea distance are now stale for "
            "the moved rows; re-run their backfills."
        )


if __name__ == "__main__":
    main()
