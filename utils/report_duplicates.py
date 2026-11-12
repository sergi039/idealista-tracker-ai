"""Which listings are publications of the same home, counted. Spends nothing.

Read-only by construction: it opens no external API, writes no column and
takes no lock. It applies `services/property_duplicates.py` (#633) to one
subscription and prints what the rule finds, so the owner can eyeball pairs
before anything is folded on a screen.

    python -m utils.report_duplicates --profile-id 24
    python -m utils.report_duplicates --profile-id 24 --tier probable --sample 30
    python -m utils.report_duplicates --profile-id 24 --format csv

Not wrapped in `utils/inflight.inflight(...)`: the rule (#283) is for long
jobs whose interruption loses work. This is one read of a subscription and
pure Python over it (seconds for ~2.5k rows), and killing it loses nothing.
"""

import argparse
import csv
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app import create_app, db
from models import Property, PropertyActivity
from services import property_duplicates as dup

DEFAULT_SAMPLE = 30


def _signals_text(explanation: Dict[str, Any]) -> str:
    return "; ".join(
        f"{name}={sig['status']}({sig['value']})"
        for name, sig in explanation["signals"].items()
        if sig["status"] != dup.ABSTAINED
    )


def collect(
    rows: Sequence[Any],
    activity_ids: Iterable[int] = (),
    *,
    tier: str = dup.PROBABLE,
    sample: int = DEFAULT_SAMPLE,
    links: Optional[dup.Links] = None,
) -> Dict[str, Any]:
    """The report as data. Pure: rows in, numbers and sample pairs out.

    `links` are the owner's decisions (`dup.load_links`); they are applied the
    way the rule applies them everywhere: `same` first, `different` forbidden.
    """
    if tier not in dup.TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    activity = frozenset(activity_ids)
    links = links if links is not None else dup.Links()
    by_id = {row.id: row for row in rows}
    all_edges = dup.edges(rows)  # once: groups, counts and sample reuse it

    tiers: Dict[str, Dict[str, Any]] = {}
    for name in dup.GROUPABLE_TIERS:
        # Cumulative: `probable` means confirmed and probable together.
        wanted = dup.GROUPABLE_TIERS[: dup.GROUPABLE_TIERS.index(name) + 1]
        refused: List[Any] = []
        found = dup.groups_with_links(
            rows, links, tiers=wanted, found_edges=all_edges, refused=refused
        )
        members = [row for group in found for row in group.members]
        folded = dup.folded_ids(found, activity_ids=activity)
        protected = Counter(
            dup.protection(row, activity)
            for row in members
            if dup.protection(row, activity) is not None
        )
        tiers[name] = {
            "groups": len(found),
            "rows": len(members),
            "would_fold": len(folded),
            "refused_by_veto": len(refused),
            "protected_by_reason": dict(sorted(protected.items())),
        }
    owner = _owner_summary(by_id, links)
    tiers[dup.POSSIBLE] = {"pairs": sum(1 for e in all_edges if e.tier == dup.POSSIBLE)}

    wanted_edges = [e for e in all_edges if e.tier == tier]
    pairs = []
    for edge in wanted_edges[: max(0, sample)]:
        a, b = by_id[edge.a], by_id[edge.b]
        pairs.append(
            {
                "a": a.id,
                "b": b.id,
                "tier": edge.tier,
                "url_a": a.url,
                "url_b": b.url,
                "explain": edge.explain(),
            }
        )
    return {
        "rows": len(rows),
        "edges": dict(Counter(e.tier for e in all_edges)),
        "tiers": tiers,
        "owner_links": owner,
        "sample_tier": tier,
        "sample_total": len(wanted_edges),
        "sample": pairs,
    }


def _owner_summary(by_id: Dict[int, Any], links: dup.Links) -> Dict[str, int]:
    """How many of the owner's decisions touch these rows, and how many held.

    A decision is read only when both ends are among the rows. `same` is
    counted as applied when its ends end up in one group (by the strongest
    reading, confirmed + probable); one refused by an owner `different` is
    counted separately.
    """
    inside = {p for p in links.same_pairs if p[0] in by_id and p[1] in by_id}
    different = {p for p in links.different_pairs if p[0] in by_id and p[1] in by_id}
    refused: List[Any] = []
    joined = dup.groups_with_links(
        list(by_id.values()),
        dup.Links(frozenset(inside), frozenset(different)),
        found_edges=[],
        refused=refused,
    )
    together = {i: g.ids for g in joined for i in g.ids}
    held = sum(1 for a, b in inside if together.get(a) and b in together[a])
    return {
        "same_applied": held,
        "same_refused": len(inside) - held,
        "different_applied": len(different),
    }


def _print_text(report: Dict[str, Any]) -> None:
    print(f"Rows read: {report['rows']}")
    for name in dup.GROUPABLE_TIERS:
        t = report["tiers"][name]
        label = "confirmed" if name == dup.CONFIRMED else "confirmed + probable"
        print(
            f"{label}: {t['groups']} groups, {t['rows']} rows, "
            f"{t['would_fold']} would fold"
        )
        if t["refused_by_veto"]:
            print(
                f"    edges refused (would join a vetoed pair): {t['refused_by_veto']}"
            )
        for reason, count in t["protected_by_reason"].items():
            print(f"    protected ({reason}): {count}")
    owner = report["owner_links"]
    print(
        f"owner decisions applied: {owner['same_applied']} same, "
        f"{owner['different_applied']} different"
        + (
            f"; {owner['same_refused']} same refused by a different"
            if owner["same_refused"]
            else ""
        )
    )
    print(f"possible (never grouped): {report['tiers'][dup.POSSIBLE]['pairs']} pairs")
    shown = len(report["sample"])
    print(
        f"\nSample of {report['sample_tier']} pairs ({shown} of {report['sample_total']}):"
    )
    for pair in report["sample"]:
        print(f"  {pair['a']} <-> {pair['b']}")
        print(f"    {pair['url_a']}")
        print(f"    {pair['url_b']}")
        print(f"    {_signals_text(pair['explain'])}")


def _print_csv(report: Dict[str, Any]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["tier", "id_a", "id_b", "url_a", "url_b", "evidence"])
    for pair in report["sample"]:
        writer.writerow(
            [
                pair["tier"],
                pair["a"],
                pair["b"],
                pair["url_a"],
                pair["url_b"],
                _signals_text(pair["explain"]),
            ]
        )


def load(profile_id: int):
    """The subscription's rows and the ids with a live timeline entry."""
    rows: List[Property] = (
        Property.query.filter(Property.search_profile_id == profile_id)
        .order_by(Property.id)
        .all()
    )
    activity = {
        pid
        for (pid,) in db.session.query(PropertyActivity.property_id)
        .filter(PropertyActivity.deleted_at.is_(None))
        .distinct()
    }
    return rows, activity


def main(argv: Optional[Sequence[str]] = None, app: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-id", type=int, required=True)
    parser.add_argument("--tier", choices=dup.TIERS, default=dup.PROBABLE)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--format", choices=("text", "csv"), default="text")
    args = parser.parse_args(argv)

    app = app or create_app()
    with app.app_context():
        rows, activity = load(args.profile_id)
        links = dup.load_links(row.id for row in rows)
        report = collect(
            rows, activity, tier=args.tier, sample=args.sample, links=links
        )
        db.session.rollback()  # nothing was written; leave no open transaction

    (_print_csv if args.format == "csv" else _print_text)(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
