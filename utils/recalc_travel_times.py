import argparse
import logging
import time

from app import create_app
from models import Land
from services.travel_time_service import TravelTimeService

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Recalculate travel times for lands (batch-optimized).")
    parser.add_argument("--sleep", type=float, default=0.1, help="Sleep between properties (seconds).")
    parser.add_argument("--only-missing", action="store_true", help="Only recalc lands with missing travel times.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of lands processed (0 = all).")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        q = Land.query.filter(Land.listing_status != "removed")
        if args.only_missing:
            q = q.filter(
                (Land.travel_time_oviedo.is_(None))
                | (Land.travel_time_gijon.is_(None))
                | (Land.travel_time_nearest_beach.is_(None))
            )
        q = q.order_by(Land.id.asc())
        if args.limit:
            q = q.limit(args.limit)

        lands = q.all()
        total = len(lands)
        service = TravelTimeService()
        ok = 0
        fail = 0

        for idx, land in enumerate(lands, start=1):
            if not land.location_lat or not land.location_lon:
                continue
            try:
                if service.calculate_travel_times(land.id):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                fail += 1
                logger.warning("Failed to recalc for land %s: %s", land.id, e)

            if idx % 10 == 0:
                logger.info("Progress %s/%s ok=%s fail=%s", idx, total, ok, fail)

            if args.sleep:
                time.sleep(max(0.0, float(args.sleep)))

        logger.info("Done. total=%s ok=%s fail=%s", total, ok, fail)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

