"""Re-file the plots a portal called `Residential` (the #503 defect, backwards).

`services/fotocasa_source.py` decided plot-vs-built from `buildingSubtype`
alone until #503, and fotocasa tags a building plot `Residential` -- its word
for *residential land*, which the advert then sells as somewhere to "construir
la casa de tus sueños". The path, which says `/comprar/terreno/`, was never
read. #503 reads it, and #503 is forward-only: the rows already in the table
still say what the old parser wrote.

Measured on production 2026-08-31, after #503 was deployed: **10 rows** on a
`/comprar/terreno/` URL disagree with what the shipped parser would now say --
7 filed `property_category='housing'`, and 3 already `land` but still holding
`area_type='built'` because `build_property` skipped the reconciliation.
Property 1336 is a 21,472 m² field stored as a house at EUR 54,000; 1333 is a
16,782 m² one. Both pass the owner's "at least 150 m² of house" filter and both
sit at `score_total` 100.00.

**The scores go with them, and that is not scope creep.**
`PropertyScoringService.scorer_for()` picks the scorer *by*
`property_category`, so those 100.00s were produced by the housing scorer on a
row that is land. Correcting the classification and leaving the number is a
judgement about a listing that no longer exists -- so the repair rescores the
same rows in the same transaction, and the snapshot carries the score columns
and the `scoring` payload so the restore puts both halves back together. The
rescore is free: `calculate_for_property` reads what is already stored and
makes no request of anything.

**The condition is the shipped parser, not a hand-written guess.** A row is in
scope when `fotocasa_source.url_says_plot()` -- the same function #503 added
and the parser itself calls -- says the portal's own path names land, and the
row does not already say `land` / `plot`. Nothing keys on the title, on
`Residential`, or on a list of ids: a rule narrower than the defect is the
lesson of `utils/repair_import_status_source.py`, and a rule written twice is
how two of them come to disagree.

Reports and exits unless `--apply` is given. Writes a snapshot of exactly what
it is about to overwrite first (`--no-backup` is a thing you say out loud), and
`restore` is compare-and-swap: it touches only the rows its snapshot names and
refuses one that has been edited since, because a row somebody corrected by
hand afterwards must not be quietly put back.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from app import create_app, db
from models import Property
from services.fotocasa_import import classify_row
from services.fotocasa_source import url_says_plot
from services.property_classification_service import PropertyClassificationService
from services.property_scoring_service import PropertyScoringService
from utils import score_snapshot

logger = logging.getLogger(__name__)

SNAPSHOT_COLUMNS = ("property_category", "property_subtype", "area_type")


def _rows_to_repair() -> List[Property]:
    """Every row whose portal path says land and whose columns do not.

    The URL test runs in Python rather than in SQL because `url_says_plot` is
    the parser's own reading -- host, then the path's type segment, in every
    language fotocasa serves it. A LIKE that approximated it here would be the
    second copy of that rule.
    """
    candidates = (
        db.session.query(Property)
        .filter(Property.url.isnot(None))
        .order_by(Property.id)
    )
    return [
        prop
        for prop in candidates
        if url_says_plot(prop.url)
        and (
            (prop.property_category or "").strip().lower() != "land"
            or (prop.area_type or "").strip().lower() != "plot"
        )
    ]


def _classification_now(prop: Property):
    """What the shipped classifier says about this row today.

    Asked rather than assumed: `classify_row` is the path both portal doors
    take, it applies the subscription's own rules, and it is where #503 put
    the portal's type word. Writing "land" here instead would be a second
    copy of that decision, and a subscription whose rules say otherwise would
    disagree with its own ingest.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    imported = enrichment.get("import") if isinstance(enrichment, dict) else {}
    building_type = (imported or {}).get("building_type")

    return classify_row(
        {
            "title": prop.title,
            "building_type": building_type,
            "description": prop.description,
            "url": prop.url,
        },
        prop.search_profile_id,
    )


def _repair_row(prop: Property) -> Dict[str, Any]:
    """Correct one row in place and report what moved."""
    before = {
        "property_category": prop.property_category,
        "property_subtype": prop.property_subtype,
        "area_type": prop.area_type,
        "score_total": score_snapshot.decimal_str(prop.score_total),
    }

    category, subtype = _classification_now(prop)
    if (category or "").strip().lower() != "land":
        # The classifier does not agree that this is land, so nothing is
        # written: a repair that overrules the code it exists to catch up
        # with is not a repair. Reported so the row is visible rather than
        # silently skipped.
        return {"id": prop.id, "before": before, "after": None, "skipped": category}

    prop.property_category = category
    prop.property_subtype = subtype or "plot"
    PropertyClassificationService.reconcile_area_type(prop)
    PropertyScoringService().calculate_for_property(prop)

    return {
        "id": prop.id,
        "before": before,
        "after": {
            "property_category": prop.property_category,
            "property_subtype": prop.property_subtype,
            "area_type": prop.area_type,
            "score_total": score_snapshot.decimal_str(prop.score_total),
        },
    }


def repair(
    apply: bool = False, snapshot_path: str = "", backup: bool = True
) -> Dict[str, Any]:
    rows = _rows_to_repair()
    outcome: Dict[str, Any] = {
        "found": len(rows),
        "repaired": 0,
        "skipped": 0,
        "applied": bool(apply),
        "snapshot": None,
        "rows": [],
    }
    if not rows:
        return outcome

    if apply and backup:
        path = snapshot_path or os.path.join(
            "data",
            "portal_plot_classification_snapshot_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".json",
        )
        score_snapshot.write(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                # `score_snapshot.load` names this list `scores`; writing it
                # under any other key produces a file this repository's own
                # loader refuses, which is a snapshot that is not a way back.
                "scores": [
                    score_snapshot.snapshot_row(
                        prop, classification_columns=SNAPSHOT_COLUMNS
                    )
                    for prop in rows
                ],
            },
            path,
        )
        outcome["snapshot"] = path

    for prop in rows:
        result = _repair_row(prop)
        outcome["rows"].append(result)
        if result.get("after") is None:
            outcome["skipped"] += 1
        else:
            outcome["repaired"] += 1

    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return outcome


def restore(path: str, apply: bool = False) -> Dict[str, Any]:
    """Put a snapshot back. The way out, and it is tested."""
    # `load` has already parsed and validated every row -- re-parsing its
    # output would hand `_parse_score` the Decimal it just produced.
    parsed = score_snapshot.load(path).rows

    restored = 0
    missing: List[int] = []
    for row in parsed:
        prop = db.session.get(Property, row["id"])
        if prop is None:
            missing.append(row["id"])
            continue
        if not score_snapshot.differs(prop, row):
            continue
        score_snapshot.apply_row(prop, row)
        restored += 1

    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return {"restored": restored, "missing": missing, "applied": bool(apply)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the repair")
    parser.add_argument("--snapshot", default="", help="where to write the snapshot")
    parser.add_argument(
        "--no-backup", action="store_true", help="do not write a snapshot"
    )
    parser.add_argument("--restore", default="", help="put a snapshot back")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = create_app()
    with app.app_context():
        if args.restore:
            print(json.dumps(restore(args.restore, apply=args.apply), indent=2))
            return
        outcome = repair(
            apply=args.apply,
            snapshot_path=args.snapshot,
            backup=not args.no_backup,
        )
        print(json.dumps(outcome, indent=2))
        if not args.apply:
            print("\nDry run. Nothing was written. Pass --apply to repair.")


if __name__ == "__main__":
    main()
