"""How many stored listings a second saved search has delivered. Spends nothing.

Read-only by construction: it opens no external API, writes no column, takes
no lock and ends by rolling its read transaction back. It counts the journal
`services/arrival_journal.py` keeps (#632) and prints who delivered what, so
the owner can see how often a listing really belongs to two subscriptions
before anything is built on that.

    python -m utils.report_arrivals
    python -m utils.report_arrivals --format json

What it cannot see, and says so in its own output: an arrival from before the
journal existed (nothing stored it, and nothing can be backfilled), and a
repeat by the listing's own subscription (never recorded -- that is the same
search, not a second one).

Each entry's subscription is read through today's routes, one hop, exactly as
`SearchProfileService.canonical_profile()` reads it: a subscription that has
since been routed onto the listing's home is no longer a second one, and is
counted as `absorbed` rather than dropped silently.

Not wrapped in `utils/inflight.inflight(...)`: the rule (#283) is for long
jobs whose interruption loses work. This is one pass over the table in
Python, and killing it loses nothing.
"""

import argparse
import json
import sys
from collections import Counter
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from app import create_app, db
from models import Property, SearchProfile
from services import arrival_journal


def collect(
    rows: Iterable[Any], routed_to: Mapping[int, Optional[int]]
) -> Dict[str, Any]:
    """The report as data. Pure: rows and today's routes in, numbers out.

    `rows` need `id`, `search_profile_id` and `enrichment`; `routed_to` maps
    every subscription id to its route target or None.
    """

    def canonical(profile_id: Any) -> Any:
        return routed_to.get(profile_id) or profile_id

    scanned = journaled = truncated = entries = absorbed = 0
    listings = 0
    pair_listings: Counter = Counter()
    pair_entries: Counter = Counter()
    by_source: Counter = Counter()

    for row in rows:
        scanned += 1
        journal = arrival_journal.read(row)
        if not journal["items"] and not journal["truncated"]:
            continue
        journaled += 1
        truncated += 1 if journal["truncated"] else 0
        home = row.search_profile_id
        others = set()
        for item in journal["items"]:
            entries += 1
            arrival = canonical(item.get("profile_id"))
            if arrival == home:
                absorbed += 1
                continue
            others.add(arrival)
            pair_entries[(home, arrival)] += 1
            by_source[item.get("source") or "unknown"] += 1
        if others:
            listings += 1
        for arrival in others:
            pair_listings[(home, arrival)] += 1

    pairs = [
        {
            "home_profile_id": home,
            "arrival_profile_id": arrival,
            "listings": count,
            "entries": pair_entries[(home, arrival)],
        }
        for (home, arrival), count in sorted(
            pair_listings.items(), key=lambda kv: (-kv[1], str(kv[0]))
        )
    ]
    return {
        "rows_scanned": scanned,
        "rows_with_a_journal": journaled,
        "rows_truncated": truncated,
        "entries": entries,
        "entries_absorbed_by_a_later_route": absorbed,
        "listings_delivered_by_a_second_search": listings,
        "by_source": dict(sorted(by_source.items())),
        "pairs": pairs,
    }


def _print_text(report: Dict[str, Any], names: Mapping[int, str]) -> None:
    def name(profile_id: Any) -> str:
        if profile_id is None:
            return "no subscription"
        return f"{profile_id} {names.get(profile_id, '(unknown subscription)')}"

    print(f"Rows scanned:                          {report['rows_scanned']}")
    print(f"Rows carrying a journal:               {report['rows_with_a_journal']}")
    print(
        "Listings a second search delivered:    "
        f"{report['listings_delivered_by_a_second_search']}"
    )
    print(f"Journal entries:                       {report['entries']}")
    print(
        "  of them now the row's own search:    "
        f"{report['entries_absorbed_by_a_later_route']}"
    )
    print(f"Rows whose journal is full (truncated): {report['rows_truncated']}")
    if report["by_source"]:
        print("Entries by portal:")
        for source, count in report["by_source"].items():
            print(f"  {source}: {count}")
    if report["pairs"]:
        print("Home subscription <- also delivered by (listings, entries):")
        for pair in report["pairs"]:
            print(
                f"  {name(pair['home_profile_id'])} <- "
                f"{name(pair['arrival_profile_id'])}: "
                f"{pair['listings']}, {pair['entries']}"
            )
    print(
        "Not counted: arrivals from before the journal existed (#632, nothing "
        "stored them) and repeats by a listing's own subscription."
    )


def load():
    """Rows and routes, in one read transaction."""
    rows = (
        db.session.query(Property.id, Property.search_profile_id, Property.enrichment)
        .order_by(Property.id)
        .yield_per(500)
    )
    profiles = db.session.query(
        SearchProfile.id, SearchProfile.name, SearchProfile.routed_to
    ).all()
    routed_to = {profile.id: profile.routed_to for profile in profiles}
    names = {profile.id: profile.name for profile in profiles}
    return rows, routed_to, names


def main(argv: Optional[Sequence[str]] = None, app: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    app = app or create_app()
    with app.app_context():
        try:
            rows, routed_to, names = load()
            report = collect(rows, routed_to)
        finally:
            # Nothing was written; this only ends the read transaction.
            db.session.rollback()

    if args.format == "json":
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_text(report, names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
