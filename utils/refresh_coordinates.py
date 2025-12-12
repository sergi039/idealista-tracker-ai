import argparse
import logging
import time
from typing import Optional, Tuple

from app import create_app, db
from models import Land
from services.enrichment_service import EnrichmentService

logger = logging.getLogger(__name__)


def _as_float_pair(land: Land) -> Tuple[Optional[float], Optional[float]]:
    try:
        lat = float(land.location_lat) if land.location_lat is not None else None
    except Exception:
        lat = None
    try:
        lon = float(land.location_lon) if land.location_lon is not None else None
    except Exception:
        lon = None
    return lat, lon


def _delta_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # Fast approximation: 1 deg lat ~= 111_320m, lon scaled by cos(lat)
    import math

    dlat = (lat2 - lat1) * 111_320.0
    dlon = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return (dlat * dlat + dlon * dlon) ** 0.5


def refresh_all(
    sleep_s: float = 0.25,
    limit: Optional[int] = None,
    dry_run: bool = False,
    min_move_m: float = 150.0,
) -> None:
    service = EnrichmentService()

    q = Land.query.filter(Land.listing_status != "removed").order_by(Land.id.asc())
    if limit:
        q = q.limit(limit)
    lands = q.all()

    total = len(lands)
    checked = 0
    updated = 0
    upgraded = 0
    skipped = 0
    failed = 0

    for land in lands:
        checked += 1
        old_lat, old_lon = _as_float_pair(land)
        old_acc = (land.location_accuracy or "unknown").lower()

        needs = (old_lat is None or old_lon is None) or old_acc != "precise"
        if not needs:
            skipped += 1
            continue

        try:
            coords = service._geocode_with_accuracy(land)
            if not coords:
                failed += 1
                continue

            new_lat = float(coords["lat"])
            new_lon = float(coords["lng"])
            new_acc = (coords.get("accuracy") or "unknown").lower()

            move_ok = True
            if old_lat is not None and old_lon is not None:
                move_m = _delta_m(old_lat, old_lon, new_lat, new_lon)
                move_ok = move_m >= min_move_m or (old_acc != "precise" and new_acc == "precise")

            better = old_acc != "precise" and new_acc == "precise"
            should_apply = (old_lat is None or old_lon is None) or better or move_ok

            if should_apply:
                if not dry_run:
                    land.location_lat = new_lat
                    land.location_lon = new_lon
                    land.location_accuracy = new_acc
                    db.session.add(land)

                updated += 1
                if better:
                    upgraded += 1
            else:
                skipped += 1

        except Exception as e:
            failed += 1
            logger.warning("Failed to refresh coords for land %s: %s", land.id, e)

        if not dry_run and checked % 10 == 0:
            db.session.commit()
            logger.info(
                "Progress: %s/%s checked, %s updated (%s upgraded), %s failed",
                checked,
                total,
                updated,
                upgraded,
                failed,
            )

        if sleep_s:
            time.sleep(sleep_s)

    if not dry_run:
        db.session.commit()

    logger.info(
        "Done. checked=%s total=%s updated=%s upgraded=%s skipped=%s failed=%s dry_run=%s",
        checked,
        total,
        updated,
        upgraded,
        skipped,
        failed,
        dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh land coordinates using improved geocoding from title.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of lands processed (0 = all).")
    parser.add_argument("--sleep", type=float, default=0.25, help="Sleep between geocoding calls (seconds).")
    parser.add_argument("--dry-run", action="store_true", help="Do not write changes to DB.")
    parser.add_argument("--min-move-m", type=float, default=150.0, help="Minimum movement (meters) to accept update.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        refresh_all(
            sleep_s=max(0.0, float(args.sleep)),
            limit=(args.limit or None),
            dry_run=bool(args.dry_run),
            min_move_m=float(args.min_move_m),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
