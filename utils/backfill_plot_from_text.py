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
snippet, extractor version, portal, UTC time, and the run's id) and nothing
else, under `FOR UPDATE`, one commit per row. `apply` needs `--snapshot` with
a path that does not exist; phase 1 (atomic, fsynced) lists every planned row
with what it held BEFORE (`plot_area` as text, the earlier `plot_from_text`
block) before the first write; a locked row that no longer holds that is
skipped, so the snapshot's "before" is true of every row written. Each commit
is journaled, phase 2 replaces the file with what was written. `restore` is
compare-and-swap and puts back the "before": a row is undone only when it
still carries this run's id, the written value and the extractor version; a
changed row is reported and left alone. From a phase-1 file it walks every
planned row, not only the journal, so a commit whose journal line never
reached the disk is undone too (review of #661).

No network, no Catastro. Free. Run it on the mini, after
`tools/backfill_status.sh` says idle, and announce it (module, rows, cost 0).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
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
    if source_of(prop) != IDEALISTA:
        # An idealista title may be the saved-search subject (#645): not evidence.
        sources.append(("title", prop.title))
    return sources, skipped


def _before(prop: Any) -> Dict[str, Any]:
    """What `apply` may change, exactly enough to put it back (0 stays 0)."""
    block = (prop.enrichment or {}).get(plot_text.ENRICHMENT_KEY)
    return {
        "plot_area": None if prop.plot_area is None else str(prop.plot_area),
        "plot_from_text": block,
    }


def _in_scope(prop: Any) -> bool:
    if effective_figures(prop)["plot_m2"] is not None:
        return False
    return prop.plot_area is None or plot_text._blank(prop.plot_area)


def plan_row(prop: Any) -> Dict[str, Any]:
    """What the text says about one row (no write)."""
    sources, skipped = _sources(prop)
    decided = plot_text.read_sources(sources)
    if decided is not None:
        field, reading = decided
        return {
            "id": prop.id,
            "portal": source_of(prop),
            "outcome": "gain",
            "value": reading.value,
            "field": field,
            "snippet": reading.snippet,
            "before": _before(prop),
        }
    if any(plot_text.mentions_plot(text) for _, text in sources):
        outcome = "ambiguous"
    else:
        outcome = "multi_listing_skipped" if skipped else "no_reading"
    return {
        "id": prop.id,
        "portal": source_of(prop),
        "outcome": outcome,
        "has_text": any(bool(text) for _, text in sources),
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
    """The row under `FOR UPDATE`, read fresh: a compare-and-swap must not
    compare against what the session cached before the lock (review of #675)."""
    from app import db
    from models import Property

    return (
        db.session.query(Property)
        .filter(Property.id == prop_id)
        .with_for_update()
        .populate_existing()
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

    run_id = uuid.uuid4().hex

    def _snap(phase: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
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
            if prop is None or not _in_scope(prop) or _before(prop) != entry["before"]:
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
            block = {**block, "run": run_id}
            prop.enrichment = {**prop.enrichment, plot_text.ENRICHMENT_KEY: block}
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
    """Compare-and-swap back to the snapshot's "before"; a changed row is left alone.

    A phase-1 file walks every planned row: a row is this run's only if it
    carries the run id, so a commit whose journal line was lost is still found,
    and a planned row that was never written simply does not match.
    """
    from app import db
    from sqlalchemy.orm.attributes import flag_modified

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    run_id = payload.get("run_id")
    if not run_id:
        raise ValueError(f"{path} carries no run_id; it is not an apply snapshot.")
    rows = payload.get("rows", [])
    journal: Dict[int, Dict[str, Any]] = {}
    unreadable = 0
    if payload.get("phase") == "planned":
        journal, unreadable = _read_journal(_journal_path(path))
        targets = rows
    else:
        targets = [e for e in rows if e.get("status") == "written"]
    restored: List[int] = []
    changed_since: List[int] = []
    not_written: List[int] = []
    for entry in targets:
        written = float(entry["value"])
        prop = _locked(entry["id"])
        block = (
            ((prop.enrichment or {}).get(plot_text.ENRICHMENT_KEY)) if prop else None
        )
        holds = (
            prop is not None
            and isinstance(block, dict)
            and block.get("run") == run_id
            and block.get("extractor_version") == payload.get("extractor_version")
            and prop.plot_area is not None
            and float(prop.plot_area) == written
            and float(block.get("value", -1)) == written
        )
        if not holds:
            db.session.rollback()
            planned_only = (
                payload.get("phase") == "planned" and entry["id"] not in journal
            )
            (not_written if planned_only else changed_since).append(entry["id"])
            continue
        if apply_it:
            before = entry["before"]
            enrichment = dict(prop.enrichment)
            if before["plot_from_text"] is None:
                enrichment.pop(plot_text.ENRICHMENT_KEY, None)
            else:
                enrichment[plot_text.ENRICHMENT_KEY] = before["plot_from_text"]
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
            prop.plot_area = (
                None if before["plot_area"] is None else Decimal(before["plot_area"])
            )
            db.session.commit()
        else:
            db.session.rollback()
        restored.append(entry["id"])
    return {
        "applied": bool(apply_it),
        "restored": restored,
        "changed_since_apply": changed_since,
        "planned_not_written_or_changed": not_written,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # The bare command is the report, so it takes the report's filter too.
    parser.add_argument("--profile-id", type=int, default=None)
    sub = parser.add_subparsers(dest="command")
    for name in ("report", "apply"):
        p = sub.add_parser(name)
        # SUPPRESS: a subcommand without the flag keeps the one given before it.
        p.add_argument("--profile-id", type=int, default=argparse.SUPPRESS)
        if name == "apply":
            p.add_argument("--snapshot", required=True)
    back = sub.add_parser("restore")
    back.add_argument("path")
    back.add_argument("--apply", action="store_true", dest="restore_apply")
    return parser


def main(argv=None) -> int:
    return _run(build_parser().parse_args(argv))


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from app import create_app

    app = create_app()
    with app.app_context():
        if args.command == "restore":
            print(json.dumps(restore(args.path, args.restore_apply), indent=2))
        elif args.command == "apply":
            _print_report(apply(args.snapshot, args.profile_id))
        else:
            _print_report(report(args.profile_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
