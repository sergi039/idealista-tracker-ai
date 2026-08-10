"""Repair listings whose stored price is their price per m² (issue #220).

Until the extractor fix in the same issue, `99,000 € 309 €/m²` was read as a
price change from 99,000 to 309, so 20 of 360 listings were stored, scored and
analysed at their unit price. The true price is in the stored `description` of
every one of them, so this repair needs no email refetch and no paid API.

The rule is deliberately narrow: a row is rewritten only when the price the
description now yields differs from the stored one **and** the stored one is
the per-m² figure that same description states. A row that merely parses
differently today is reported and left alone — re-parsing every description is
not what this tool is for.

A run rewrites `price`, and then `score_total`, `score_investment`,
`score_lifestyle` and `scoring`, because the price criterion reads the price.
Rolling the application back does not roll those columns back, so a write needs
a snapshot it can be restored from:

    python -m utils.repair_prices --dry-run
    python -m utils.repair_prices --snapshot data/price_repair.json
    python -m utils.repair_prices --restore data/price_repair.json
"""

import argparse
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app import create_app, db
from models import Property
from services.property_scoring_service import PropertyScoringService
from utils.idealista_extractors import extract_price, extract_price_per_m2

logger = logging.getLogger(__name__)

# A stored price and a parsed one are the same price when they agree to the
# cent; `price` is Numeric(10, 2) and the extractor returns a float.
PRICE_EPSILON = 0.01


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def diagnose(prop: Property) -> Tuple[str, Optional[float]]:
    """Classify one row: ("repair", new_price) or (reason, None).

    Kept free of database writes so the dry run and the real run cannot drift.
    """
    stored = _as_float(prop.price)
    if stored is None:
        return "no_stored_price", None

    description = prop.description or ""
    if not description.strip():
        return "no_description", None

    parsed = extract_price(description)
    if parsed is None:
        return "description_states_no_price", None
    if abs(parsed - stored) < PRICE_EPSILON:
        return "already_correct", None

    stated_unit_price = extract_price_per_m2(description)
    if stated_unit_price is None:
        return "differs_but_no_unit_price_stated", None
    if abs(stored - stated_unit_price) >= PRICE_EPSILON:
        # It differs for some other reason. Overwriting here would be a second
        # guess at what the price is, which is how a repair becomes a defect.
        return "differs_for_another_reason", None

    return "repair", parsed


SNAPSHOT_FIELDS = (
    "price",
    "score_total",
    "score_investment",
    "score_lifestyle",
    "scoring",
)


def _snapshot_row(prop: Property) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": prop.id}
    for field in SNAPSHOT_FIELDS:
        value = getattr(prop, field)
        if field == "scoring":
            row[field] = value
        else:
            row[field] = str(value) if value is not None else None
    return row


def _write_snapshot(rows: List[Dict[str, Any]], path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        raise SystemExit(
            f"Snapshot {path} already exists; refusing to overwrite a rollback point."
        )
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    logger.info("Wrote rollback snapshot for %s properties to %s", len(rows), path)


def _restore(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)

    restored = 0
    for row in rows:
        prop = db.session.get(Property, row["id"])
        if not prop:
            logger.warning("Property %s from snapshot no longer exists", row["id"])
            continue
        for field in SNAPSHOT_FIELDS:
            value = row.get(field)
            if field == "scoring":
                prop.scoring = value
            else:
                setattr(prop, field, Decimal(value) if value is not None else None)
        restored += 1
    db.session.commit()
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair listings whose stored price is their price per m²."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--snapshot",
        help="Path for the rollback snapshot. Required unless --dry-run.",
    )
    parser.add_argument(
        "--restore",
        help="Restore price and scores from a snapshot file, then exit.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number examined (0 = all)."
    )
    args = parser.parse_args()

    if not args.dry_run and not args.restore and not args.snapshot:
        parser.error("--snapshot is required (or use --dry-run / --restore)")

    app = create_app()
    with app.app_context():
        if args.restore:
            logger.info(
                "Restored %s properties from %s", _restore(args.restore), args.restore
            )
            return

        query = Property.query.filter(Property.price.isnot(None)).order_by(
            Property.id.asc()
        )
        if args.limit:
            query = query.limit(args.limit)
        properties = query.all()

        planned: List[Tuple[Property, float]] = []
        reasons: Dict[str, int] = {}
        for prop in properties:
            verdict, new_price = diagnose(prop)
            reasons[verdict] = reasons.get(verdict, 0) + 1
            if verdict == "repair" and new_price is not None:
                planned.append((prop, new_price))
            elif verdict == "differs_for_another_reason":
                # The interesting skip: name these rows one by one so a second,
                # different defect cannot hide inside a summary count.
                logger.warning(
                    "id=%s stored %s but its description now parses as %s; left alone",
                    prop.id,
                    prop.price,
                    extract_price(prop.description or ""),
                )

        logger.info("Examined %s properties: %s", len(properties), reasons)
        for prop, new_price in planned:
            logger.info(
                "id=%s %s -> %s (area %s, %s)",
                prop.id,
                prop.price,
                new_price,
                prop.area,
                (prop.title or "")[:60],
            )

        if not planned:
            logger.info("Nothing to repair.")
            return

        if args.dry_run:
            logger.info("Dry run: %s rows would be repaired.", len(planned))
            return

        _write_snapshot([_snapshot_row(p) for p, _ in planned], args.snapshot)

        scoring_service = PropertyScoringService()
        repaired = 0
        failed = 0
        for prop, new_price in planned:
            try:
                prop.price = Decimal(str(new_price))
                scoring_service.calculate_for_property(prop, commit=False)
                db.session.commit()
                repaired += 1
            except Exception as exc:
                failed += 1
                db.session.rollback()
                logger.warning("Failed to repair property %s: %s", prop.id, exc)

        logger.info(
            "Done. repaired=%s failed=%s snapshot=%s", repaired, failed, args.snapshot
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
