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
* `unstated`   the card (every block at the row's own price) states no area: `area = NULL`, and `area_type` goes
               from `built` (which only ever meant "an area exists") to
               `unknown`. `plot` stays `plot`: that records what the listing
               is, and `reconcile_area_type` enforces it for land.

Three more answers are reported and never written: `ambiguous` (two blocks at the
row's own price state different areas), `no_description` and `no_card` (no
block at the row's price, or the row has none). The row's card
is found by its price; a block at another price never supplies an area.

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
phase `planned` (the run died) is restored from the write journal
`<snapshot>.journal` only; planned rows missing from it are reported, not
written (see `restore`).

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

from utils.idealista_extractors import (
    extract_area_figures_m2,
    extract_card_price_blocks_m2,
    single_listing_mail,
)
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
    (`_clean_description`): a PREHEADER that repeats the price and a teaser
    (no area), then the greeting and the real card, and possibly further cards.
    So the row's card is found by its PRICE, not by position: every price
    block whose price equals the row's own stored `price` (the new price, for
    a price drop) counts, and no other block ever supplies an area -- card 222
    at another price cannot repair row 111. Among the same-price blocks: one
    distinct area is the answer, none is `unstated`, several different ones
    are `ambiguous`. No block at the row's price (or a row without a price) is
    `no_card`. A description that does not state it is about exactly one
    listing (`single_listing_mail`) is `ambiguous` whatever its blocks say.
    The room counts of same-price blocks are not exposed by the block parser,
    so no guard compares them. A description that does carry listing links is scoped to the
    row's own card by the shared extractor.
    """
    description = (prop.description or "").strip()
    old = (_num(prop.area), prop.area_type)
    if not description:
        return (NO_DESCRIPTION, *old)
    price = _num(prop.price)
    if price is None:
        return (NO_CARD, *old)
    own_id = prop.idealista_property_id if "/inmueble/" in description else None
    blocks = [
        (p, a)
        for p, a in extract_card_price_blocks_m2(
            description, idealista_property_id=own_id
        )
        if p is not None and _same(p, price)
    ]
    if not blocks:
        return (NO_CARD, *old)
    # A price matches the row's card only when the mail holds one listing; in a
    # mail with several, a later card at the same price would pass for it.
    if not single_listing_mail(description):
        return (AMBIGUOUS, *old)
    distinct: List[float] = []
    for _, a in blocks:
        if a is not None and not any(_same(a, d) for d in distinct):
            distinct.append(a)
    if len(distinct) > 1:
        return (AMBIGUOUS, *old)
    if not distinct:
        new_type = prop.area_type
        if (prop.area_type or "").strip().lower() == "built":
            new_type = "unknown"
        return UNSTATED, None, new_type
    if _same(distinct[0], prop.area):
        return (CONFIRMED, *old)
    return RECOVERED, distinct[0], prop.area_type


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


def _journal_path(snapshot_path: str) -> str:
    return snapshot_path + ".journal"


def _journal_append(handle, record: Dict[str, Any]) -> None:
    """One JSON line per committed row, on disk before the next row starts."""
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _read_journal(path: str) -> Tuple[Dict[int, Dict[str, Any]], int]:
    """({id: record}, unreadable_line_count). A torn line is counted, not guessed."""
    records: Dict[int, Dict[str, Any]] = {}
    unreadable = 0
    if not os.path.exists(path):
        return records, unreadable
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                records[int(record["id"])] = record
            except (ValueError, KeyError, TypeError):
                unreadable += 1
    return records, unreadable


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
        if os.path.exists(_journal_path(snapshot_path)):
            raise FileExistsError(
                f"journal {_journal_path(snapshot_path)} already exists; "
                "choose a new snapshot path."
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
    journal = open(_journal_path(snapshot_path), "x", encoding="utf-8")
    with journal, inflight("repair_area_from_subject", resumable=True):
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
            _journal_append(
                journal,
                {
                    "id": entry["id"],
                    "old_area": entry["old_area"],
                    "old_area_type": entry["old_area_type"],
                    "written_area": entry["new_area"],
                    "written_area_type": entry["new_area_type"],
                },
            )
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
    were never touched.

    A snapshot still in phase `planned` (the run died before the final record)
    cannot say which rows were written: planned values alone also match a row
    somebody else corrected to the same figure. So it acts ONLY on rows in the
    write journal (`<snapshot>.journal`, one fsynced line per committed row),
    by compare-and-swap against the journaled written values. A planned row
    absent from the journal is never written by restore; it is reported as
    `not_journaled` with its current values. That is also the rare case of a
    process that died between a row's commit and its journal line: such a row
    may hold the repaired value, and it is left as it is for a person to
    judge. A journal line that cannot be parsed (a torn last line) is ignored
    and counted in `unreadable_journal_lines`, never guessed.
    """
    from app import db

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    rows = payload.get("rows") or []
    unreadable = 0
    not_journaled: List[Dict[str, Any]] = []
    if payload.get("phase") == "planned":
        journal, unreadable = _read_journal(_journal_path(path))
        targets = []
        for entry in rows:
            record = journal.get(entry["id"])
            if record is None:
                prop = _locked(entry["id"])
                not_journaled.append(
                    {
                        "id": entry["id"],
                        "current_area": _num(prop.area) if prop else None,
                        "current_area_type": prop.area_type if prop else None,
                    }
                )
                db.session.rollback()
                continue
            targets.append({**record, "status": "written"})
    else:
        targets = [e for e in rows if e.get("status") == "written"]

    restored: List[int] = []
    changed_since: List[int] = []
    missing: List[int] = []
    for entry in targets:
        prop = _locked(entry["id"])
        if prop is None:
            missing.append(entry["id"])
            continue
        holds_written = (
            _same(prop.area, entry["written_area"])
            and prop.area_type == entry["written_area_type"]
        )
        if not holds_written:
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
        "not_journaled": not_journaled,
        "unreadable_journal_lines": unreadable,
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
