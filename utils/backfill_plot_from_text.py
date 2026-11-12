"""Fill `plot_area` from the listing text already stored, with its source (#634).

    python -m utils.backfill_plot_from_text [--profile-id N]                  # report
    python -m utils.backfill_plot_from_text apply --snapshot data/plot_634.json [--profile-id N]
    python -m utils.backfill_plot_from_text restore data/plot_634.json [--apply]

`report` (the default) is a dry run and doubles as the AFTER measurement: per
portal it counts the rows that would gain a plot, and for every subscription
with criteria (or the one `--profile-id`) it prints the share of rows passing
the price and the house requirement whose plot is still `unknown`, before and
after, judged with `subscription_criteria.read_verdict`.

SCOPE: rows whose plot is not stated (`services.plot_text` decides: a credible
`plot_area`, or bare land whose `area` is the parcel, is never touched). The
text is the stored `description`, then the `title`. An idealista description
is the whole alert e-mail, which can hold several listings, so it is read only
when `single_listing_mail` says it is about exactly one (else counted as
`multi_listing_skipped`). Yaencontre rows carry no description; fetching its
pages is forbidden (docs/rules/portal-network-safety.md), so they stay
unknown and the report says how many.

It writes `plot_area` and `enrichment["plot_from_text"]` (value, field,
snippet, extractor version, portal, UTC time) and nothing else, under
`FOR UPDATE`, one commit per row. `apply` needs `--snapshot` with a path that
does not exist; phase 1 (atomic, fsynced) lists every planned row before the
first write, each commit is journaled, phase 2 replaces the file with what was
written. `restore` is compare-and-swap: a row is put back only when it still
holds exactly the written value AND the same extractor version; a changed row
is reported and left alone.

No network, no Catastro. Free. Run it on the mini, after
`tools/backfill_status.sh` says idle, and announce it (module, rows, cost 0).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from services import plot_text
from services.subscription_criteria import (
    effective_figures,
    read_criteria,
    read_verdict,
)
from utils.idealista_extractors import single_listing_mail
from utils.inflight import inflight
from utils.listing_source import IDEALISTA, source_of
from utils.repair_area_from_subject import (
    _journal_append,
    _journal_path,
    _read_journal,
    _write_snapshot,
)

logger = logging.getLogger(__name__)

SNAPSHOT_COLUMNS = ["plot_area", "enrichment.plot_from_text"]


def _sources(prop: Any) -> Tuple[List[Tuple[str, Optional[str]]], bool]:
    """(text sources to read, idealista description skipped as multi-listing)."""
    skipped = False
    sources: List[Tuple[str, Optional[str]]] = []
    description = prop.description
    if (
        source_of(prop) == IDEALISTA
        and description
        and not single_listing_mail(description)
    ):
        skipped = True
        description = None
    sources.append(("description", description))
    sources.append(("title", prop.title))
    return sources, skipped


def _in_scope(prop: Any) -> bool:
    if effective_figures(prop)["plot_m2"] is not None:
        return False
    return prop.plot_area is None or plot_text._blank(prop.plot_area)


def plan_row(prop: Any) -> Dict[str, Any]:
    """What the text says about one row (no write)."""
    sources, skipped = _sources(prop)
    mentions = False
    for field, text in sources:
        reading = plot_text.extract_plot_m2(text)
        if reading is not None:
            return {
                "id": prop.id,
                "portal": source_of(prop),
                "outcome": "gain",
                "value": reading.value,
                "field": field,
                "snippet": reading.snippet,
            }
        mentions = mentions or bool(text)
    return {
        "id": prop.id,
        "portal": source_of(prop),
        "outcome": "multi_listing_skipped" if skipped else "no_reading",
        "has_text": mentions,
    }


def _proxy(prop: Any, plot: Optional[float]) -> SimpleNamespace:
    return SimpleNamespace(
        currency=prop.currency,
        price=prop.price,
        area=prop.area,
        area_type=prop.area_type,
        property_category=prop.property_category,
        property_subtype=prop.property_subtype,
        plot_area=plot if plot is not None else prop.plot_area,
    )


def _measure(profiles, gains: Dict[int, float]) -> List[Dict[str, Any]]:
    """Unknown-plot share among rows passing price and house, before and after."""
    from models import Property

    out = []
    for profile in profiles:
        criteria = read_criteria(profile)
        if not criteria or "min_plot_m2" not in criteria:
            continue
        promising = before = after = 0
        for prop in Property.query.filter(Property.search_profile_id == profile.id):
            checks = read_verdict(prop, criteria).get("checks", {})
            if checks.get("price") is not True or checks.get("house") is not True:
                continue
            promising += 1
            if checks.get("plot") is None:
                before += 1
                gained = gains.get(prop.id)
                post = read_verdict(_proxy(prop, gained), criteria)["checks"]
                if post.get("plot") is None:
                    after += 1
        out.append(
            {
                "profile_id": profile.id,
                "name": profile.name,
                "promising": promising,
                "plot_unknown_before": before,
                "plot_unknown_after": after,
            }
        )
    return out


def _profiles(profile_id: Optional[int]):
    from models import SearchProfile

    query = SearchProfile.query
    if profile_id is not None:
        query = query.filter(SearchProfile.id == profile_id)
    return query.all()


def _candidates(profile_id: Optional[int]):
    from models import Property

    query = Property.query.order_by(Property.id)
    if profile_id is not None:
        query = query.filter(Property.search_profile_id == profile_id)
    return [p for p in query if _in_scope(p)]


def report(profile_id: Optional[int] = None) -> Dict[str, Any]:
    plans = [plan_row(p) for p in _candidates(profile_id)]
    per_portal: Dict[str, Counter] = {}
    for plan in plans:
        per_portal.setdefault(plan["portal"], Counter())[plan["outcome"]] += 1
    gains = {p["id"]: p["value"] for p in plans if p["outcome"] == "gain"}
    return {
        "in_scope": len(plans),
        "per_portal": {k: dict(v) for k, v in sorted(per_portal.items())},
        "plans": [p for p in plans if p["outcome"] == "gain"],
        "measure": _measure(_profiles(profile_id), gains),
    }


def _locked(prop_id: int):
    from app import db
    from models import Property

    return (
        db.session.query(Property)
        .filter(Property.id == prop_id)
        .with_for_update()
        .one_or_none()
    )


def apply(snapshot_path: str, profile_id: Optional[int] = None) -> Dict[str, Any]:
    from app import db

    if not snapshot_path:
        raise ValueError("apply needs a snapshot path (--snapshot)")
    for path in (snapshot_path, _journal_path(snapshot_path)):
        if os.path.exists(path):
            raise FileExistsError(f"{path} already exists; choose a new path.")

    summary = report(profile_id)
    todo = summary["plans"]
    summary.update(applied=False, written=[], skipped_changed=[])
    if not todo:
        return summary

    def _snap(phase: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "reason": "#634: plot read from listing text",
            "columns": SNAPSHOT_COLUMNS,
            "extractor_version": plot_text.EXTRACTOR_VERSION,
            "phase": phase,
            "rows": rows,
        }

    _write_snapshot(
        _snap("planned", [{**e, "status": "planned"} for e in todo]), snapshot_path
    )
    final: List[Dict[str, Any]] = []
    journal = open(_journal_path(snapshot_path), "x", encoding="utf-8")
    with (
        journal,
        inflight("utils.backfill_plot_from_text", resumable=True, argv=sys.argv),
    ):
        for entry in todo:
            prop = _locked(entry["id"])
            # The scope and the reading are re-decided on the locked row.
            if prop is None or not _in_scope(prop):
                db.session.rollback()
                summary["skipped_changed"].append(entry["id"])
                final.append({**entry, "status": "skipped", "reason": "changed"})
                continue
            sources, _ = _sources(prop)
            block = plot_text.apply_to_property(prop, sources, portal=source_of(prop))
            if block is None or block["value"] != entry["value"]:
                db.session.rollback()
                summary["skipped_changed"].append(entry["id"])
                final.append({**entry, "status": "skipped", "reason": "changed"})
                continue
            db.session.commit()
            _journal_append(
                journal,
                {
                    "id": entry["id"],
                    "written_value": block["value"],
                    "extractor_version": block["extractor_version"],
                },
            )
            summary["written"].append(entry["id"])
            final.append(
                {**entry, "status": "written", "written_value": block["value"]}
            )
    _write_snapshot(_snap("final", final), snapshot_path)
    summary["applied"] = True
    return summary


def restore(path: str, apply_it: bool = False) -> Dict[str, Any]:
    """Compare-and-swap: only a row still holding what was written is undone."""
    from app import db
    from sqlalchemy.orm.attributes import flag_modified

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("phase") == "planned":
        journal, unreadable = _read_journal(_journal_path(path))
        targets = [
            {"id": i, "written_value": r["written_value"]} for i, r in journal.items()
        ]
    else:
        unreadable = 0
        targets = [e for e in payload.get("rows", []) if e.get("status") == "written"]
    restored: List[int] = []
    changed_since: List[int] = []
    for entry in targets:
        prop = _locked(entry["id"])
        block = (
            ((prop.enrichment or {}).get(plot_text.ENRICHMENT_KEY)) if prop else None
        )
        holds = (
            prop is not None
            and isinstance(block, dict)
            and block.get("extractor_version")
            == payload.get("extractor_version", plot_text.EXTRACTOR_VERSION)
            and prop.plot_area is not None
            and float(prop.plot_area) == float(entry["written_value"])
            and float(block.get("value", -1)) == float(entry["written_value"])
        )
        if not holds:
            db.session.rollback()
            changed_since.append(entry["id"])
            continue
        if apply_it:
            enrichment = dict(prop.enrichment)
            enrichment.pop(plot_text.ENRICHMENT_KEY, None)
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
            prop.plot_area = None
            db.session.commit()
        else:
            db.session.rollback()
        restored.append(entry["id"])
    return {
        "applied": bool(apply_it),
        "restored": restored,
        "changed_since_apply": changed_since,
        "unreadable_journal_lines": unreadable,
    }


def _print_report(out: Dict[str, Any]) -> None:
    print(f"in scope (plot not stated): {out['in_scope']}")
    for portal, counts in out["per_portal"].items():
        print(f"  {portal}: {json.dumps(counts)}")
    print(
        "yaencontre rows carry no description and their pages may not be "
        "fetched: they stay unknown (docs/rules/portal-network-safety.md)."
    )
    for m in out["measure"]:
        before, after, total = (
            m["plot_unknown_before"],
            m["plot_unknown_after"],
            m["promising"],
        )

        def share(n: int) -> str:
            return f"{100 * n / total:.1f}%" if total else "n/a"

        print(
            f"profile {m['profile_id']} {m['name']!r}: {total} rows pass price+house; "
            f"plot unknown {before} ({share(before)}) -> {after} ({share(after)})"
        )
    if out.get("applied"):
        print(f"written: {out['written']}")
        print(f"skipped, changed since the report: {out['skipped_changed']}")
    else:
        print(f"Report only; {len(out['plans'])} row(s) would gain a plot.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    for name in ("report", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--profile-id", type=int, default=None)
        if name == "apply":
            p.add_argument("--snapshot", required=True)
    back = sub.add_parser("restore")
    back.add_argument("path")
    back.add_argument("--apply", action="store_true", dest="restore_apply")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from app import create_app

    app = create_app()
    with app.app_context():
        if args.command == "restore":
            print(json.dumps(restore(args.path, args.restore_apply), indent=2))
        elif args.command == "apply":
            _print_report(apply(args.snapshot, args.profile_id))
        else:
            _print_report(report(getattr(args, "profile_id", None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
