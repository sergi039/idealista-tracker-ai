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

Three more answers are reported and never written: `ambiguous` (the first card
states an area and a later card in the same description states a different
one), `no_description` and `no_card` (no price block to read). The row's card
is the FIRST price block; a later block never supplies its area.

It writes only `area` and `area_type`: never `enrichment`, never a score. It
does NOT rescore; the last lines list the changed ids so the scores can be
recomputed separately.

    python -m utils.repair_area_from_subject                       # report only
    python -m utils.repair_area_from_subject --apply --snapshot data/area_645.json
    python -m utils.repair_area_from_subject restore data/area_645.json          # report
    python -m utils.repair_area_from_subject restore data/area_645.json --apply

`--apply` needs `--snapshot`, which must not exist yet (an earlier run's restore
data is never overwritten). Phase 1 writes every candidate, marked `planned`
(fsync, atomic rename) before the first row changes. Each row is then
re-evaluated in full (scope and outcome) under `FOR UPDATE` and written only if
it is still what the report showed; otherwise it is skipped and named. Phase 2
replaces the file with the final record: each row `written` (old and written
values) or `skipped` (with the reason). `restore` acts on `written` rows only,
by compare-and-swap against the values this repair wrote; a snapshot still in
phase `planned` (the run died) is restored the same way against the planned
values, row by row.

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

from utils.idealista_extractors import extract_area_figures_m2, extract_card_blocks_m2
from utils.inflight import inflight
from utils.listing_source import IDEALISTA, source_of

logger = logging.getLogger(__name__)

RECOVERED = "recovered"
CONFIRMED = "confirmed"
UNSTATED = "unstated"
AMBIGUOUS = "ambiguous"
NO_DESCRIPTION = "no_description"
NO_CARD = "no_card"

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
    """(outcome, new_area, new_area_type) for a row that is in scope.

    The stored description is the whole e-mail's text with the anchors cut out
    (`_clean_description`), so it can hold several cards and has no links to
    tell them apart. The row is the FIRST listing of its e-mail (ingestion
    emits only the first link), so its card is the FIRST price block: that
    block's area, or none -- never a later block's. Later blocks can only make
    the answer `ambiguous`, never supply it. A description that does carry
    listing links is scoped to the row's own card by the shared extractor.
    """
    description = (prop.description or "").strip()
    old = (_num(prop.area), prop.area_type)
    if not description:
        return (NO_DESCRIPTION, *old)
    own_id = prop.idealista_property_id if "/inmueble/" in description else None
    blocks = extract_card_blocks_m2(description, idealista_property_id=own_id)
    if not blocks:
        return (NO_CARD, *old)
    first = blocks[0]
    if first is None:
        new_type = prop.area_type
        if (prop.area_type or "").strip().lower() == "built":
            new_type = "unknown"
        return UNSTATED, None, new_type
    if any(b is not None and not _same(b, first) for b in blocks[1:]):
        return (AMBIGUOUS, *old)
    if _same(first, prop.area):
        return (CONFIRMED, *old)
    return RECOVERED, first, prop.area_type


def _entry(prop: Any) -> Optional[Dict[str, Any]]:
    """The planned change for a row, or None when it is not in scope."""
    if not in_scope(prop):
        return None
    outcome, new_area, new_type = diagnose(prop)
    return {
        "id": prop.id,
        "outcome": outcome,
        "old_area": _num(prop.area),
        "old_area_type": prop.area_type,
        "new_area": new_area,
        "new_area_type": new_type,
    }


def _plan() -> List[Dict[str, Any]]:
    from models import Property

    rows = Property.query.filter(
        Property.area.isnot(None), Property.email_subject.isnot(None)
    ).order_by(Property.id)
    return [e for e in (_entry(prop) for prop in rows) if e is not None]


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


def _snapshot(phase: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "reason": "#645: area read from the e-mail subject (saved-search name)",
        "columns": ["area", "area_type"],
        "phase": phase,
        "rows": rows,
    }


def repair(apply: bool = False, snapshot_path: str = "") -> Dict[str, Any]:
    from app import db

    if apply:
        if not snapshot_path:
            raise ValueError("--apply needs a snapshot path (--snapshot)")
        if os.path.exists(snapshot_path):
            raise FileExistsError(
                f"snapshot {snapshot_path} already exists; it holds an earlier "
                "run's restore data. Choose a new path."
            )

    plan = _plan()
    counts = {
        outcome: sum(1 for e in plan if e["outcome"] == outcome)
        for outcome in (
            RECOVERED,
            CONFIRMED,
            UNSTATED,
            AMBIGUOUS,
            NO_DESCRIPTION,
            NO_CARD,
        )
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

    # Phase 1: every candidate, before the first write.
    _write_snapshot(
        _snapshot("planned", [{**e, "status": "planned"} for e in todo]),
        snapshot_path,
    )
    summary["snapshot"] = snapshot_path

    final: List[Dict[str, Any]] = []
    with inflight("repair_area_from_subject", resumable=True):
        for entry in todo:
            prop = _locked(entry["id"])
            # The whole scope is re-evaluated on the locked row: the subject,
            # the area, the description, not only the two columns written.
            now = _entry(prop) if prop is not None else None
            if (
                now is None
                or now["outcome"] != entry["outcome"]
                or not _same(now["old_area"], entry["old_area"])
                or now["old_area_type"] != entry["old_area_type"]
                or not _same(now["new_area"], entry["new_area"])
                or now["new_area_type"] != entry["new_area_type"]
            ):
                db.session.rollback()
                summary["skipped_changed"].append(entry["id"])
                final.append(
                    {
                        **entry,
                        "status": "skipped",
                        "reason": "changed since the report",
                    }
                )
                continue
            prop.area = entry["new_area"]
            prop.area_type = entry["new_area_type"]
            db.session.commit()
            summary["written"].append(entry["id"])
            final.append(
                {
                    **entry,
                    "status": "written",
                    "written_area": entry["new_area"],
                    "written_area_type": entry["new_area_type"],
                }
            )

    # Phase 2: the record of what was actually written (replaces phase 1).
    _write_snapshot(_snapshot("final", final), snapshot_path)
    summary["applied"] = True
    return summary


def restore(path: str, apply: bool = False) -> Dict[str, Any]:
    """Compare-and-swap, row by row, against what THIS repair wrote.

    A `final` snapshot is acted on for its `written` rows only; `skipped` rows
    were never touched. A snapshot still in phase `planned` (the run died
    mid-way) is read against the planned new values: a row holding them goes
    back, a row still holding its old values was never written, and anything
    else was changed by someone else and is left alone.
    """
    from app import db

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    restored: List[int] = []
    changed_since: List[int] = []
    never_written: List[int] = []
    missing: List[int] = []
    for entry in payload.get("rows") or []:
        if entry.get("status") not in ("written", "planned"):
            continue
        prop = _locked(entry["id"])
        if prop is None:
            missing.append(entry["id"])
            continue
        wrote_area = entry.get("written_area", entry["new_area"])
        wrote_type = entry.get("written_area_type", entry["new_area_type"])
        holds_new = _same(prop.area, wrote_area) and prop.area_type == wrote_type
        holds_old = (
            _same(prop.area, entry["old_area"])
            and prop.area_type == entry["old_area_type"]
        )
        if not holds_new:
            db.session.rollback()
            if entry["status"] == "planned" and holds_old:
                never_written.append(entry["id"])
            else:
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
        "never_written": never_written,
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
    if args.apply and args.command != "restore" and os.path.exists(args.snapshot):
        parser.error(
            f"snapshot {args.snapshot} already exists and holds an earlier run's "
            "restore data; choose a new path. Nothing was written."
        )

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
