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

One municipality is one row, whatever the emails called it: rows are grouped
by `utils.municipality_grouping.group_key`, because `municipality` is free
text and "Gijón" / "Gijon" used to render as two places with two medians and
two coverage counts. The name shown is the most readable spelling actually
stored, never a form nobody wrote.

Every metric carries its own coverage count, because a median over 2 of 30
listings is a different claim from a median over 30 of 30, and the page says
which it is. A municipality whose name the INE join cannot resolve shows its
listing medians and an explicit "not matched" for the municipality facts —
never a guessed code (#98's shape, applied to a join).

**The row's link opens the rows the row counted** (#417). A count and a link
that answer different questions are worse than either alone: until this
module carried the scope, every row linked to
`/properties?municipality=<name>&profile_id=all`, and `all` is defined in
this codebase as *active and not hidden*, while the aggregate counts every
stored listing. Measured on production 2026-08-19, 49 of 87 drill-downs
agreed with the row above them; 25 undercounted by 259 listings and 13 opened
on zero while claiming 52. So each row now carries the scope it was computed
under — see `drill_down_args` — and the scope is read off *the rows the
medians came from*, never from a second query, because a second query is how
the two numbers came to disagree in the first place.
"""

import logging
from statistics import median
from typing import Any, Dict, List, Optional

from models import Property
from services.quality_of_life_service import QualityOfLifeService
from services.sea_distance_service import parcel_measurement
from utils.municipality_grouping import group_key, preferred_display

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
    # Read through `parcel_measurement`, not straight off the block: a stored
    # `ok` can be the distance from a locality centroid, and a median over
    # those ranks municipalities by where their town halls sit rather than by
    # their listings. Such a row drops out, which the per-metric coverage
    # count already reports -- it is a smaller sample, not a missing feature.
    sea = parcel_measurement(prop)
    if sea.get("status") != "ok":
        return None
    metres = _finite(sea.get("distance_m"))
    return round(metres / 1000.0, 1) if metres is not None else None


def _price_per_m2(prop: Property) -> Optional[float]:
    price = _finite(prop.price)
    area = _finite(prop.area)
    if price is None or area is None or area <= 0:
        return None
    return price / area


def drill_down_args(
    row: Dict[str, Any], *, favorites_only: bool, hide_removed: bool
) -> Dict[str, Any]:
    """Query arguments opening `/properties` on exactly the rows `row` counted.

    Four axes, and all four have to travel or the link answers a different
    question from the number it sits under (#417):

    * **the contributing subscriptions**, named one by one. `profile_id=all`
      cannot express this: it means *active and not hidden*, while the
      aggregate counts every stored listing, so 311 of 773 production rows sat
      in retired subscriptions the link could not reach. Explicit ids can name
      a retired or hidden subscription, which is what
      `services/profile_selection.py` already supports and what the
      subscription menu already puts a ticked checkbox against.
    * **the unassigned rows**, as the `unassigned` sentinel, and only when such
      rows really contributed. Adding it unconditionally would widen the
      drill-down past its row -- `all` never implies it, and neither does this.
    * **the favorites mode** the aggregate was computed under.
    * **the listing-status scope**. `/municipalities?archived=on` and
      `/properties?hide_removed=on` are the same fact under two names, and the
      names are opposites, so this is the one axis where a copied parameter
      would be exactly wrong.

    `hide_removed` is *omitted* rather than sent as `off`, because that is how
    /properties itself serialises the switch (`base_args` in
    templates/properties.html) and a link that round-trips through the page's
    own controls is one fewer spelling of the same state.

    More contributing subscriptions than `MAX_SELECTED_PROFILE_IDS` would be
    truncated by the parser -- not silently: `ProfileSelection.truncated`
    reaches the page, which says so. Production's worst municipality is carried
    by 8 (2026-08-19), and there are 15 subscriptions in total.
    """
    from services.profile_selection import PROFILE_UNASSIGNED_SENTINEL

    profile_id: List[Any] = list(row.get("profile_ids") or ())
    if row.get("unassigned"):
        profile_id.append(PROFILE_UNASSIGNED_SENTINEL)

    return {
        "municipality": row["name"],
        "profile_id": profile_id,
        "favorites": "on" if favorites_only else None,
        "hide_removed": "on" if hide_removed else None,
    }


# Column keys the page may sort by, and how to read them off a built row.
# Kept here so the route's allow-list cannot drift from what the table shows.
SORT_KEYS = {
    # The canonical key, so "Avilés" sorts where "Aviles" does instead of
    # after every unaccented name -- and so the order does not move when the
    # preferred spelling of a group changes.
    "municipality": lambda row: row["key"],
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
            key = group_key(name)
            if key is None:
                # A listing with no municipality cannot be compared by one,
                # and a truncated email artifact ("Ovi...", issue #298) is
                # not a municipality either -- its INE join can only ever
                # say "not matched" next to the real Oviedo row. Both are
                # counted in the page's footnote instead of inventing a
                # bucket for them.
                continue
            # The canonical key, not `name.lower()`: casefolding alone leaves
            # "Gijón" and "Gijon" as two keys, which rendered one
            # municipality as two rows with two medians and two coverage
            # counts. The spellings are tallied so the row can show the most
            # readable one rather than whichever listing came first.
            group = groups.get(key)
            if group is None:
                group = {
                    "spellings": {},
                    "listings": 0,
                    "favorites": 0,
                    # The subscriptions this municipality's number is made of,
                    # read off the rows themselves (#417). A second query
                    # asking "which profiles hold listings here" would be a
                    # different question the moment either side grew a filter,
                    # which is exactly how the row and its link came to
                    # disagree.
                    "profile_ids": set(),
                    "unassigned": 0,
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

            group["spellings"][name] = group["spellings"].get(name, 0) + 1
            group["listings"] += 1
            if prop.search_profile_id is None:
                group["unassigned"] += 1
            else:
                group["profile_ids"].add(int(prop.search_profile_id))
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
        for key, group in groups.items():
            if group["listings"] < min_listings:
                continue
            row: Dict[str, Any] = {
                "key": key,
                "name": preferred_display(group["spellings"]),
                "listings": group["listings"],
                "favorites": group["favorites"],
                # What the number above is made of. `drill_down_args` turns
                # it into the link, and it is the only reading of "which
                # subscriptions is this municipality made of" on this page --
                # UNIVERSE-001 in #265 wants the same fact said out loud, and
                # it has to come from here rather than from a query of its
                # own, or the sentence and the link describe different sets.
                "profile_ids": tuple(sorted(group["profile_ids"])),
                "unassigned": group["unassigned"],
            }
            for metric, accumulator in group["metrics"].items():
                row[metric] = accumulator.summary()
            row["ine"] = self._ine_facts(row["name"])
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
