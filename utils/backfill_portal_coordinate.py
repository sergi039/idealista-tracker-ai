"""Establish, by reading the page, that a stored coordinate is the portal's pin.

Free -- no Google, no AI, no key. It re-reads the fotocasa listing page a row
already links to and records the coordinate the portal publishes, so
`services/coordinate_quality.portal_coordinate` has something to read and a
re-geocode can no longer replace a listing-specific pin with a district
centroid (#393).

    python -m utils.backfill_portal_coordinate --dry-run
    python -m utils.backfill_portal_coordinate

**Why this exists at all.** #393 shipped the guard, and the guard reads
provenance the importer writes. The 56 fotocasa rows already in this table came
from an out-of-band script that wrote no such block, so they are unprotected.
Their coordinates *are* portal pins by that script's own docstring -- but the
row does not say so, and writing that inference into a provenance field would
be STATUS-002 (#265) in a new column: a claim about the world with no evidence
behind it. The page is the evidence, and it is free to ask.

**This tool never moves a row.** It writes provenance and nothing else. Where
the page agrees with what is stored, the row gains a pin it can defend; where
it does not, nothing is written and the disagreement is reported. That split is
the whole design:

* **agreed** -- the page's coordinate matches the stored one, so the stored one
  demonstrably is the portal's pin. Recorded.
* **differs** -- the page says somewhere else. Two explanations fit and the row
  cannot tell them apart: the stored coordinate was geocoded rather than copied
  from the portal, or the portal has since moved its pin. Recording the page's
  point would make the next refresh *move* the row to a coordinate it has never
  held, which is the defect this whole issue is about, pointed the other way.
  So: report, write nothing, leave it to a human.
* **no coordinate on the page** -- the portal published none. Nothing to
  record.
* **unreadable** -- removed listing, block, parse failure. Nothing is written;
  a row nobody could read keeps no block, which is what leaves it in scope for
  the next run.

Pacing follows `utils/backfill_advertiser.py`, and for the same measured
reason: after 5 requests spaced 3 s apart fotocasa began serving its "SENTIMOS
LA INTERRUPCIÓN" page with a `200` status and kept doing so for minutes. So
`--sleep` defaults to 30 s, and a run that collects `--max-refusals` refusals in
a row stops rather than walking the rest into the same wall. Stopping costs
nothing: the scope is "rows with no pin recorded", so the next run resumes.
"""

import argparse
import logging
import math
import time
from collections import Counter

from app import create_app, db
from models import Property
from services.coordinate_quality import portal_coordinate, record_portal_coordinate
from services.fotocasa_source import SOURCE_NAME, fetch_listing
from utils.inflight import inflight
from utils.listing_source import FOTOCASA, source_of

logger = logging.getLogger(__name__)

DEFAULT_MAX_REFUSALS = 3

# Measured pacing, not a published limit. See the module docstring.
DEFAULT_SLEEP_S = 30.0

# How far apart two coordinates may be and still be the same pin. The stored
# column is Numeric(10, 7) and the portal publishes about as many decimals, so
# an exact copy round-trips exactly; this absorbs the last digit rather than
# any real difference. One metre is roughly 9e-6 degrees of latitude.
MATCH_TOLERANCE_M = 1.0

OUTCOME_RECORDED = "recorded"
OUTCOME_DIFFERS = "differs"
OUTCOME_NO_COORDINATE = "no_coordinate_on_page"
OUTCOME_UNREADABLE = "unreadable"
OUTCOME_NO_STORED_COORDINATE = "row_has_no_coordinate"


def _metres_apart(lat1, lon1, lat2, lon2) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _scope(profile_ids, ids, limit):
    """Fotocasa rows with no pin recorded, oldest first.

    "Only missing" is not a flag, for the reason `backfill_advertiser` gives:
    a row that already carries a pin is never in scope, which is what makes an
    interrupted run resumable without one.

    The fotocasa filter is `utils/listing_source.source_of`, not an `ilike`, so
    this agrees with the badge and the filter on the page rather than being a
    fifth reading of the same URL.
    """
    query = Property.query
    if ids:
        query = query.filter(Property.id.in_(ids))
    elif profile_ids:
        query = query.filter(Property.search_profile_id.in_(profile_ids))

    rows = [
        row
        for row in query.order_by(Property.id.asc()).all()
        if source_of(row) == FOTOCASA and portal_coordinate(row) is None
    ]
    return rows[:limit] if limit else rows


def process(prop, *, session=None, apply: bool = True) -> dict:
    """Read one listing page and decide what, if anything, to record."""
    listing = fetch_listing(prop.url, session=session)

    if not listing.ok:
        return {"outcome": OUTCOME_UNREADABLE, "reason": listing.refusal}

    if listing.latitude is None or listing.longitude is None:
        return {"outcome": OUTCOME_NO_COORDINATE}

    if prop.location_lat is None or prop.location_lon is None:
        # Nothing to corroborate. The page's pin may well be right, but this
        # tool's job is to establish that the coordinate the row *has* is the
        # portal's -- filling an empty one is a different decision, and one
        # that moves the row.
        return {
            "outcome": OUTCOME_NO_STORED_COORDINATE,
            "page_lat": listing.latitude,
            "page_lon": listing.longitude,
        }

    apart = _metres_apart(
        prop.location_lat, prop.location_lon, listing.latitude, listing.longitude
    )
    if apart > MATCH_TOLERANCE_M:
        return {
            "outcome": OUTCOME_DIFFERS,
            "metres_apart": round(apart, 1),
            "stored": f"{float(prop.location_lat):.7f},{float(prop.location_lon):.7f}",
            "page": f"{listing.latitude:.7f},{listing.longitude:.7f}",
        }

    if apply:
        # The stored value, not the page's: they agree to within a metre, and
        # the row's own number is what every measurement on it was taken from.
        prop.enrichment = record_portal_coordinate(
            prop.enrichment,
            source=SOURCE_NAME,
            lat=prop.location_lat,
            lon=prop.location_lon,
        )
        db.session.commit()

    return {"outcome": OUTCOME_RECORDED, "metres_apart": round(apart, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", nargs="*", type=int, default=None)
    parser.add_argument("--ids", nargs="*", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the pages and report; write nothing.",
    )
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S)
    parser.add_argument("--max-refusals", type=int, default=DEFAULT_MAX_REFUSALS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    app = create_app()
    with app.app_context():
        rows = _scope(args.profiles, args.ids, args.limit)
        logger.info("Scope: %s fotocasa row(s) with no pin recorded", len(rows))
        if not rows:
            return

        counts = Counter()
        consecutive = 0
        differed = []

        # `resumable=True` is a claim and it is true here: the scope is what is
        # still missing, and each row commits on its own.
        with inflight("backfill_portal_coordinate", resumable=True):
            for index, prop in enumerate(rows):
                try:
                    result = process(prop, apply=not args.dry_run)
                except Exception:
                    db.session.rollback()
                    logger.exception("Property %s failed", prop.id)
                    counts[OUTCOME_UNREADABLE] += 1
                    consecutive += 1
                    if consecutive >= args.max_refusals:
                        logger.warning(
                            "stopping after %s failures in a row", consecutive
                        )
                        break
                    continue

                outcome = result["outcome"]
                counts[outcome] += 1
                logger.info("  %s: %s %s", prop.id, outcome, result)

                if outcome == OUTCOME_DIFFERS:
                    differed.append({"id": prop.id, **result})

                if outcome == OUTCOME_UNREADABLE:
                    consecutive += 1
                    if consecutive >= args.max_refusals:
                        logger.warning(
                            "stopping after %s refusals in a row: %s",
                            consecutive,
                            result.get("reason"),
                        )
                        break
                else:
                    consecutive = 0

                if args.sleep and index < len(rows) - 1:
                    time.sleep(args.sleep)

        logger.info("Done: %s", dict(counts))
        if differed:
            # Named individually, because each one is a question for a human
            # and a count would hide which rows to look at.
            logger.warning(
                "%s row(s) disagree with their page and were left alone:", len(differed)
            )
            for row in differed:
                logger.warning(
                    "  property %s: stored %s, page %s, %s m apart",
                    row["id"],
                    row["stored"],
                    row["page"],
                    row["metres_apart"],
                )
        if args.dry_run:
            logger.info("Dry run. Nothing was written.")


if __name__ == "__main__":
    main()
