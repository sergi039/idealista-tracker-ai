"""Fill `plot_area` for stored portal rows whose source can state it.

    python -m utils.backfill_plot_area              # report the scope
    python -m utils.backfill_plot_area --apply      # fetch and write

Free — no Google, no AI. Only fotocasa rows are in scope: their listing
pages answer the honest UA and their payload carries `surfaceLand` /
`groundSurface` (the parser now keeps it; rows imported before this feature
dropped it on the floor). Idealista refuses this machine (DataDome),
yaencontre refuses both machines, and milanuncios digests carry trackers a
bulk tool must not knock on — those rows stay honestly `unknown`, which is
exactly what the criteria verdict renders for them (#98).

Paced at 30 s (the `backfill_advertiser` measurement: fotocasa starts
serving its block page after 5 requests spaced 3 s), stops after three host
refusals in a row, per-row commit, and `resumable=True` is a true claim:
a row that gained its plot (or measurably has none) leaves the scope.
"""

import argparse
import logging
import time
from collections import Counter

from app import create_app, db
from models import Property
from services import fotocasa_source
from utils.inflight import inflight
from utils.listing_source import source_of

logger = logging.getLogger(__name__)

DEFAULT_SLEEP_S = 30.0
DEFAULT_MAX_REFUSALS = 3


def _scope(args):
    """Fotocasa rows with no plot on record, oldest first.

    `plot_area IS NULL` is what makes an interrupted run resumable — a
    scored row leaves the scope. A page that answers but states no plot
    writes a zero-marker? No: it writes nothing, and the row stays in
    scope; `--skip-ids` exists for the handful a re-run should not keep
    re-fetching.
    """
    query = Property.query.filter(Property.plot_area.is_(None))
    if args.ids:
        query = query.filter(Property.id.in_(args.ids))
    rows = [
        prop
        for prop in query.order_by(Property.id.asc()).all()
        if source_of(prop) == "fotocasa" and prop.id not in set(args.skip_ids or [])
    ]
    return rows[: args.limit] if args.limit else rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill plot_area from fotocasa listing pages (free, paced).",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Fetch and write. Default reports."
    )
    parser.add_argument("--ids", type=int, nargs="+", default=[], help="Only these.")
    parser.add_argument(
        "--skip-ids", type=int, nargs="+", default=[], help="Leave these alone."
    )
    parser.add_argument("--limit", type=int, default=0, help="Stop after N rows.")
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP_S, help="Pause between pages."
    )
    parser.add_argument(
        "--max-refusals",
        type=int,
        default=DEFAULT_MAX_REFUSALS,
        help="Stop after this many host refusals in a row.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = create_app()
    with app.app_context():
        rows = _scope(args)
        print(f"scope: {len(rows)} fotocasa rows without a plot on record")
        if not rows or not args.apply:
            if rows:
                print("Dry run — nothing fetched. Re-run with --apply.")
            return

        tally: Counter = Counter()
        consecutive_refusals = 0
        with inflight("utils.backfill_plot_area", resumable=True):
            for index, prop in enumerate(rows):
                if index:
                    time.sleep(args.sleep)
                listing = fotocasa_source.fetch_listing(prop.url)
                if listing.refusal:
                    consecutive_refusals += 1
                    tally["refused"] += 1
                    logger.warning(
                        "property %s: %s (%d refusals in a row)",
                        prop.id,
                        listing.refusal,
                        consecutive_refusals,
                    )
                    if consecutive_refusals >= args.max_refusals:
                        print(
                            f"Stopping: {consecutive_refusals} refusals in a row. "
                            "Nothing was written for them; the next run resumes."
                        )
                        break
                    continue
                consecutive_refusals = 0
                if listing.plot_area is None:
                    # The page answered and stated no plot — nothing is
                    # written (#98: the absence stays an absence, and a
                    # marker value would read as a measurement).
                    tally["page_states_no_plot"] += 1
                    logger.info("property %s: page states no plot", prop.id)
                    continue
                prop.plot_area = listing.plot_area
                db.session.commit()
                tally["filled"] += 1
                logger.info("property %s: plot %s m2", prop.id, listing.plot_area)

        print(
            f"\ndone: {tally['filled']} filled, "
            f"{tally['page_states_no_plot']} pages state no plot, "
            f"{tally['refused']} refusals"
        )


if __name__ == "__main__":
    main()
