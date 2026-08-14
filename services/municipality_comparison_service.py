"""Municipality comparison (agreed proposal D22, owner spec 2026-08-14).

One row per municipality that holds listings, so the owner can ask "where is
it better to live" across the whole search rather than one property at a
time. Two kinds of column, deliberately labeled apart:

* **Municipality facts** — INE renta and población, SEPE registered
  unemployment: exact values for the municipality itself, no listing
  involved;
* **Listing medians** — sea, beach, pool, hospital, supermarket, airport,
  train, price, score: the median over *that municipality's listings*, which
  is what the owner is actually choosing between (owner decision
  2026-08-14, over the capital-centroid alternative). The median, never the
  minimum: a minimum would crown a municipality because one listing happens
  to sit next to a pool — the objection the proposal review raised.

Every metric carries its own coverage count, because a median over 2 of 30
listings is a different claim from a median over 30 of 30, and the page says
which it is. A municipality whose name the INE join cannot resolve shows its
listing medians and an explicit "not matched" for the municipality facts —
never a guessed code (#98's shape, applied to a join).
"""

import logging
from statistics import median
from typing import Any, Dict, List, Optional

from models import Property
from services.quality_of_life_service import QualityOfLifeService

logger = logging.getLogger(__name__)


def _finite(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


class _Metric:
    """A median plus the coverage it was computed from."""

    __slots__ = ("values", "total")

    def __init__(self) -> None:
        self.values: List[float] = []
        self.total = 0

    def add(self, value: Any) -> None:
        self.total += 1
        number = _finite(value)
        if number is not None:
            self.values.append(number)

    def summary(self) -> Dict[str, Any]:
        return {
            "median": round(median(self.values), 1) if self.values else None,
            "measured": len(self.values),
            "total": self.total,
        }


def _nearest_beach_minutes(prop: Property) -> Optional[float]:
    travel = prop.travel if isinstance(prop.travel, dict) else {}
    beaches = travel.get("beaches")
    if not isinstance(beaches, dict):
        return None
    items = beaches.get("items")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    return _finite(first.get("duration_min")) if isinstance(first, dict) else None


def _target_minutes(prop: Property, key: str) -> Optional[float]:
    travel = prop.travel if isinstance(prop.travel, dict) else {}
    targets = travel.get("targets")
    if not isinstance(targets, dict):
        return None
    target = targets.get(key)
    return _finite(target.get("duration_min")) if isinstance(target, dict) else None


def _pool_minutes(prop: Property, require_indoor: bool = False) -> Optional[float]:
    """Drive minutes to the nearest measured qualifying pool.

    Mirrors the scorer's own reading rather than re-deriving it: an
    owner-verified absence and an unverified one are both "no minutes", but
    only the first is a statement about the municipality — and neither is a
    number, so both simply do not enter the median.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    pool = enrichment.get("pool")
    if not isinstance(pool, dict) or pool.get("status") != "ok":
        return None
    best: Optional[float] = None
    for candidate in pool.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if require_indoor and candidate.get("indoor_status") not in (
            "verified",
            "likely",
        ):
            continue
        minutes = _finite(candidate.get("drive_min"))
        if minutes is not None and (best is None or minutes < best):
            best = minutes
    return best


def _qol_part(prop: Property, part: str) -> Dict[str, Any]:
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    block = enrichment.get("quality_of_life")
    if not isinstance(block, dict):
        return {}
    entry = block.get(part)
    return entry if isinstance(entry, dict) else {}


def _hospital_km(prop: Property, grouping: str) -> Optional[float]:
    hospitals = _qol_part(prop, "hospitals")
    if hospitals.get("status") != "ok":
        return None
    nearest = hospitals.get("nearest")
    if not isinstance(nearest, dict):
        return None
    entry = nearest.get(grouping)
    return _finite(entry.get("distance_km")) if isinstance(entry, dict) else None


def _supermarket_km(prop: Property) -> Optional[float]:
    shops = _qol_part(prop, "supermarkets")
    if shops.get("status") != "ok":
        return None
    items = shops.get("items")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    return _finite(first.get("distance_km")) if isinstance(first, dict) else None


def _sea_km(prop: Property) -> Optional[float]:
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    sea = enrichment.get("sea")
    if not isinstance(sea, dict) or sea.get("status") != "ok":
        return None
    metres = _finite(sea.get("distance_m"))
    return round(metres / 1000.0, 1) if metres is not None else None


def _price_per_m2(prop: Property) -> Optional[float]:
    price = _finite(prop.price)
    area = _finite(prop.area)
    if price is None or area is None or area <= 0:
        return None
    return price / area


# Column keys the page may sort by, and how to read them off a built row.
# Kept here so the route's allow-list cannot drift from what the table shows.
SORT_KEYS = {
    "municipality": lambda row: (row["name"] or "").lower(),
    "listings": lambda row: row["listings"],
    "price_per_m2": lambda row: row["price_per_m2"]["median"],
    "price": lambda row: row["price"]["median"],
    "score": lambda row: row["score"]["median"],
    "renta": lambda row: (row["ine"] or {}).get("renta_media_persona"),
    "renta_index": lambda row: (row["ine"] or {}).get("renta_index"),
    "population": lambda row: (row["ine"] or {}).get("population"),
    "population_trend": lambda row: (row["ine"] or {}).get("population_5y_change_pct"),
    "unemployment": lambda row: (row["unemployment"] or {}).get("proxy_pct"),
    "sea_km": lambda row: row["sea_km"]["median"],
    "beach_min": lambda row: row["beach_min"]["median"],
    "pool_min": lambda row: row["pool_min"]["median"],
    "hospital_general_km": lambda row: row["hospital_general_km"]["median"],
    "hospital_teaching_km": lambda row: row["hospital_teaching_km"]["median"],
    "supermarket_km": lambda row: row["supermarket_km"]["median"],
    "airport_min": lambda row: row["airport_min"]["median"],
    "train_min": lambda row: row["train_min"]["median"],
}

DEFAULT_SORT = "listings"


class MunicipalityComparisonService:
    """Builds the /municipalities table from listings + reference data."""

    def __init__(self, qol_service: Optional[QualityOfLifeService] = None):
        self.qol_service = qol_service or QualityOfLifeService()

    def build_rows(
        self, properties: List[Property], min_listings: int = 1
    ) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for prop in properties:
            name = (prop.municipality or "").strip()
            if not name:
                # A listing with no municipality cannot be compared by one;
                # it is counted in the page's footnote instead of inventing
                # a bucket for it.
                continue
            key = name.lower()
            group = groups.get(key)
            if group is None:
                group = {
                    "name": name,
                    "listings": 0,
                    "favorites": 0,
                    "metrics": {
                        metric: _Metric()
                        for metric in (
                            "price",
                            "price_per_m2",
                            "score",
                            "sea_km",
                            "beach_min",
                            "pool_min",
                            "pool_indoor_min",
                            "hospital_general_km",
                            "hospital_teaching_km",
                            "supermarket_km",
                            "airport_min",
                            "train_min",
                        )
                    },
                }
                groups[key] = group

            group["listings"] += 1
            if prop.is_favorite:
                group["favorites"] += 1
            metrics = group["metrics"]
            metrics["price"].add(prop.price)
            metrics["price_per_m2"].add(_price_per_m2(prop))
            metrics["score"].add(prop.score_total)
            metrics["sea_km"].add(_sea_km(prop))
            metrics["beach_min"].add(_nearest_beach_minutes(prop))
            metrics["pool_min"].add(_pool_minutes(prop))
            metrics["pool_indoor_min"].add(_pool_minutes(prop, require_indoor=True))
            metrics["hospital_general_km"].add(_hospital_km(prop, "general_acute"))
            metrics["hospital_teaching_km"].add(
                _hospital_km(prop, "teaching_high_tech")
            )
            metrics["supermarket_km"].add(_supermarket_km(prop))
            metrics["airport_min"].add(_target_minutes(prop, "airport"))
            metrics["train_min"].add(_target_minutes(prop, "train_station"))

        rows = []
        for group in groups.values():
            if group["listings"] < min_listings:
                continue
            row: Dict[str, Any] = {
                "name": group["name"],
                "listings": group["listings"],
                "favorites": group["favorites"],
            }
            for metric, accumulator in group["metrics"].items():
                row[metric] = accumulator.summary()
            row["ine"] = self._ine_facts(group["name"])
            row["unemployment"] = self._unemployment_facts(row["ine"])
            rows.append(row)
        return rows

    def _ine_facts(self, name: str) -> Optional[Dict[str, Any]]:
        """Municipality-level INE facts, or None when the join cannot match."""
        context = self.qol_service.municipality_context(name)
        if context.get("status") != "ok":
            return None
        renta = _finite(context.get("renta_media_persona"))
        province_median = _finite(context.get("renta_province_median"))
        index = (
            round(100.0 * renta / province_median)
            if renta is not None and province_median
            else None
        )
        return {
            "ine_code": context.get("ine_code"),
            "name_matched": context.get("name_matched"),
            "renta_media_persona": renta,
            "renta_year": context.get("renta_year"),
            "renta_index": index,
            "population": context.get("population"),
            "population_year": context.get("population_year"),
            "population_5y_change_pct": context.get("population_5y_change_pct"),
        }

    def _unemployment_facts(
        self, ine: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """SEPE registered unemployment as a labeled proxy, never a rate.

        The published figure is a *count* of registered unemployed; dividing
        it by the total población gives a comparable ratio but not the
        official unemployment rate, and the page says exactly that. Missing
        reference data reads as missing, never as zero.
        """
        if not ine or not ine.get("ine_code"):
            return None
        record = self.qol_service.unemployment_for(ine["ine_code"])
        if not record:
            return None
        total = _finite(record.get("unemployed_total"))
        population = _finite(ine.get("population"))
        proxy = (
            round(100.0 * total / population, 1)
            if total is not None and population
            else None
        )
        return {
            "unemployed_total": total,
            "proxy_pct": proxy,
            "period": record.get("period"),
        }

    def sort_rows(
        self, rows: List[Dict[str, Any]], sort_by: str, descending: bool
    ) -> List[Dict[str, Any]]:
        """Sort by one column, with unmeasured rows always last.

        Nulls-last in *both* directions, like the listing table: a
        municipality nobody measured must never win a "closest to the sea"
        sort by being empty.
        """
        reader = SORT_KEYS.get(sort_by) or SORT_KEYS[DEFAULT_SORT]

        def key(row):
            value = reader(row)
            if isinstance(value, str):
                return (0, value)
            number = _finite(value)
            if number is None:
                return (1, 0.0)
            return (0, -number if descending else number)

        return sorted(rows, key=key)
