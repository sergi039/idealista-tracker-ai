"""Move provable legacy CSV parcel figures out of the built-area column.

Dry run by default. --apply requires a new rollback snapshot; --restore
compares the current area columns with the repair's write before restoring.
No network, geocoding, model calls or score/enrichment rewrites.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from app import create_app, db
from models import Property
from utils import import_research_sheet, score_snapshot

AREA_COLUMNS = ("area", "plot_area", "area_type")


def _state(prop):
    return {
        key: float(value) if key != "area_type" and value is not None else value
        for key in AREA_COLUMNS
        for value in [getattr(prop, key)]
    }


def proposed_areas(prop):
    """The importer key proves this is still the original Plot m² figure.

    A changed price, area, URL or title refuses automatic repair. A filled
    plot column is left alone. Research prose cannot establish the main
    house's area: guest houses, future buildings and disputed totals need a
    separate sourced correction. This repair leaves the house unmeasured.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    imported = enrichment.get("import")
    imported = imported if isinstance(imported, dict) else {}
    if (
        imported.get("source") != import_research_sheet.SOURCE_NAME
        or prop.area_type != "built"
        or prop.area is None
        or prop.plot_area is not None
        or import_research_sheet._number(prop.area) is None
    ):
        return None
    original_key = import_research_sheet.source_email_id_for(
        {"url": prop.url, "title": prop.title, "price": prop.price, "area": prop.area}
    )
    if prop.source_email_id != original_key:
        return None
    return {"area": None, "plot_area": str(prop.area), "area_type": "built"}


def repair(*, apply=False, snapshot_path="", ids=None):
    if apply and not snapshot_path:
        raise ValueError("--apply requires --snapshot")
    query = Property.query.filter(
        Property.area_type == "built",
        Property.source_email_id.like("research_sheet:%"),
    ).order_by(Property.id)
    if ids:
        query = query.filter(Property.id.in_(ids))
    # Short locked transaction, no external work. Re-read even an identity-map
    # row: another writer may have corrected its columns since it was loaded.
    rows = query.populate_existing().with_for_update().all()
    changes = []
    for prop in rows:
        after = proposed_areas(prop)
        if after is None:
            continue
        before = _state(prop)
        for key, value in after.items():
            setattr(prop, key, value)
        db.session.flush()
        changes.append({"id": prop.id, "before": before, "after": _state(prop)})
    payload = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rows": changes,
    }
    try:
        if apply and changes:
            score_snapshot.write(payload, snapshot_path)
        if apply:
            db.session.commit()
        else:
            db.session.rollback()
    except BaseException:
        db.session.rollback()
        raise
    return {"applied": apply, "count": len(changes), "rows": changes}


def restore(path, *, apply=False):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema") != 1 or not isinstance(payload.get("rows"), list):
        raise ValueError("invalid research-area snapshot")
    rows = payload["rows"]
    # Validate the complete artifact before locking or changing anything.
    seen = set()
    for row in rows:
        if type(row.get("id")) is not int or row["id"] in seen:
            raise ValueError("invalid or duplicate property id")
        seen.add(row["id"])
        for side in ("before", "after"):
            state = row.get(side)
            if not isinstance(state, dict) or set(state) != set(AREA_COLUMNS):
                raise ValueError("snapshot must cover exactly the area columns")
            if state["area_type"] != "built":
                raise ValueError("unexpected area type")
            for key in ("area", "plot_area"):
                value = state[key]
                if value is not None and import_research_sheet._number(value) is None:
                    raise ValueError("invalid area figure")
    restored, skipped = [], []
    for row in rows:
        prop = (
            Property.query.filter_by(id=row["id"])
            .populate_existing()
            .with_for_update()
            .first()
        )
        if prop is None or _state(prop) != row["after"]:
            skipped.append(row["id"])
            continue
        for key, value in row["before"].items():
            setattr(prop, key, value)
        restored.append(row["id"])
    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return {"applied": apply, "restored": restored, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--restore")
    parser.add_argument("--ids", type=int, nargs="+")
    args = parser.parse_args()
    with create_app().app_context():
        result = (
            restore(args.restore, apply=args.apply)
            if args.restore
            else repair(apply=args.apply, snapshot_path=args.snapshot, ids=args.ids)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
