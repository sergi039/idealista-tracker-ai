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
a row that gained its plot leaves the scope, and so does one whose page
ANSWERED and stated none.

That second half is a measurement, not a blank, so it is recorded rather
than inferred from a still-NULL column: `enrichment["plot_lookup"]` says
the page was read and stated no plot, and the scope skips it. Without it
the run re-fetched every known no-plot page forever, which is the
resumability claim being false in the direction that costs somebody
else's server (the gate review's finding). It is NOT a zero — `plot_area`
stays NULL, so the criteria verdict still reads `unknown` (#98). A row
named explicitly with `--ids` is re-read whatever the marker says: an
operator naming a row means "ask again".
"""

import argparse
import logging
import time
from collections import Counter

from datetime import datetime, timezone

from app import create_app, db
from models import Property
from services import fotocasa_source
from services.enrichment_write import check_writable, locked_write
from utils.inflight import inflight
from utils.listing_source import source_of

logger = logging.getLogger(__name__)

DEFAULT_SLEEP_S = 30.0
DEFAULT_MAX_REFUSALS = 3

# Where "the page answered and stated no plot" is recorded. A measured
# absence, in the family's own shape: a status somebody can read, never a
# zero in the measurement column.
LOOKUP_KEY = "plot_lookup"
STATED_NONE = "page_states_no_plot"


def _states_no_plot(prop) -> bool:
    block = (prop.enrichment or {}).get(LOOKUP_KEY) if prop.enrichment else None
    return isinstance(block, dict) and block.get("status") == STATED_NONE


def _record_states_no_plot(prop) -> None:
    """Write the measured absence under the row lock (#339's contract)."""
    check_writable(prop, True)
    with locked_write(prop, locked=True, commit=True):
        enrichment = dict(prop.enrichment or {})
        enrichment[LOOKUP_KEY] = {
            "status": STATED_NONE,
            "source": "fotocasa",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        prop.enrichment = enrichment


#: Refusals that are an answer about the row rather than the host saying no.
#: They must not count towards the consecutive-refusal stop, or a handful of
#: withdrawn adverts halts a run that had plenty left to ask. Same shape, and
#: the same reasoning, as `utils/backfill_advertiser._NOT_A_HOST_REFUSAL`.
#:
#: `REFUSAL_NOT_FOTOCASA` is here for the same reason and is not reachable
#: today: the scope selects fotocasa rows, so `source_of()` and
#: `fetch_listing()` would have to disagree about one URL. It costs nothing to
#: be right in advance about a refusal that asks nobody anything.
_NOT_A_HOST_REFUSAL = (
    fotocasa_source.REFUSAL_NOT_A_LISTING,
    fotocasa_source.REFUSAL_NOT_FOTOCASA,
)


def _scope(args):
    """Fotocasa rows whose plot nobody has established, oldest first.

    Two ways a row leaves this scope, and both are what makes an
    interrupted run resumable: it gained a `plot_area`, or its page was
    read and stated none (`enrichment["plot_lookup"]`). Naming a row with
    `--ids` overrides the second — an operator asking for a row means ask
    again, and a portal may have gained the figure since.
    """
    query = Property.query.filter(Property.plot_area.is_(None))
    named = bool(args.ids)
    if named:
        query = query.filter(Property.id.in_(args.ids))
    rows = [
        prop
        for prop in query.order_by(Property.id.asc()).all()
        if source_of(prop) == "fotocasa"
        and prop.id not in set(args.skip_ids or [])
        and (named or not _states_no_plot(prop))
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
                if listing.refusal in _NOT_A_HOST_REFUSAL:
                    # The advert is gone, not the host refusing us: fotocasa
                    # redirects a withdrawn listing to a search page
                    # (services/fotocasa_source.py:481-488). That is an answer
                    # about this row, and counting it as a host refusal stalled
                    # the whole run — three dead adverts in a row stopped it,
                    # nothing was written, and the scope is ordered by id so
                    # the next run met the same three and stopped again. No
                    # forward progress at any number of re-runs (#502 review).
                    #
                    # The sibling already drew this line and said why:
                    # `utils/backfill_advertiser.py` `_NOT_A_HOST_REFUSAL`,
                    # "a run would stop on its third row having asked nobody
                    # anything".
                    consecutive_refusals = 0
                    tally["gone"] += 1
                    logger.info(
                        "property %s: %s — the advert is gone, moving on",
                        prop.id,
                        listing.refusal,
                    )
                    continue
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
                    # The page answered and stated no plot. `plot_area`
                    # stays NULL — a zero there would be a measurement
                    # nobody took (#98) — and the READING is recorded, so
                    # the next run does not re-fetch a page that already
                    # answered.
                    _record_states_no_plot(prop)
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
