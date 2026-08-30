"""Score stored listings against the owner's taste profile (issue #498).

    python -m utils.backfill_taste --profiles 24              # report the scope
    python -m utils.backfill_taste --profiles 24 --apply      # run it

Free of Google by construction — the only transport is the subscription
bridge, and one batch of listings is ONE bridge call (the owner's standing
cost order of 2026-08-30 names Google; the bridge spends subscription
credit, which this tool reports per run).

The scope is explicit: `--profiles`, `--ids` or `--all` — there is no
implicit default, because "the visible subscription" is a moving answer and
a spend tool must not guess (the lesson `curate_on_mini.sh` records about
profile ids). Within the population, only rows that are NOT scored against
the current profile version are in scope — missing and version-stale alike —
so an interrupted run resumes by construction: finished rows leave the
scope. `--force` re-scores current rows too and is therefore NOT resumable;
the inflight marker says which one this run claimed.

Three failed bridge CALLS in a row stop the run (the `backfill_advertiser`
rule — calls, not rows: one failed batch of 8 is one refusal, not eight).
Nothing is written for a refusal; a `superseded` row (scored by someone else
mid-call, or facts changed under the call) is skipped, not overwritten.
"""

import argparse
import logging
from collections import Counter

from sqlalchemy import or_

from app import create_app, db
from models import Property
from services import taste_service
from utils.inflight import inflight

logger = logging.getLogger(__name__)

DEFAULT_MAX_REFUSALS = 3


def _scope(args, current_version):
    query = Property.query
    if args.ids:
        query = query.filter(Property.id.in_(args.ids))
    elif args.profiles:
        query = query.filter(Property.search_profile_id.in_(args.profiles))
    if not args.force:
        query = query.filter(
            or_(
                Property.taste_score.is_(None),
                db.not_(
                    taste_service.scored_current_expression(Property, current_version)
                ),
            )
        )
    rows = query.order_by(Property.id.asc()).all()
    return rows[: args.limit] if args.limit else rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score listings against the owner's taste profile "
        "(subscription bridge; no Google).",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--profiles",
        type=int,
        nargs="+",
        help="Listings of these subscription (search_profile) ids.",
    )
    scope.add_argument("--ids", type=int, nargs="+", help="Only these property ids.")
    scope.add_argument(
        "--all",
        action="store_true",
        help="Every listing in the database. Say it on purpose.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually score. Default reports the scope and exits.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-score rows already scored against the current profile "
        "(NOT resumable: a restart repeats them).",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Stop after this many rows (0 = all)."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=taste_service.DEFAULT_BATCH_SIZE,
        help="Listings per bridge call.",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "openai"],
        default="claude",
        help="Bridge provider (default claude).",
    )
    parser.add_argument(
        "--max-refusals",
        type=int,
        default=DEFAULT_MAX_REFUSALS,
        help="Stop after this many failed bridge calls in a row.",
    )
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch must be at least 1")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    app = create_app()
    with app.app_context():
        profile_data = taste_service.load_current_profile()
        if profile_data is None:
            print(
                "No taste profile. Build one first: "
                "python -m utils.build_taste_profile --apply"
            )
            raise SystemExit(1)
        rows = _scope(args, profile_data["version"])
        n_batches = (len(rows) + args.batch - 1) // args.batch
        print(
            f"profile v{profile_data['version']} "
            f"(built {profile_data['built_at']}, provider {profile_data['provider']})"
        )
        print(
            f"scope: {len(rows)} rows -> {n_batches} bridge calls "
            f"(batch {args.batch}, provider {args.provider})"
        )
        if not rows:
            return
        if not args.apply:
            print("Dry run — nothing scored. Re-run with --apply.")
            return

        tally: Counter = Counter()
        calls_made = 0
        consecutive_refusals = 0
        with inflight(
            "utils.backfill_taste",
            resumable=not args.force,
            argv=[
                "--profiles" if args.profiles else ("--ids" if args.ids else "--all"),
            ],
        ):
            for start in range(0, len(rows), args.batch):
                batch = rows[start : start + args.batch]
                outcome = taste_service.score_batch(
                    batch, profile_data, provider=args.provider, commit=True
                )
                calls_made += 1
                if outcome.get("status") != "ok":
                    consecutive_refusals += 1
                    tally["failed_calls"] += 1
                    logger.warning(
                        "bridge call failed (%d in a row): %s",
                        consecutive_refusals,
                        outcome.get("error"),
                    )
                    if consecutive_refusals >= args.max_refusals:
                        print(
                            f"Stopping: {consecutive_refusals} failed bridge calls "
                            "in a row. Nothing was written for them; the next run "
                            "resumes here."
                        )
                        break
                    continue
                consecutive_refusals = 0
                for pid, row_status in outcome["rows"].items():
                    tally[row_status] += 1
                    logger.info("property %s: %s", pid, row_status)

        print(
            f"\ndone: {tally['scored']} scored, {tally['superseded']} superseded, "
            f"{tally['insufficient_evidence']} without enough evidence to judge, "
            f"{tally['failed_calls']} failed calls, {calls_made} bridge calls total"
        )


if __name__ == "__main__":
    main()
