"""Similar to the favorites: how alike a listing is to the ones the owner starred.

The owner's ask (2026-09-02): from the whole Galicia subscription, "the
objects most similar by their characteristics" to the two listings they
favorited, as a filter of its own on /properties. This module is that
reading, and it is deliberately a reading of what the table already holds —
nothing here makes a request, spends anything, or writes a row. The numbers
are derived per request and never stored, so there is no staleness
machinery: a CSV or API value moves the moment a favorite or a located row
changes.

**The references are the favorites of the row's OWN subscription**, exactly
as the criteria are its own subscription's bounds
(services/subscription_criteria.py): a listing in one saved search is never
measured against a favorite in another, an unassigned row has no reference at
all, and the control is offered only while some subscription on screen holds
a favorite or a cut is applied. The favorites themselves read as
`reference` — kept by every cut and carrying the highest sort key, because
they are the definition the cut is made against: they lead the descending
order (the default), and the ascending order, which lists the least alike
first, closes with them.

**What is compared, and how each fact abstains.** Every component is 0–100
or absent, and absent means *one side or the other has not stated the fact*
(#98: a fact nobody stated is not a fact that differs). The score is the
weighted mean over the components both sides state, the nearest reference
wins, and the row reports which facts it rests on — a reader can always see
that a 90 stands on price, area and location alone, and the chip beside the
score prints how many of the eight facts it rests on.

* `price` and `area` — the log ratio, 100 for equal and 0 at twice or half.
  The area is the BUILT surface on the criteria module's own reading
  (`effective_figures`: `area` unless the row says `plot`, where `area` IS
  the parcel), so a parcel is never scored as a house and never scored twice.
* `plot` — the same, on that module's parcel figure (`plot_area`, or `area`
  for bare land): "what is this listing's plot" has one answer on one page.
  On production 2026-09-02 the two Galicia favorites carry their plot only
  under a dossier key (`attributes.plot_area_cadastre_m2`), so the component
  is dormant on THAT subscription until the column is filled — by a hand-set
  writer with a source note, on the owner's word, not by this module and not
  by a bare UPDATE. It is live wherever both sides are bare land, which is
  most of the favorites: 18 of 22 are land (16 `plot`, 2 `developed`), and
  there `area` is the parcel and this is the only surface compared.
* `geography` — a linear decay to 0 at `GEOGRAPHY_SCALE_KM` (60 km: Malpica
  to Fisterra is 55 km, to the Rías Baixas 100+, to the Lugo coast 150+), on
  the row's own coordinate whatever its accuracy or, for a row with none, on
  the **municipality point**: the median of the coordinates the table
  already holds for that municipality, under the same key the /properties
  dropdown groups on. Derived, never stored, and the reading says which
  basis it used and how many located rows made the point. Measured on
  subscription 24 (2026-09-02): a 5 km locality slack is 8.3 points of this
  component (~2 of the total at full coverage, ~3 at the price+area+location
  coverage most rows have); leave-one-out of the municipality point over the
  228 located rows moved the total by median 0.0, p90 1.5, max 5.4 points
  and flipped 0 rows at the 80 cut, 3 at 70, 5 at 60. 299 of 543 rows have
  no coordinate; without the point they would have no location at all.
* `bedrooms` / `bathrooms` — a count difference of 0/1/2 scores 100/60/20.
  Bedrooms read `attributes.bedrooms` (the ingester's key) and then
  `attributes.rooms` (the hand-import scripts' key for idealista's
  *habitaciones*: 24 production rows carry it, the one row carrying both
  agrees).
* `sea_distance` — metres from the sea on the `sea_dist` filter's own two
  keys, 0 at a 2 km difference — but ONLY where the answer is the same at
  every point the coordinate's slack allows (the #358 rule the scorer
  applies): an approximate coordinate's figure is the locality's, a 5 km
  slack against a 2 km scale, so a non-precise side is a band and the
  component is 0 when the bands cannot come within the scale, the plain
  figure when both sides are precise, and absent otherwise. The reading
  says which (`sea_distance_basis`). A listing-pin coordinate (#524, 2 km
  slack) is not told apart here and abstains on the wide side.
* `sea_view` — the verdict's bucket (yes/likely against no); `unknown`
  abstains. Binary, and under nearest-wins it scores 100 for every measured
  row while the favorites span both buckets (they do on production), so it
  lifts measured rows rather than separating candidates.

**A different kind of listing is not scored** (`different_kind`): a plot is
not similar to a house whatever its price, and a terraced house is not what
the owner means by a detached one (the /agencies definition, #474). The kind
comes from category and subtype together — `land` is one kind whatever the
legacy `developed` word says, since sub 17 stars 14 plots beside 2
"developed" parcels — and, for houses, from the typology the title head
states (`property_classification_service.house_typology`); a side that
states neither is compared, never gated. **A row that cannot be placed is
not ranked** (`thin`): location is the one component a claim of similarity
cannot do without, so a row with neither a coordinate nor a municipality
point keeps its number for the reader and never passes a cut, and sorts
last in both directions.

The reading is Python, once per request over the rows of the subscriptions
in scope, and the SQL is *derived* from it — an `id IN (...)` clause and a
`CASE id WHEN ...` sort key — so the list, the map, the CSV, the API and the
row's own page cannot disagree: there is one reading and no twin to drift
from it. The loader reads JSON as text or sub-documents and parses here,
never with a CAST in SQL: a hand-edited value would raise on PostgreSQL and
take the whole page with it (the hazard-service lesson). Measured on
production 2026-09-02: ~90 ms for subscription 24's 563 rows through the
filters' casting expressions, most of it PostgreSQL parsing `enrichment`
per row; the sub-document reads below are what that was traded for.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, case, false, or_

from models import Property, db
from services import subscription_criteria
from services.coordinate_quality import APPROXIMATE_COORD_SLACK_M, is_precise
from services.listing_attribute_filters import LEGACY_SEA_VIEW_TRUE_TEXT
from services.property_classification_service import (
    KIND_HOUSE,
    house_typology,
    listing_kind,
)
from services.sea_view_service import VALID_STATES, haversine_m
from utils.municipality_grouping import group_key

# The weights are a statement of what "similar" means to a house buyer:
# where it is and what it costs first, then how big it is, then the rest.
# Measured against production subscription 24 on 2026-09-02 with the rules
# in this module (565 rows, favorites 969 and 1282): 500 rankable, 56 of a
# different kind (22 non-houses, 34 attached houses), 5 unplaceable; the
# Seiruga neighbour at 3 km, 275k, 292 m² scored 93.7; the Ponteceso / Laxe /
# Cabana houses of 240–320 m² at 250–300k scored 80–88; Camariñas and
# Arteixo at 27–36 km scored ~70; the Rías Baixas scored 0 on location and
# fell below 50. The cuts kept 11 / 42 / 80 rows at ≥ 80 / 70 / 60, of which
# 10 / 30 / 44 rest on price, area and location alone — 305 of the 500 rows
# state only those three facts, which the chip ("3/8") and the line beside
# the count both say. On a LAND subscription the parcel enters the score
# once, at the plot's 1.5 (never again as `area`): that weight was not
# calibrated by the sub-24 run, and changing it is the owner's call.
WEIGHTS: Dict[str, float] = {
    "price": 3.0,
    "area": 2.0,
    "geography": 3.0,
    "plot": 1.5,
    "bedrooms": 1.0,
    "bathrooms": 0.5,
    "sea_distance": 1.0,
    "sea_view": 1.0,
}
TOTAL_WEIGHT = sum(WEIGHTS.values())
FACT_COUNT = len(WEIGHTS)

# The components without which a similarity claim is not made. Location is
# the one: price and area alone would rank a house 150 km away above the one
# in the next village.
REQUIRED_COMPONENTS: Tuple[str, ...] = ("geography",)
# The three facts nearly every row states; a row resting on these alone is
# counted as such in the disclosure line.
BASE_COMPONENTS: Tuple[str, ...] = ("price", "area", "geography")

# The log ratio at which price, area and plot reach 0: twice or half.
RATIO_ZERO_AT = math.log(2.0)
GEOGRAPHY_SCALE_KM = 60.0
SEA_DISTANCE_SCALE_M = 2000.0
COUNT_DIFFERENCE_SCORES: Dict[int, float] = {0: 100.0, 1: 60.0, 2: 20.0}

# Sea-view buckets: the two positive states against the negative one.
_POSITIVE_SEA_VIEW = ("yes", "likely")
_NEGATIVE_SEA_VIEW = ("no",)

# The `similar` parameter's vocabulary: the least similarity a row needs.
# Numbers rather than words, the `sea_dist` precedent: the chip beside every
# row shows the same number, so the cut reads in the row's own units. One
# rounding rule for the cut, the chip and the count: the score is kept to
# one decimal and printed to one decimal, so a 79.6 never prints as 80 while
# the ≥ 80 cut leaves it out.
FILTER_VALUES: Dict[str, float] = {"80": 80.0, "70": 70.0, "60": 60.0}

STATE_REFERENCE = "reference"
STATE_OK = "ok"
STATE_THIN = "thin"
STATE_DIFFERENT_KIND = "different_kind"
STATE_NO_REFERENCE = "no_reference"
STATE_NOTHING_COMPARED = "nothing_compared"

BASIS_COORDINATE = "coordinate"
BASIS_APPROXIMATE = "approximate"
BASIS_MUNICIPALITY = "municipality"

# Firm to loose, for a surface that says when the favorite's basis is the
# weaker side of a distance.
BASIS_ORDER = (BASIS_COORDINATE, BASIS_APPROXIMATE, BASIS_MUNICIPALITY)


def weaker_basis(reading: Dict[str, Any]) -> Optional[str]:
    """The reference's basis when it is looser than the row's own, else None."""
    own, theirs = (
        reading.get("geography_basis"),
        reading.get("reference_geography_basis"),
    )
    if own not in BASIS_ORDER or theirs not in BASIS_ORDER:
        return None
    return theirs if BASIS_ORDER.index(theirs) > BASIS_ORDER.index(own) else None


SEA_BASIS_PARCEL = "parcel"
SEA_BASIS_BAND = "band"

# A reference sorts above every score, whatever a duplicate of it scores:
# the page breaks ties by id, and a copy of a favorite under a lower id
# would otherwise sort above the favorite the cut is made against.
REFERENCE_SORT_KEY = 101.0


@dataclass(frozen=True)
class ListingFacts:
    """The facts one row states, as the comparison reads them.

    Loaded by `load_facts`, the module's ONE loader — there is no second
    loader from an ORM row on purpose: one loader cannot disagree with
    itself, and a detail page reads its row through the same context with
    `candidate_ids`. The sea readings mirror the `sea_dist` and `sea_view`
    filters' SQL expressions key for key, pinned by a test that runs both
    over one fixture.
    """

    id: int
    profile_id: Optional[int]
    is_favorite: bool
    kind: Optional[str]
    typology: Optional[str]
    subtype: Optional[str]
    price: Optional[float]
    area: Optional[float]
    area_type: Optional[str]
    plot_area: Optional[float]
    bedrooms: Optional[float]
    bathrooms: Optional[float]
    lat: Optional[float]
    lon: Optional[float]
    accuracy: Optional[str]
    municipality_key: Optional[str]
    sea_distance_m: Optional[float]
    sea_view_state: Optional[str]


def _positive_number(value: Any, ceiling: Optional[float] = None) -> Optional[float]:
    """A finite positive number, or None. A JSON value may arrive as an int,
    a float, a Decimal or the text a JSON path returns; a NaN and fotocasa's
    `0` blank are absences. `ceiling` is for a SURFACE (the criteria
    module's credibility bound, which is what excludes a PostgreSQL NaN
    there) and is never applied to a price or a distance — a bound about
    square metres says nothing about euros."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if ceiling is not None and number >= ceiling:
        return None
    return number


def _surface(value: Any) -> Optional[float]:
    """A credible surface in m²: the criteria module's own bound."""
    return _positive_number(value, ceiling=subscription_criteria.MAX_CREDIBLE_M2)


def _coordinate(value: Any, limit: float) -> Optional[float]:
    """A coordinate on the globe, or None: NaN, infinity and a latitude of
    400 are bad input, not a location — the `sea_distance_service` rule —
    and a row carrying one takes the municipality path like a row with no
    coordinate at all."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > limit:
        return None
    return number


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _count(value: Any) -> Optional[float]:
    """A room count: a positive whole-ish number. `0` is fotocasa's blank."""
    number = _positive_number(value)
    return number if number is not None and number < 1000 else None


def _as_dict(value: Any) -> Dict[str, Any]:
    """A JSON sub-document as a dict: SQLAlchemy hands back a dict on both
    dialects for an indexed JSON column, and text is parsed defensively."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def sea_distance_from(sea: Any) -> Optional[float]:
    """The `sea_dist` filter's reading of `enrichment["sea"]`, in Python:
    `distance_m` first (the parcel figure, written only for a precise
    coordinate), then `origin_distance_m` (the centroid figure) — the same
    two keys in the same order as `sea_distance_m_expr`, parsed here."""
    block = _as_dict(sea)
    distance = _positive_number(block.get("distance_m"))
    if distance is not None:
        return distance
    return _positive_number(block.get("origin_distance_m"))


def sea_view_state_from(environment: Any, legacy_environment: Any) -> Optional[str]:
    """The `sea_view` filter's reading, in Python: the computed state where it
    is one of the four known ones, else a legacy `true` reads `likely` and
    never `yes`, else None — `sea_view_state_expr` branch for branch."""
    computed = _text(_as_dict(environment).get("sea_view"))
    if computed in VALID_STATES:
        return computed
    legacy = _as_dict(legacy_environment).get("sea_view")
    if legacy is True or (
        _text(legacy) is not None and _text(legacy).lower() in LEGACY_SEA_VIEW_TRUE_TEXT
    ):
        return "likely"
    return None


def facts_from_row(row: Sequence[Any]) -> ListingFacts:
    """One `load_facts` tuple into `ListingFacts`; the column order is the
    query's own, below, and nowhere else."""
    (
        row_id,
        profile_id,
        is_favorite,
        category,
        subtype,
        title,
        price,
        area,
        area_type,
        plot_area,
        bedrooms,
        rooms,
        bathrooms,
        lat,
        lon,
        accuracy,
        municipality,
        sea,
        environment,
        legacy_environment,
    ) = row
    latitude, longitude = _coordinate(lat, 90.0), _coordinate(lon, 180.0)
    if latitude is None or longitude is None:
        latitude = longitude = None
    return ListingFacts(
        id=int(row_id),
        profile_id=int(profile_id) if profile_id is not None else None,
        is_favorite=bool(is_favorite),
        kind=listing_kind(category, subtype),
        typology=house_typology(title),
        subtype=_text(subtype),
        price=_positive_number(price),
        area=_surface(area),
        area_type=_text(area_type),
        plot_area=_surface(plot_area),
        bedrooms=_count(bedrooms) if _count(bedrooms) is not None else _count(rooms),
        bathrooms=_count(bathrooms),
        lat=latitude,
        lon=longitude,
        accuracy=_text(accuracy),
        municipality_key=group_key(municipality),
        sea_distance_m=sea_distance_from(sea),
        sea_view_state=sea_view_state_from(environment, legacy_environment),
    )


def load_facts(
    profile_ids: Iterable[int], candidate_ids: Optional[Iterable[int]] = None
) -> List[ListingFacts]:
    """The rows of the given subscriptions, as facts — every row, or with
    `candidate_ids` only the favorites plus those rows (a page that reads one
    row must not pay for scoring a subscription).

    Typed columns, JSON leaves as text and three small JSON sub-documents;
    nothing is CAST in SQL, and the whole `enrichment` blob never travels.
    """
    ids = sorted({int(pid) for pid in profile_ids})
    if not ids:
        return []
    membership = Property.search_profile_id.in_(ids)
    if candidate_ids is not None:
        wanted_rows = sorted({int(row_id) for row_id in candidate_ids})
        membership = and_(
            membership,
            or_(Property.is_favorite.is_(True), Property.id.in_(wanted_rows)),
        )
    rows = (
        db.session.query(
            Property.id,
            Property.search_profile_id,
            Property.is_favorite,
            Property.property_category,
            Property.property_subtype,
            Property.title,
            Property.price,
            Property.area,
            Property.area_type,
            Property.plot_area,
            Property.attributes["bedrooms"].as_string(),
            Property.attributes["rooms"].as_string(),
            Property.attributes["bathrooms"].as_string(),
            Property.location_lat,
            Property.location_lon,
            Property.location_accuracy,
            Property.municipality,
            Property.enrichment["sea"],
            Property.enrichment["environment"],
            Property.enrichment["legacy_land"]["environment"],
        )
        .filter(membership)
        .order_by(Property.id.asc())
        .all()
    )
    return [facts_from_row(row) for row in rows]


def municipality_points() -> Dict[str, Tuple[float, float, int]]:
    """Where each municipality is, from the coordinates the table holds.

    The median latitude and longitude of every located row sharing the
    municipality key, whatever subscription it is in and whatever its
    accuracy — an approximate row IS the locality's centroid, which is the
    shape of point wanted here — with the number of rows that made it.
    Derived on read, never stored, and only for a row that has no coordinate
    of its own. What it cannot do is notice a municipality whose only located
    rows are wrongly geocoded; with several rows the median shrugs one off,
    with one it cannot, and the reading carries the count so the reader
    knows which they are looking at.
    """
    rows = (
        db.session.query(
            Property.municipality, Property.location_lat, Property.location_lon
        )
        .filter(
            Property.location_lat.isnot(None),
            Property.location_lon.isnot(None),
        )
        .all()
    )
    by_key: Dict[str, List[Tuple[float, float]]] = {}
    for municipality, lat, lon in rows:
        key = group_key(municipality)
        if not key:
            continue
        latitude, longitude = _coordinate(lat, 90.0), _coordinate(lon, 180.0)
        if latitude is None or longitude is None:
            continue
        by_key.setdefault(key, []).append((latitude, longitude))
    return {
        key: (
            statistics.median(lat for lat, _ in points),
            statistics.median(lon for _, lon in points),
            len(points),
        )
        for key, points in by_key.items()
    }


def locate(
    facts: ListingFacts, points: Dict[str, Tuple[float, float, int]]
) -> Optional[Tuple[float, float, str, Optional[int]]]:
    """The row's point, the basis it rests on, and how many rows made a
    municipality point (None for the row's own coordinate)."""
    if facts.lat is not None and facts.lon is not None:
        basis = BASIS_COORDINATE if is_precise(facts.accuracy) else BASIS_APPROXIMATE
        return facts.lat, facts.lon, basis, None
    if facts.municipality_key and facts.municipality_key in points:
        lat, lon, count = points[facts.municipality_key]
        return lat, lon, BASIS_MUNICIPALITY, count
    return None


def _ratio_score(value: float, reference: float) -> float:
    return max(0.0, 1.0 - abs(math.log(value / reference)) / RATIO_ZERO_AT) * 100.0


def _count_score(value: float, reference: float) -> float:
    difference = int(round(abs(value - reference)))
    return COUNT_DIFFERENCE_SCORES.get(difference, 0.0)


def _geography_score(metres: float) -> float:
    return max(0.0, 1.0 - (metres / 1000.0) / GEOGRAPHY_SCALE_KM) * 100.0


def _sea_band(facts: ListingFacts) -> Optional[Tuple[float, float]]:
    """The metres a side's sea distance may really be: a point for a precise
    coordinate, the locality slack either way for anything else."""
    if facts.sea_distance_m is None:
        return None
    if is_precise(facts.accuracy):
        return facts.sea_distance_m, facts.sea_distance_m
    return (
        max(0.0, facts.sea_distance_m - APPROXIMATE_COORD_SLACK_M),
        facts.sea_distance_m + APPROXIMATE_COORD_SLACK_M,
    )


def _sea_distance_component(
    facts: ListingFacts, reference: ListingFacts
) -> Optional[Tuple[float, str]]:
    """The component and its basis, or None where the slack leaves the
    answer open. The decay `1 - |d - d_ref| / scale` is a triangle whose
    only flat region is zero, so the bands settle it in exactly two cases:
    both are points, or they cannot come within the scale of each other."""
    band, reference_band = _sea_band(facts), _sea_band(reference)
    if band is None or reference_band is None:
        return None
    lower, upper = band
    reference_lower, reference_upper = reference_band
    if lower == upper and reference_lower == reference_upper:
        score = max(0.0, 1.0 - abs(lower - reference_lower) / SEA_DISTANCE_SCALE_M)
        return score * 100.0, SEA_BASIS_PARCEL
    smallest_gap = max(0.0, lower - reference_upper, reference_lower - upper)
    if smallest_gap >= SEA_DISTANCE_SCALE_M:
        return 0.0, SEA_BASIS_BAND
    return None


def _sea_view_bucket(state: Optional[str]) -> Optional[str]:
    if state in _POSITIVE_SEA_VIEW:
        return "positive"
    if state in _NEGATIVE_SEA_VIEW:
        return "negative"
    return None


def _typology_applies(facts: ListingFacts) -> bool:
    """The house typology is a fact about houses: it gates only a side that
    is a house or of unknown kind. A parcel titled "Parcela con casa
    adosada" is land, and its title's typology says nothing about it."""
    return facts.kind in (None, KIND_HOUSE)


def _gated(facts: ListingFacts, reference: ListingFacts) -> bool:
    """A different kind, or -- between houses -- a different house typology,
    when both sides state one. A side stating neither is compared, never
    gated (#98)."""
    if facts.kind and reference.kind and facts.kind != reference.kind:
        return True
    if not (_typology_applies(facts) and _typology_applies(reference)):
        return False
    return bool(
        facts.typology and reference.typology and facts.typology != reference.typology
    )


def compare(
    facts: ListingFacts,
    reference: ListingFacts,
    points: Dict[str, Tuple[float, float, int]],
) -> Dict[str, Any]:
    """One row against one reference: the components both state, the score
    over them, and what the comparison rests on."""
    if _gated(facts, reference):
        return {"state": STATE_DIFFERENT_KIND, "reference_id": reference.id}

    components: Dict[str, float] = {}
    basis: Optional[str] = None
    point_count: Optional[int] = None
    sea_basis: Optional[str] = None
    if facts.price and reference.price:
        components["price"] = _ratio_score(facts.price, reference.price)
    figures = subscription_criteria.effective_figures(facts)
    reference_figures = subscription_criteria.effective_figures(reference)
    if figures["house_m2"] and reference_figures["house_m2"]:
        components["area"] = _ratio_score(
            figures["house_m2"], reference_figures["house_m2"]
        )
    if figures["plot_m2"] and reference_figures["plot_m2"]:
        components["plot"] = _ratio_score(
            figures["plot_m2"], reference_figures["plot_m2"]
        )
    if facts.bedrooms is not None and reference.bedrooms is not None:
        components["bedrooms"] = _count_score(facts.bedrooms, reference.bedrooms)
    if facts.bathrooms is not None and reference.bathrooms is not None:
        components["bathrooms"] = _count_score(facts.bathrooms, reference.bathrooms)
    here, there = locate(facts, points), locate(reference, points)
    reference_basis: Optional[str] = None
    if here and there:
        components["geography"] = _geography_score(
            haversine_m(here[0], here[1], there[0], there[1])
        )
        basis, point_count = here[2], here[3]
        reference_basis = there[2]
    sea = _sea_distance_component(facts, reference)
    if sea is not None:
        components["sea_distance"], sea_basis = sea
    bucket, reference_bucket = (
        _sea_view_bucket(facts.sea_view_state),
        _sea_view_bucket(reference.sea_view_state),
    )
    if bucket and reference_bucket:
        components["sea_view"] = 100.0 if bucket == reference_bucket else 0.0

    if not components:
        return {"state": STATE_NOTHING_COMPARED, "reference_id": reference.id}

    compared_weight = sum(WEIGHTS[name] for name in components)
    score = sum(WEIGHTS[name] * value for name, value in components.items())
    score /= compared_weight
    missing_required = [name for name in REQUIRED_COMPONENTS if name not in components]
    compared = [name for name in WEIGHTS if name in components]
    return {
        "state": STATE_THIN if missing_required else STATE_OK,
        "score": round(score, 1),
        "coverage": round(compared_weight / TOTAL_WEIGHT, 3),
        "components": {name: round(value, 1) for name, value in components.items()},
        "compared": compared,
        "compared_count": len(compared),
        "fact_count": FACT_COUNT,
        "base_only": all(name in BASE_COMPONENTS for name in compared),
        "missing_required": missing_required,
        "geography_basis": basis,
        "municipality_point_n": point_count,
        # The favorite's own basis: a precise row measured to a favorite
        # that is a centroid must not read as a coordinate-to-coordinate
        # distance (sub 6's favorite 218 is a centroid, its plots precise).
        "reference_geography_basis": reference_basis,
        "sea_distance_basis": sea_basis,
        "reference_id": reference.id,
    }


def _better(candidate: Dict[str, Any], incumbent: Optional[Dict[str, Any]]) -> bool:
    """Whether `candidate` should replace `incumbent` as the nearest
    reference: a rankable comparison beats a thin one, then the score, then
    the lower reference id so the answer never depends on row order."""
    if incumbent is None:
        return True
    rank = {STATE_OK: 2, STATE_THIN: 1}
    a = (
        rank.get(candidate["state"], 0),
        candidate.get("score") or 0.0,
        -candidate["reference_id"],
    )
    b = (
        rank.get(incumbent["state"], 0),
        incumbent.get("score") or 0.0,
        -incumbent["reference_id"],
    )
    return a > b


def read_against(
    facts: ListingFacts,
    references: Sequence[ListingFacts],
    points: Dict[str, Tuple[float, float, int]],
) -> Dict[str, Any]:
    """The row's reading against its subscription's references."""
    if facts.is_favorite and any(ref.id == facts.id for ref in references):
        return {
            "state": STATE_REFERENCE,
            "score": 100.0,
            "coverage": None,
            "components": {},
            "compared": [],
            "compared_count": 0,
            "fact_count": FACT_COUNT,
            "base_only": False,
            "reference_id": None,
            "geography_basis": None,
            "municipality_point_n": None,
            "reference_geography_basis": None,
            "sea_distance_basis": None,
            "reference_count": len(references),
        }
    if not references:
        return {"state": STATE_NO_REFERENCE, "score": None, "reference_count": 0}
    nearest: Optional[Dict[str, Any]] = None
    gated = 0
    for reference in references:
        result = compare(facts, reference, points)
        if result["state"] in (STATE_DIFFERENT_KIND, STATE_NOTHING_COMPARED):
            gated += result["state"] == STATE_DIFFERENT_KIND
            continue
        if _better(result, nearest):
            nearest = result
    if nearest is None:
        state = (
            STATE_DIFFERENT_KIND if gated == len(references) else STATE_NOTHING_COMPARED
        )
        return {"state": state, "score": None, "reference_count": len(references)}
    return {**nearest, "reference_count": len(references)}


class SimilarityContext:
    """One request's reading: the references per subscription, the
    municipality points, and every candidate row's reading by id.

    Built once and handed to every consumer in the request — the filter
    clause, the sort key, the chips, the disclosure line, the CSV columns —
    the way `criteria_ctx` is, so the page's rows and the counts beside its
    controls are one reading.
    """

    def __init__(
        self,
        references_by_profile: Dict[int, List[ListingFacts]],
        points: Dict[str, Tuple[float, float, int]],
        readings: Dict[int, Dict[str, Any]],
    ):
        self.references_by_profile = references_by_profile
        self.points = points
        self.readings = readings

    @property
    def reference_ids(self) -> List[int]:
        return sorted(
            ref.id for refs in self.references_by_profile.values() for ref in refs
        )

    def reference_count_for(self, profile_id: Optional[int]) -> int:
        if profile_id is None:
            return 0
        return len(self.references_by_profile.get(int(profile_id), ()))

    def read(self, property_id: int) -> Dict[str, Any]:
        """A row's reading; a row outside the context has no reference."""
        return self.readings.get(int(property_id)) or {
            "state": STATE_NO_REFERENCE,
            "score": None,
            "reference_count": 0,
        }

    def kept_ids(self, cut: float) -> List[int]:
        """The ids a cut keeps: every reference, and every rankable row at or
        above it. A thin row is never kept, whatever its number says."""
        return sorted(
            row_id
            for row_id, reading in self.readings.items()
            if reading["state"] == STATE_REFERENCE
            or (reading["state"] == STATE_OK and reading["score"] >= cut)
        )

    def similar_ids(self, cut: float) -> List[int]:
        """`kept_ids` without the references: what the disclosure counts."""
        return [
            row_id
            for row_id in self.kept_ids(cut)
            if self.readings[row_id]["state"] != STATE_REFERENCE
        ]

    def sort_keys(self) -> Dict[int, float]:
        """The ORDER BY value per row: above every score for a reference (so
        it leads the descending order and closes the ascending one), the
        score for a rankable row, and nothing for the rest so they sort last
        both ways."""
        keys: Dict[int, float] = {}
        for row_id, reading in self.readings.items():
            if reading["state"] == STATE_REFERENCE:
                keys[row_id] = REFERENCE_SORT_KEY
            elif reading["state"] == STATE_OK:
                keys[row_id] = float(reading["score"])
        return keys

    def summarize(self, property_ids: Iterable[int]) -> Dict[str, int]:
        """What the given rows read as, counted by state — for the line
        beside the result count, which has to say what "no chip" means on
        this page: unplaceable, a different kind, or nothing to compare to.
        `base_only` counts the rankable rows resting on price, area and
        location alone; `plot_compared` the rankable rows whose plot was."""
        counts = _empty_summary()
        for property_id in property_ids:
            reading = self.read(property_id)
            counts[reading["state"]] = counts.get(reading["state"], 0) + 1
            counts["total"] += 1
            if reading["state"] == STATE_OK:
                if reading.get("base_only"):
                    counts["base_only"] += 1
                if "plot" in reading.get("compared", ()):
                    counts["plot_compared"] += 1
        return counts


def _empty_summary() -> Dict[str, int]:
    return {
        STATE_REFERENCE: 0,
        STATE_OK: 0,
        STATE_THIN: 0,
        STATE_DIFFERENT_KIND: 0,
        STATE_NO_REFERENCE: 0,
        STATE_NOTHING_COMPARED: 0,
        "base_only": 0,
        "plot_compared": 0,
        "total": 0,
    }


def summarize(
    ctx: Optional[SimilarityContext], property_ids: Iterable[int]
) -> Dict[str, int]:
    """`SimilarityContext.summarize`, and the same answer with NO context.

    With no favorite anywhere in the table there is no context, and every
    row reads `no_reference` — which is the honest count and exactly the
    state where the line beside the result count matters most, because a
    cut has then emptied the page. Reading it off the context alone left
    the sentence unrendered in that one state (SIMILAR-001, found by the
    independent review's third round and reproduced): every fixture in the
    suite held a favorite somewhere, so nothing could see it.
    """
    if ctx is not None:
        return ctx.summarize(property_ids)
    counts = _empty_summary()
    for _ in property_ids:
        counts[STATE_NO_REFERENCE] += 1
        counts["total"] += 1
    return counts


def build_context(
    profile_ids: Optional[Iterable[int]] = None,
    candidate_ids: Optional[Iterable[int]] = None,
) -> Optional[SimilarityContext]:
    """The reading for the given subscriptions (every subscription when
    None). None when none of them holds a favorite — the dormant state, in
    which no control is drawn and no query is touched.

    A listing surface hands in the subscriptions it will be asked about —
    the visible ones and the selected ones on an ordinary page, EVERY
    favorite-holding one under a cut or a similarity sort, because then the
    hidden-subscription note counts rows that have to have been scored — and
    gets every row of those that hold a favorite. A page about ONE row hands
    in `candidate_ids` and gets the favorites plus that row. Measured on
    production 2026-09-02 before the sub-document loader: scoring
    subscription 24's 563 rows cost ~67 ms and every subscription with a
    favorite ~177 ms.

    Costs two queries: the rows, and the located rows of the whole table for
    the municipality points.
    """
    favorite_profiles = db.session.query(Property.search_profile_id).filter(
        Property.is_favorite.is_(True), Property.search_profile_id.isnot(None)
    )
    if profile_ids is not None:
        wanted = sorted({int(pid) for pid in profile_ids})
        if not wanted:
            return None
        favorite_profiles = favorite_profiles.filter(
            Property.search_profile_id.in_(wanted)
        )
    with_favorites = sorted({int(pid) for (pid,) in favorite_profiles.distinct()})
    if not with_favorites:
        return None

    facts = load_facts(with_favorites, candidate_ids)
    references_by_profile: Dict[int, List[ListingFacts]] = {
        pid: [] for pid in with_favorites
    }
    for row in facts:
        if row.is_favorite and row.profile_id is not None:
            references_by_profile[row.profile_id].append(row)
    points = municipality_points()
    readings = {
        row.id: read_against(row, references_by_profile.get(row.profile_id, ()), points)
        for row in facts
    }
    return SimilarityContext(references_by_profile, points, readings)


def payload_score(reading: Optional[Dict[str, Any]]) -> Optional[float]:
    """The number a payload or a spreadsheet cell carries for a reading:
    the score where one was measured (`ok`, `reference`, `thin` -- the state
    beside it says which), None where nothing was compared."""
    if not reading:
        return None
    if reading.get("state") in (STATE_OK, STATE_REFERENCE, STATE_THIN):
        return reading.get("score")
    return None


def read_filter_cut(raw_value: Any) -> Optional[float]:
    """The cut a `similar` value names, or None for absent and unknown."""
    return FILTER_VALUES.get(str(raw_value or "").strip())


def apply_filter(query, model, ctx: Optional[SimilarityContext], raw_value: Any):
    """Keep the rows a `similar` cut keeps.

    Hands back the SAME query object for an absent or unknown value (the
    `filter_bar_active` identity contract). A KNOWN cut always narrows, even
    with no favorite anywhere (`ctx` None): "similar to the favorites" of a
    subscription that has none is nothing, and a page that showed every row
    under a control reading "similar ≥ 70" would be a filter that did not
    apply — the surface discloses the narrowing and offers the clear link,
    which is a better sentence than a silently ignored parameter. This is
    deliberately NOT the criteria module's dormant rule: there the absent
    parameter is a hide, here it is nothing.
    """
    cut = read_filter_cut(raw_value)
    if cut is None:
        return query
    kept = ctx.kept_ids(cut) if ctx is not None else []
    return query.filter(model.id.in_(kept))


def sort_expression(model, ctx: Optional[SimilarityContext]):
    """The similarity for ORDER BY: NULL for a thin, gated or unreferenced
    row, so those sort last in BOTH directions (the beach-sort rule), and
    NULL for every row while no favorite exists."""
    if ctx is None:
        return case((false(), model.id), else_=None)
    keys = ctx.sort_keys()
    if not keys:
        return case((false(), model.id), else_=None)
    return case(keys, value=model.id, else_=None)


def similar_count(
    query, model, ctx: Optional[SimilarityContext], raw_value: Any
) -> Optional[int]:
    """How many rows of `query` a cut counts as similar, references aside —
    the number the disclosure line beside the result count prints. None
    when no cut applies."""
    cut = read_filter_cut(raw_value)
    if cut is None:
        return None
    ids = ctx.similar_ids(cut) if ctx is not None else []
    if not ids:
        return 0
    return query.filter(model.id.in_(ids)).count()
