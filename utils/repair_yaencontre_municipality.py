"""Re-read the municipality off the titles yaencontre already sent (#507, backwards).

#507 taught `services/yaencontre_source._municipality_from_title` to read past
a district -- "Teis en Vigo" names Vigo, "Casa en venta en Boiro" names Boiro
-- and like every parser fix it only speaks for mail that arrives afterwards.
The rows already in the table keep the string the old reading produced.

Measured on production after #507 deployed: **39 rows still carry a district
where the municipality goes and 63 carry nothing at all**, and the symptom is
on the page. `/properties`' municipality filter offers 16 district options,
and **Vigo is not among them**: its 8 listings sit under "Teis en Vigo" (3),
"Cabral - Candeán en Vigo" (2), "Coruxo - Oia - Saiáns en Vigo" (1),
"Lavadores en Vigo" (1) and "Matamá - … en Vigo" (1), so the municipality
itself cannot be selected at all. `/municipalities` draws each of them as its
own municipality with its own medians and coverage counts.

Nothing is fetched. The title is already stored, so the corrected name is a
re-reading of what the row holds -- which matters more here than usual:
yaencontre answers DataDome to every request from either machine, so a repair
that needed the portal could not run at all.

**The scores go with it.** `services/property_comparables.same_municipality()`
builds a row's peer pool from this string, so moving a listing from a
three-row district to its real municipality changes the neighbours its value
component is measured against. Correcting the name and keeping the number
would leave a judgement made against a peer set the row is no longer in, so
the repaired rows are rescored in the same transaction and the snapshot
carries the score columns and the `scoring` payload beside the name.

**The new name is asked for, not constructed here.** The scope and the value
both come from `_municipality_from_title` itself -- the shipped function, one
underscore and all, because the alternative is a second copy of a reading this
repository has already had to fix once. A row the parser now reads the same
way is out of scope, and a row it can only answer `None` for is **left
alone**: replacing a stored name with a blank is a loss, and "no comma and no
' en '" is the parser's honest refusal rather than a correction.

**And two guards narrow it further, both because the dry run found rows they
had to catch.** The stored value must be one of the two shapes the old reading
produced -- nothing, or a district string still carrying the separator --
because a row holding a plain name was not written by the defect being
repaired (#265: a repair narrower than its defect is a repair whose narrowness
is the safety). And the proposal must be a name the INE register knows, since
a parser reading a title cannot tell a municipality from a street. Both were
measured rather than imagined: of the 253 production rows, four are
hand-imported with the street last -- `Porceyo, Gijón, Calle del Castañeu`,
`Bañugues, Gozón, Calle Go` -- and each already carries the right
municipality, which the naive scope would have replaced with the street. The
register refuses all four proposals and accepts all 102 real ones. Every row a
guard refuses is **reported with its reason**, because this walk is the only
thing that will ever look at these rows and a scope that quietly shrinks
claims a completeness it does not have.

Reports and exits unless `--apply`. The snapshot goes first (`--no-backup` is
a thing you say out loud) and `restore` is compare-and-swap: it touches only
the rows its snapshot names and skips one that no longer differs, so a name
somebody set by hand afterwards is not quietly overwritten.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from app import create_app, db
from models import Property
from services.property_scoring_service import PropertyScoringService
from services.yaencontre_source import (
    _DISTRICT_SEPARATOR,
    _municipality_from_title,
    is_yaencontre_url,
)
from utils import score_snapshot
from utils.municipality_codes import load_name_index, match

logger = logging.getLogger(__name__)

SNAPSHOT_COLUMNS = ("municipality",)


def _proposed(prop: Property) -> Tuple[str, str]:
    """The stored name and what the shipped parser reads today."""
    return (prop.municipality or ""), (_municipality_from_title(prop.title) or "")


def _written_by_the_old_reading(stored: str) -> bool:
    """Is this the shape the reading being repaired produced?

    Two, and only two: nothing at all, or a district string still carrying the
    separator. A row holding a plain municipality name was not written by the
    defect, so it is not this repair's to rewrite -- #265's rule that a repair
    narrower than its defect is a repair whose narrowness is the safety.
    """
    return not stored or _DISTRICT_SEPARATOR in stored


def _names_a_municipality(proposed: str) -> bool:
    """Does the INE register recognise this as a municipality?

    A street is not one, and the difference is not visible to a parser reading
    a title. Measured on the 253 production rows: four hand-imported ones read
    `Porceyo, Gijón, Calle del Castañeu` and `Bañugues, Gozón, Calle Go`, whose
    last comma is a street; the register refuses all four proposals and accepts
    all 102 real ones. It is the join `/municipalities` already uses, folding
    both sides through `normalize()`, so it costs no new reading of a name.
    """
    return match(proposed, load_name_index()) is not None


def _rows_to_repair() -> Tuple[List[Property], List[Dict[str, Any]]]:
    """The rows to repair, and the ones a guard refused with its reason.

    A refusal is reported rather than dropped: this walk is the only thing that
    will ever look at these rows, and a scope that quietly shrinks is a repair
    reporting completeness it did not have.
    """
    candidates = (
        db.session.query(Property)
        .filter(Property.url.isnot(None))
        .order_by(Property.id)
    )
    rows: List[Property] = []
    skipped: List[Dict[str, Any]] = []
    for prop in candidates:
        if not is_yaencontre_url(prop.url):
            continue
        stored, proposed = _proposed(prop)
        if not proposed or proposed == stored:
            # The parser's own refusal, or it already agrees. Writing a blank
            # over a stored name is a loss, not a correction.
            continue
        entry = {"id": prop.id, "stored": stored or None, "proposed": proposed}
        if not _written_by_the_old_reading(stored):
            skipped.append({**entry, "reason": "stored_name_is_not_a_district"})
        elif not _names_a_municipality(proposed):
            skipped.append({**entry, "reason": "proposal_is_not_a_municipality"})
        else:
            rows.append(prop)
    return rows, skipped


def _before(prop: Property) -> Dict[str, Any]:
    """What this row held before anything was written.

    Captured for every row up front, because the rename pass runs before the
    scoring pass and a row read afterwards would report its new name as its
    old one -- the report of what changed, describing no change.
    """
    return {
        "municipality": prop.municipality or None,
        "score_total": score_snapshot.decimal_str(prop.score_total),
    }


def _rescore(prop: Property, before: Dict[str, Any]) -> Dict[str, Any]:
    PropertyScoringService().calculate_for_property(prop)
    return {
        "id": prop.id,
        "before": before,
        "after": {
            "municipality": prop.municipality,
            "score_total": score_snapshot.decimal_str(prop.score_total),
        },
    }


def repair(
    apply: bool = False, snapshot_path: str = "", backup: bool = True
) -> Dict[str, Any]:
    rows, skipped = _rows_to_repair()
    outcome: Dict[str, Any] = {
        "found": len(rows),
        "repaired": 0,
        "applied": bool(apply),
        "snapshot": None,
        "rows": [],
        "skipped": skipped,
    }
    if not rows:
        return outcome

    if apply and backup:
        path = snapshot_path or os.path.join(
            "data",
            "yaencontre_municipality_snapshot_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".json",
        )
        score_snapshot.write(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
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

    # Two passes, and the boundary is a flush. Scoring reaches
    # `property_comparables.same_municipality()`, which asks the *table* which
    # spellings exist -- a live query, and the session autoflushes before it.
    # Renaming and scoring one row at a time therefore scores each row against
    # a half-repaired table: the early rows find a municipality peer pool that
    # is still empty, fall through the comparables ladder to a wider scope, and
    # are written a number the app does not produce from the committed table.
    # Reproduced on six rows sharing one municipality: five of the six moved on
    # a plain re-score afterwards, one by 25.6 points, on the column
    # `/properties` sorts by -- the very fault the rescore exists to prevent,
    # inflicted by the rescore. Nothing here rescores those rows again, so it
    # would have stayed.
    before = {prop.id: _before(prop) for prop in rows}
    for prop in rows:
        prop.municipality = _proposed(prop)[1]
    db.session.flush()
    for prop in rows:
        outcome["rows"].append(_rescore(prop, before[prop.id]))
        outcome["repaired"] += 1

    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return outcome


def restore(path: str, apply: bool = False) -> Dict[str, Any]:
    """Put a snapshot back. The way out, and it is tested."""
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
            apply=args.apply, snapshot_path=args.snapshot, backup=not args.no_backup
        )
        print(json.dumps(outcome, indent=2))
        if not args.apply:
            print("\nDry run. Nothing was written. Pass --apply to repair.")


if __name__ == "__main__":
    main()
