"""Take back an area that came from an e-mail subject, not from the listing (#645).

The saved-search NAME rides in an idealista alert's subject ("... Rias Bajas
costa - 350k - 150m2!"). Ingestion used to read the subject for the area before
the body, so every listing of that search was stored with the search's filter
as its size, and any non-NULL area was then labelled `area_type = 'built'`.
109 rows read exactly 150 (108 in one subscription; two of them are genuinely
150 m2), and one row elsewhere read 1000 from "Solar plots Asturias 1000m2 up
to 120k". `utils/idealista_extractors.extract_card_area_m2` now reads the card
only; this tool re-reads the rows the old reading damaged.

SCOPE (printed on every run, and nothing else is touched): rows of
`utils.listing_source.IDEALISTA` whose stored `email_subject` is not empty and
contains the stored `area` as an m2 figure. A row with no subject has no
evidence of where its area came from and is never in scope. Per row the
stored `description` (the e-mail body's text) is re-read with the SAME function
ingestion uses:

* `recovered`  the card states one area, different from the stored one:
               `area` is set to it. `area_type` stays as it was: it records
               built/plot, which the card figure does not change.
* `confirmed`  the card states the stored figure itself (a genuine 150):
               the row is left alone.
* `unstated`   the card states no area: `area = NULL`, and `area_type` goes
               from `built` (which only ever meant "an area exists") to
               `unknown`. `plot` stays `plot`: that records what the listing
               is, and `reconcile_area_type` enforces it for land.

Two more answers are reported and never written: `ambiguous` (the description
holds several cards with different areas, so which one is this row's cannot be
told) and `no_description` (nothing to re-read).

It writes only `area` and `area_type`: never `enrichment`, never a score. It
does NOT rescore; the last lines list the changed ids so the scores can be
recomputed separately.

    python -m utils.repair_area_from_subject                       # report only
    python -m utils.repair_area_from_subject --apply --snapshot data/area_645.json
    python -m utils.repair_area_from_subject restore data/area_645.json          # report
    python -m utils.repair_area_from_subject restore data/area_645.json --apply

`--apply` needs `--snapshot` and writes it (fsync, then atomic rename) before
the first row changes. Each row is then re-read under `FOR UPDATE` and written
only if it still holds what the report showed; a row changed since is skipped
and named. `restore` is compare-and-swap: it puts the old values back only where
the row still holds exactly what this repair wrote.

No network. Run it on the mini, after `tools/backfill_status.sh` says idle.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from utils.idealista_extractors import extract_area_figures_m2, extract_card_areas_m2
from utils.inflight import inflight
from utils.listing_source import IDEALISTA, source_of

logger = logging.getLogger(__name__)

RECOVERED = "recovered"
CONFIRMED = "confirmed"
UNSTATED = "unstated"
AMBIGUOUS = "ambiguous"
NO_DESCRIPTION = "no_description"

# The outcomes that change a row.
WRITES = (RECOVERED, UNSTATED)

SCOPE_TEXT = (
    "Scope: idealista rows whose stored area is an m2 figure that appears in "
    "their own stored email_subject (rows with an empty subject are never in "
    "scope). Touches area and area_type only; does not rescore."
)

_AREA_TOLERANCE = 0.005


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _same(a: Any, b: Any) -> bool:
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return x is None and y is None
    return abs(x - y) < _AREA_TOLERANCE


def in_scope(prop: Any) -> bool:
    """Whether the stored area demonstrably came from the row's own subject."""
    if source_of(prop) != IDEALISTA:
        return False
    subject = (prop.email_subject or "").strip()
    area = _num(prop.area)
    if not subject or area is None:
        return False
    return any(
        abs(area - figure) < _AREA_TOLERANCE
        for figure in extract_area_figures_m2(subject)
    )


def diagnose(prop: Any) -> Tuple[str, Optional[float], Optional[str]]:
    """(outcome, new_area, new_area_type) for a row that is in scope."""
    description = (prop.description or "").strip()
    if not description:
        return NO_DESCRIPTION, _num(prop.area), prop.area_type
    areas = extract_card_areas_m2(description)
    if not areas:
        new_type = prop.area_type
        if (prop.area_type or "").strip().lower() == "built":
            new_type = "unknown"
        return UNSTATED, None, new_type
    distinct: List[float] = []
    for value in areas:
        if not any(_same(value, seen) for seen in distinct):
            distinct.append(value)
    if len(distinct) > 1:
        return AMBIGUOUS, _num(prop.area), prop.area_type
    if _same(distinct[0], prop.area):
        return CONFIRMED, _num(prop.area), prop.area_type
    return RECOVERED, distinct[0], prop.area_type


def _plan() -> List[Dict[str, Any]]:
    from models import Property

    plan: List[Dict[str, Any]] = []
    rows = Property.query.filter(
        Property.area.isnot(None), Property.email_subject.isnot(None)
    ).order_by(Property.id)
    for prop in rows:
        if not in_scope(prop):
            continue
        outcome, new_area, new_type = diagnose(prop)
        plan.append(
            {
                "id": prop.id,
                "outcome": outcome,
                "old_area": _num(prop.area),
                "old_area_type": prop.area_type,
                "new_area": new_area,
                "new_area_type": new_type,
            }
        )
    return plan


def _write_snapshot(payload: Dict[str, Any], path: str) -> None:
    """Write, fsync, then rename into place: the way back is never a partial file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)


def _locked(prop_id: int):
    from app import db
    from models import Property

    return (
        db.session.query(Property)
        .filter(Property.id == prop_id)
        .with_for_update()
        .one_or_none()
    )


def repair(apply: bool = False, snapshot_path: str = "") -> Dict[str, Any]:
    from app import db

    plan = _plan()
    counts = {
        outcome: sum(1 for e in plan if e["outcome"] == outcome)
        for outcome in (RECOVERED, CONFIRMED, UNSTATED, AMBIGUOUS, NO_DESCRIPTION)
    }
    summary: Dict[str, Any] = {
        "scope": SCOPE_TEXT,
        "in_scope": len(plan),
        "counts": counts,
        "rows": plan,
        "applied": False,
        "snapshot": None,
        "written": [],
        "skipped_changed": [],
    }
    todo = [e for e in plan if e["outcome"] in WRITES]
    if not apply or not todo:
        return summary
    if not snapshot_path:
        raise ValueError("--apply needs a snapshot path (--snapshot)")

    _write_snapshot(
        {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "reason": "#645: area read from the e-mail subject (saved-search name)",
            "columns": ["area", "area_type"],
            "rows": todo,
        },
        snapshot_path,
    )
    summary["snapshot"] = snapshot_path

    with inflight("repair_area_from_subject", resumable=True):
        for entry in todo:
            prop = _locked(entry["id"])
            if (
                prop is None
                or not _same(prop.area, entry["old_area"])
                or prop.area_type != entry["old_area_type"]
            ):
                db.session.rollback()
                summary["skipped_changed"].append(entry["id"])
                continue
            prop.area = entry["new_area"]
            prop.area_type = entry["new_area_type"]
            db.session.commit()
            summary["written"].append(entry["id"])
    summary["applied"] = True
    return summary


def restore(path: str, apply: bool = False) -> Dict[str, Any]:
    """Compare-and-swap: only a row still holding what the repair wrote goes back."""
    from app import db

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    restored: List[int] = []
    changed_since: List[int] = []
    missing: List[int] = []
    for entry in payload.get("rows") or []:
        prop = _locked(entry["id"])
        if prop is None:
            missing.append(entry["id"])
            continue
        if not _same(prop.area, entry["new_area"]) or (
            prop.area_type != entry["new_area_type"]
        ):
            db.session.rollback()
            changed_since.append(entry["id"])
            continue
        if apply:
            prop.area = entry["old_area"]
            prop.area_type = entry["old_area_type"]
            db.session.commit()
        else:
            db.session.rollback()
        restored.append(entry["id"])
    return {
        "applied": bool(apply),
        "restored": restored,
        "changed_since_repair": changed_since,
        "missing": missing,
    }


def _print_report(outcome: Dict[str, Any]) -> None:
    print(outcome["scope"])
    print(f"in scope: {outcome['in_scope']}  {json.dumps(outcome['counts'])}")
    for row in outcome["rows"]:
        print(
            f"  id={row['id']} {row['outcome']}: area {row['old_area']} -> "
            f"{row['new_area']}, area_type {row['old_area_type']} -> "
            f"{row['new_area_type']}"
        )
    if outcome["applied"]:
        print(f"snapshot: {outcome['snapshot']}")
        print(f"written: {outcome['written']}")
        print(f"skipped, changed since the report: {outcome['skipped_changed']}")
        print(
            "Not rescored. Recompute the scores of these ids separately: "
            f"{outcome['written']}"
        )
    else:
        would = [r["id"] for r in outcome["rows"] if r["outcome"] in WRITES]
        print(f"Report only; {len(would)} row(s) would change. Re-run with --apply.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write; else report")
    parser.add_argument("--snapshot", default="", help="snapshot path (with --apply)")
    sub = parser.add_subparsers(dest="command")
    back = sub.add_parser("restore", help="put a snapshot back (compare-and-swap)")
    back.add_argument("path")
    back.add_argument("--apply", action="store_true", dest="restore_apply")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply and not args.snapshot and args.command != "restore":
        parser.error("--apply needs --snapshot PATH")

    from app import create_app

    app = create_app()
    with app.app_context():
        if args.command == "restore":
            print(json.dumps(restore(args.path, args.restore_apply), indent=2))
            return 0
        _print_report(repair(apply=args.apply, snapshot_path=args.snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
