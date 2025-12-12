import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import create_app, db
from models import Land
from services.anthropic_service import get_anthropic_service


def _as_dict(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _has_investment_rating(land: Land) -> bool:
    analysis = _as_dict(land.ai_analysis)
    rental = analysis.get("rental_market_analysis")
    if not isinstance(rental, dict):
        return False
    rating = rental.get("investment_rating")
    return bool(str(rating).strip()) if rating is not None else False


def _build_property_data(land: Land, existing_analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": land.id,
        "title": land.title,
        "price": float(land.price) if land.price else None,
        "area": float(land.area) if land.area else None,
        "municipality": land.municipality,
        "land_type": land.land_type,
        "score_total": float(land.score_total) if land.score_total else None,
        "description": land.description,
        "travel_time_nearest_beach": land.travel_time_nearest_beach,
        "nearest_beach_name": land.nearest_beach_name,
        "travel_time_oviedo": land.travel_time_oviedo,
        "travel_time_gijon": land.travel_time_gijon,
        "travel_time_airport": land.travel_time_airport,
        "infrastructure_basic": land.infrastructure_basic or {},
        "existing_analysis": existing_analysis,
    }


def _iter_lands(batch_size: int):
    offset = 0
    while True:
        batch = (
            Land.query.order_by(Land.id.asc())
            .limit(batch_size)
            .offset(offset)
            .all()
        )
        if not batch:
            return
        for land in batch:
            yield land
        offset += batch_size


def main() -> int:
    parser = argparse.ArgumentParser(description="Run structured AI analysis in bulk")
    parser.add_argument("--force", action="store_true", help="Re-run even if rating already exists")
    parser.add_argument("--enrich", action="store_true", help="Enrich existing analysis instead of replace")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit", type=int, default=0, help="Process only first N matching lands")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between requests")
    args = parser.parse_args()

    # Keep the log readable even if DEV_MODE enables DEBUG elsewhere.
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("anthropic._base_client").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("services.anthropic_service").setLevel(logging.WARNING)

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("claude_key"):
        print("ANTHROPIC_API_KEY is not configured; aborting.", file=sys.stderr)
        return 2

    app = create_app()
    with app.app_context():
        logging.getLogger().setLevel(logging.INFO)
        anthropic_service = get_anthropic_service()

        total = Land.query.count()
        processed = 0
        skipped = 0
        ok = 0
        failed = 0

        print(f"Total lands in DB: {total}", flush=True)
        print(
            f"Mode: {'FORCE' if args.force else 'ONLY MISSING'}; {'ENRICH' if args.enrich else 'REPLACE'}",
            flush=True,
        )

        for land in _iter_lands(args.batch_size):
            if args.limit and processed >= args.limit:
                break

            has_rating = _has_investment_rating(land)
            if has_rating and not args.force:
                skipped += 1
                continue

            processed += 1
            prefix = f"[{processed}] Land #{land.id}"

            try:
                existing = _as_dict(land.ai_analysis) if args.enrich else None
                property_data = _build_property_data(land, existing_analysis=existing)
                result = anthropic_service.analyze_property_structured(property_data)

                if result and result.get("status") == "success" and result.get("structured_analysis"):
                    new_analysis = result.get("structured_analysis")
                    if args.enrich and existing and isinstance(existing, dict) and isinstance(new_analysis, dict):
                        merged = dict(existing)
                        merged.update(new_analysis)
                        land.ai_analysis = merged
                    else:
                        land.ai_analysis = new_analysis

                    db.session.commit()
                    ok += 1

                    rating_full = None
                    try:
                        rating_full = _as_dict(land.ai_analysis).get("rental_market_analysis", {}).get("investment_rating")
                    except Exception:
                        rating_full = None
                    rating_short = str(rating_full).split("-")[0].strip().upper() if rating_full else "-"
                    print(f"{prefix} OK ({rating_short})", flush=True)
                else:
                    failed += 1
                    db.session.rollback()
                    error = (result or {}).get("error") if isinstance(result, dict) else "Unknown error"
                    print(f"{prefix} FAILED: {error}", file=sys.stderr, flush=True)

            except Exception as e:
                failed += 1
                db.session.rollback()
                print(f"{prefix} EXCEPTION: {e}", file=sys.stderr, flush=True)

            if args.sleep > 0:
                time.sleep(args.sleep)

        print(f"Done. processed={processed} ok={ok} failed={failed} skipped={skipped}", flush=True)
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
