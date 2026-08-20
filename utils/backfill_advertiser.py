"""Establish who is selling, for stored listings, by reading their pages.

Free -- no Google, no AI, no key. It reads the listing page a row already links
to, and only for the rows whose answer is not already derivable from the row
itself: a listing that arrived by alert email carries Idealista's own campaign
token in its URL, so `services/advertiser.py` answers it with no request at all
and this tool skips it.

    python -m utils.backfill_advertiser --profiles 14 16 --dry-run
    python -m utils.backfill_advertiser --profiles 14 16

**What it can actually reach, measured 2026-08-17 from this machine.** Of the
322 rows carrying no campaign token, 56 are fotocasa and the rest are
idealista.com. Idealista answers `403` with a DataDome captcha to every request
from here, so its pages cannot be read and this tool does not pretend to try:
those rows are reported as `site_not_readable` and nothing is written for them.
Fotocasa answers, but not quickly -- after 5 requests spaced 3 s apart it began
serving its "SENTIMOS LA INTERRUPCIÓN" page with a `200` status, and kept doing
so for several minutes afterwards. That is why `--sleep` defaults to 30 seconds
rather than the courtesy 3 the one-off import uses, and why a run that collects
`--max-refusals` consecutive refusals stops instead of walking the rest of the
list into the same wall. Stopping early costs nothing: the scope is defined by
what is still missing, so the next run resumes where this one gave up.

Nothing is written for a refusal. A row nobody could read keeps no block at
all, which is what `read_verdict` presents as `unchecked` -- a stored
"unavailable" would be a second spelling of the same nothing, and the kind of
value a later reader mistakes for a measurement.
"""

import argparse
import logging
import time
from collections import Counter

from app import create_app, db
from models import Property
from services import advertiser
from utils.enrich_scope import log_scope
from utils.inflight import inflight
from utils.listing_source import source_of

logger = logging.getLogger(__name__)

# Refusals in a row before the run gives up. Three, because that is what
# `RefusalBreaker` counts before it opens, and a tool that kept going past the
# breaker would only be queueing requests the breaker then declines.
DEFAULT_MAX_REFUSALS = 3

# Measured pacing, not a published limit. See the module docstring.
DEFAULT_SLEEP_S = 30.0

# The refusals that mean "this row was never going to answer", as opposed to
# "the host said no". They cost no request and must not count towards the
# consecutive-refusal stop, or a run over a subscription of idealista rows
# would stop on its third row having asked nobody anything.
_NOT_A_HOST_REFUSAL = (
    advertiser.REFUSAL_ALREADY_KNOWN,
    advertiser.REFUSAL_HAND_SET,
    advertiser.REFUSAL_NO_URL,
    advertiser.REFUSAL_SITE_NOT_READABLE,
)


def _scope(profile_ids, ids, limit):
    """The rows this run may touch, newest question first.

    `only missing` is not a flag: a row whose seller is already established --
    by a campaign token, by an earlier reading, or by the owner's hand -- is
    never in scope, which is what makes an interrupted run resumable.
    """
    query = Property.query
    if ids:
        query = query.filter(Property.id.in_(ids))
    elif profile_ids:
        query = query.filter(Property.search_profile_id.in_(profile_ids))
    query = query.filter(
        advertiser.state_expression(Property) == advertiser.UNCHECKED
    ).order_by(Property.id.asc())
    rows = query.all()
    return rows[:limit] if limit else rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Establish who advertises stored listings (free; reads listing pages).",
    )
    parser.add_argument(
        "--profiles",
        type=int,
        nargs="+",
        default=[],
        help="Only listings of these subscription (search_profile) ids.",
    )
    parser.add_argument(
        "--ids", type=int, nargs="+", default=[], help="Only these property ids."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Stop after this many rows (0 = all)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the scope and write nothing. Makes no request either.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_S,
        help=f"Pause between listing pages, seconds (default {DEFAULT_SLEEP_S:g}).",
    )
    parser.add_argument(
        "--max-refusals",
        type=int,
        default=DEFAULT_MAX_REFUSALS,
        help="Stop after this many refusals from the host in a row.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    app = create_app()
    with app.app_context():
        rows = _scope(args.profiles, args.ids, args.limit)
        total = len(rows)
        logger.info("%s listings with no established seller in scope", total)
        log_scope(
            logger,
            rows,
            label="advertiser_backfill_queue",
            notes=(
                "rows whose seller nothing else can answer for",
                "free: the listing page itself, paced at 30 s",
            ),
        )

        if args.dry_run:
            sites = Counter()
            for prop in rows:
                sites[source_of(prop)] += 1
            for site, count in sites.most_common():
                logger.info("  %-10s %s", site, count)
            logger.info("dry run: nothing fetched, nothing written")
            return

        states = Counter()
        refusals = Counter()
        # Every distinct publisher type the portal served, so the first private
        # advert anybody here has seen is noticed rather than silently filed
        # under `unknown`: `services/advertiser.py` maps only the spellings it
        # has evidence for, and this line is how the evidence arrives.
        portal_types = Counter()
        consecutive = 0
        stored = 0

        # Resumable in the honest sense: every row commits on its own and the
        # scope above excludes the rows that already answered, so a killed run
        # (#283) re-enters exactly where it stopped.
        with inflight("backfill_advertiser", resumable=True):
            for index, prop in enumerate(rows, start=1):
                try:
                    result = advertiser.enrich(prop, commit=True)
                except Exception:
                    logger.error(
                        "Advertiser lookup failed for %s", prop.id, exc_info=True
                    )
                    # `commit=True` needs a session with nothing pending, so one
                    # row's failure must leave nothing for the next to trip on.
                    db.session.rollback()
                    consecutive += 1
                    refusals["exception"] += 1
                    if consecutive >= args.max_refusals:
                        logger.warning(
                            "stopping after %s failures in a row", consecutive
                        )
                        break
                    continue

                refusal = result.get("refusal")
                if refusal:
                    refusals[refusal] += 1
                    if refusal in _NOT_A_HOST_REFUSAL:
                        continue
                    consecutive += 1
                    if consecutive >= args.max_refusals:
                        logger.warning(
                            "stopping after %s refusals in a row (last: %s); "
                            "the host is refusing, and the rest of the scope is "
                            "waiting for the next run",
                            consecutive,
                            refusal,
                        )
                        break
                else:
                    consecutive = 0
                    states[result["state"]] += 1
                    stored += 1 if result.get("stored") else 0
                    block = (prop.enrichment or {}).get(advertiser.ENRICHMENT_KEY) or {}
                    portal_types[
                        (block.get("evidence") or {}).get("publisher_type")
                    ] += 1

                if index % 10 == 0 or index == total:
                    logger.info("%s/%s processed", index, total)
                # Only rows that really cost a request are paced; the ones
                # refused without one fall out through `continue` above.
                if args.sleep:
                    time.sleep(args.sleep)

        logger.info("--- established ---")
        for state in advertiser.ESTABLISHED_STATES:
            logger.info("%-10s %s", state, states.get(state, 0))
        logger.info("written    %s", stored)
        logger.info("--- not established ---")
        for reason, count in refusals.most_common():
            logger.info("%-22s %s", reason, count)
        logger.info("--- publisher types the portal served ---")
        for value, count in portal_types.most_common():
            logger.info("%-22s %s", value if value is not None else "(none)", count)


if __name__ == "__main__":
    main()
