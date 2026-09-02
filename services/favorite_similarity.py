"""Similar to the favorites: how alike a listing is to the ones the owner starred.

The owner's ask (2026-09-02): from the whole Galicia subscription, "the
objects most similar by their characteristics" to the two listings they
favorited, as a filter of its own on /properties. This module is that
reading, and it is deliberately a reading of what the table already holds —
nothing here makes a request, spends anything, or writes a row.

**The references are the favorites of the row's OWN subscription**, exactly
as the criteria are its own subscription's bounds
(services/subscription_criteria.py): a listing in one saved search is never
measured against a favorite in another, an unassigned row has no reference at
all, and the control is offered only while some subscription on screen holds
a favorite. The favorites themselves read as `reference` — kept by every cut
and sorted first, because they are the definition the cut is made against.

**What is compared, and how each fact abstains.** Every component is 0–100
or absent, and absent means *one side or the other has not stated the fact*
(#98: a fact nobody stated is not a fact that differs). The score is the
weighted mean over the components both sides state, the nearest reference
wins, and the row reports which facts it rests on — a reader can always see
that a 90 stands on price, area and location alone.

* `price` and `area` — the log ratio, 100 for equal and 0 at twice or half.
* `plot` — the same, on the criteria module's own reading of the parcel
  (`plot_area`, or `area` for bare land). Read there and not re-derived,
  because "what is this listing's plot" has one answer on one page; on
  production 2026-09-02 both favorites carry it only under a dossier key
  (`attributes.plot_area_cadastre_m2`), so this component is dormant until
  the column is filled.
* `geography` — a linear decay to 0 at `GEOGRAPHY_SCALE_KM` (60 km: Malpica
  to Fisterra is 55 km, to the Rías Baixas 100+, to the Lugo coast 150+), on
  the row's own coordinate whatever its accuracy (a locality centroid is
  within 5 km of the parcel, which is nothing at this scale) or, for a row
  with none, on the **municipality point**: the median of the coordinates
  the table already holds for that municipality, under the same key the
  /properties dropdown groups on. That is derived, never stored, and the
  reading says which basis it used. 299 of 543 Galicia rows have no
  coordinate; without this they would have no location at all.
* `bedrooms` / `bathrooms` — a count difference of 0/1/2 scores 100/60/20.
  Bedrooms read `attributes.bedrooms` (the ingester's key) and then
  `attributes.rooms` (the dossier's key for idealista's *habitaciones*; the
  two favorites carry only that one).
* `sea_distance` — metres from the sea on the same reading the `sea_dist`
  filter cuts on, 0 at a 2 km difference.
* `sea_view` — the verdict's bucket (yes/likely against no); `unknown` abstains.

**A different kind of listing is not scored** (`different_kind`): a plot is
not similar to a house whatever its price, so a stated subtype that differs
from the reference's is a gate, not a component. **A row that cannot be
placed is not ranked** (`thin`): location is the one component a claim of
similarity cannot do without, so a row with neither a coordinate nor a
municipality point keeps its number for the reader and never passes a cut,
and sorts last in both directions.

The reading is Python, once per request over the rows of the subscriptions
in scope (measured on production 2026-09-02: ~90 ms for subscription 24's 563
rows, ~60 of them the two `enrichment` JSON readings PostgreSQL parses per
row — kept as the filters' own SQL expressions rather than a Python twin, one
home over 35 ms), and the SQL is *derived* from it — an `id IN (...)` clause and a
`CASE id WHEN ...` sort key — so the list, the map, the CSV, the API and the
row's own page cannot disagree about which rows are similar: there is one
reading and no twin to drift from it.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, case, false, or_

from models import Property, db
from services import subscription_criteria
from services.coordinate_quality import is_precise
from services.listing_attribute_filters import sea_distance_m_expr, sea_view_state_expr
from services.sea_view_service import haversine_m
from utils.municipality_grouping import group_key

# The weights are a statement of what "similar" means to a house buyer:
# where it is and what it costs first, then how big it is, then the rest.
# Calibrated against subscription 24 on 2026-09-02 (543 rows, favorites 969
# and 1282): the Seiruga neighbour at 3 km, 275k, 292 m² scored 94; the
# Ponteceso / Laxe / Cabana houses of 240–320 m² at 250–300k scored 80–88;
# Camariñas and Arteixo at 27–36 km scored ~70; the Rías Baixas scored 0 on
# location and fell below 50.
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

# The components without which a similarity claim is not made. Location is
# the one: price and area alone would rank a house 150 km away above the one
# in the next village.
REQUIRED_COMPONENTS: Tuple[str, ...] = ("geography",)

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
# row shows the same number, so the cut reads in the row's own units.
FILTER_VALUES: Dict[str, float] = {"80": 80.0, "70": 70.0, "60": 60.0}

STATE_REFERENCE = "reference"
STATE_OK = "ok"
STATE_THIN = "thin"
STATE_DIFFERENT_KIND = "different_kind"
STATE_NO_REFERENCE = "no_reference"
STATE_NOTHING_COMPARED = "nothing_compared"

BASIS_COORDINATE = "coordinate"
BASIS_LOCALITY = "locality"
BASIS_MUNICIPALITY = "municipality"


@dataclass(frozen=True)
class ListingFacts:
    """The facts one row states, as the comparison reads them.

    Loaded by `load_facts` through the SAME SQL readings the filters use
    (`sea_distance_m_expr`, `sea_view_state_expr`), so a row's sea distance
    here is the number the `sea_dist` filter would cut on. There is no
    second loader from an ORM row on purpose: one loader cannot disagree
    with itself, and a detail page reads its row through the same context.
    """

    id: int
    profile_id: Optional[int]
    is_favorite: bool
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


def _positive_number(value: Any) -> Optional[float]:
    """A finite positive number, or None. A JSON value may arrive as an int,
    a float, a Decimal or the text a JSON path returns; a NaN, an absurd
    surface and fotocasa's `0` blank are all absences."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= subscription_criteria.MAX_CREDIBLE_M2:
        return None
    return number


def _text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _count(value: Any) -> Optional[float]:
    """A room count: a positive whole-ish number. `0` is fotocasa's blank."""
    number = _positive_number(value)
    return number if number is not None and number < 1000 else None


def facts_from_row(row: Sequence[Any]) -> ListingFacts:
    """One `load_facts` tuple into `ListingFacts`; the column order is the
    query's own, below, and nowhere else."""
    (
        row_id,
        profile_id,
        is_favorite,
        subtype,
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
        sea_distance_m,
        sea_view_state,
    ) = row
    return ListingFacts(
        id=int(row_id),
        profile_id=int(profile_id) if profile_id is not None else None,
        is_favorite=bool(is_favorite),
        subtype=_text(subtype),
        price=_positive_number(price),
        area=_positive_number(area),
        area_type=_text(area_type),
        plot_area=_positive_number(plot_area),
        bedrooms=_count(bedrooms) if _count(bedrooms) is not None else _count(rooms),
        bathrooms=_count(bathrooms),
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        accuracy=_text(accuracy),
        municipality_key=group_key(municipality),
        sea_distance_m=_positive_number(sea_distance_m),
        sea_view_state=_text(sea_view_state),
    )


def load_facts(
    profile_ids: Iterable[int], candidate_ids: Optional[Iterable[int]] = None
) -> List[ListingFacts]:
    """The rows of the given subscriptions, as facts — every row, or with
    `candidate_ids` only the favorites plus those rows (a page that reads one
    row must not pay for scoring a subscription). Typed columns and JSON
    paths only — no whole `enrichment` blob per row, and the JSON values are
    read as text and parsed here rather than cast in SQL, where a hand-edited
    value raises on PostgreSQL and takes the page with it."""
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
            Property.property_subtype,
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
            sea_distance_m_expr(Property),
            sea_view_state_expr(Property),
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
    shape of point wanted here. Derived on read, never stored, and only for
    a row that has no coordinate of its own. What it cannot do is notice a
    municipality whose only located rows are wrongly geocoded; with several
    rows the median shrugs one off, with one it cannot, and the reading says
    `municipality` so the reader knows which basis they are looking at.
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
        try:
            point = (float(lat), float(lon))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(point[0]) and math.isfinite(point[1])):
            continue
        by_key.setdefault(key, []).append(point)
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
) -> Optional[Tuple[float, float, str]]:
    """The row's point and the basis it rests on, or None."""
    if facts.lat is not None and facts.lon is not None:
        basis = BASIS_COORDINATE if is_precise(facts.accuracy) else BASIS_LOCALITY
        return facts.lat, facts.lon, basis
    if facts.municipality_key and facts.municipality_key in points:
        lat, lon, _ = points[facts.municipality_key]
        return lat, lon, BASIS_MUNICIPALITY
    return None


def _ratio_score(value: float, reference: float) -> float:
    return max(0.0, 1.0 - abs(math.log(value / reference)) / RATIO_ZERO_AT) * 100.0


def _count_score(value: float, reference: float) -> float:
    difference = int(round(abs(value - reference)))
    return COUNT_DIFFERENCE_SCORES.get(difference, 0.0)


def _geography_score(metres: float) -> float:
    return max(0.0, 1.0 - (metres / 1000.0) / GEOGRAPHY_SCALE_KM) * 100.0


def _sea_distance_score(metres: float, reference: float) -> float:
    return max(0.0, 1.0 - abs(metres - reference) / SEA_DISTANCE_SCALE_M) * 100.0


def _sea_view_bucket(state: Optional[str]) -> Optional[str]:
    if state in _POSITIVE_SEA_VIEW:
        return "positive"
    if state in _NEGATIVE_SEA_VIEW:
        return "negative"
    return None


def _plot_of(facts: ListingFacts) -> Optional[float]:
    """The criteria module's reading of the parcel, so the plot compared
    here is the plot the criteria verdict on the same page was made on."""
    return subscription_criteria.effective_figures(facts)["plot_m2"]


def compare(
    facts: ListingFacts,
    reference: ListingFacts,
    points: Dict[str, Tuple[float, float, int]],
) -> Dict[str, Any]:
    """One row against one reference: the components both state, the score
    over them, and what the comparison rests on."""
    if facts.subtype and reference.subtype and facts.subtype != reference.subtype:
        return {"state": STATE_DIFFERENT_KIND, "reference_id": reference.id}

    components: Dict[str, float] = {}
    basis: Optional[str] = None
    if facts.price and reference.price:
        components["price"] = _ratio_score(facts.price, reference.price)
    if facts.area and reference.area:
        components["area"] = _ratio_score(facts.area, reference.area)
    plot, reference_plot = _plot_of(facts), _plot_of(reference)
    if plot and reference_plot:
        components["plot"] = _ratio_score(plot, reference_plot)
    if facts.bedrooms is not None and reference.bedrooms is not None:
        components["bedrooms"] = _count_score(facts.bedrooms, reference.bedrooms)
    if facts.bathrooms is not None and reference.bathrooms is not None:
        components["bathrooms"] = _count_score(facts.bathrooms, reference.bathrooms)
    here, there = locate(facts, points), locate(reference, points)
    if here and there:
        components["geography"] = _geography_score(
            haversine_m(here[0], here[1], there[0], there[1])
        )
        basis = here[2]
    if facts.sea_distance_m and reference.sea_distance_m:
        components["sea_distance"] = _sea_distance_score(
            facts.sea_distance_m, reference.sea_distance_m
        )
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
    return {
        "state": STATE_THIN if missing_required else STATE_OK,
        "score": round(score, 1),
        "coverage": round(compared_weight / TOTAL_WEIGHT, 3),
        "components": {name: round(value, 1) for name, value in components.items()},
        "compared": [name for name in WEIGHTS if name in components],
        "missing_required": missing_required,
        "geography_basis": basis,
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
            "reference_id": None,
            "geography_basis": None,
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
        """The ORDER BY value per row: 100 for a reference, the score for a
        rankable row, and nothing for the rest so they sort last both ways."""
        return {
            row_id: float(reading["score"])
            for row_id, reading in self.readings.items()
            if reading["state"] in (STATE_REFERENCE, STATE_OK)
        }


def build_context(
    profile_ids: Optional[Iterable[int]] = None,
    candidate_ids: Optional[Iterable[int]] = None,
) -> Optional[SimilarityContext]:
    """The reading for the given subscriptions (every subscription when
    None). None when none of them holds a favorite — the dormant state, in
    which no control is drawn and no query is touched.

    A listing surface hands in the subscriptions it will be asked about --
    the visible ones and the selected ones, because the chip counts run the
    clause with only the subscription left open -- and gets every row of
    those that hold a favorite. A page about ONE row hands in `candidate_ids`
    and gets the favorites plus that row: measured on production 2026-09-02,
    scoring subscription 24's 563 rows costs ~67 ms and every subscription
    with a favorite ~177 ms, which is a price a list pays once and a detail
    page should not pay at all.

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
