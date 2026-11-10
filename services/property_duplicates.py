"""The one rule for "these rows are publications of the same home" (#633).

Computed on read, over rows already loaded; nothing is stored and there is no
SQL twin (the pattern of `services/favorite_similarity.py`). A missing fact
abstains: it never corroborates and never vetoes.

Three tiers, each with the evidence that earned it:

* ``confirmed`` -- the same portal listing id across the idealista <->
  yaencontre pair (yaencontre re-publishes idealista ads under the same
  numeric id; measured on production 2026-11-10: 47 of 47 pairs were the same
  home, although price differed in 4 and area matched in only 3), or a link in
  the owner-corroborated seed table, or the same unit-level cadastral
  reference (the column is nearly empty today; the rule is dormant but
  correct).
* ``probable`` -- the three fields (municipality key, price, built area) AND
  one corroboration (the same place named in the title, or coordinates close
  enough). Three fields alone collide by chance in about 2-5% of pairs.
* ``possible`` -- the three fields with no corroboration. Reported, never
  grouped.

What is deliberately NOT evidence: an idealista row's `area` (108 of 118
stated values are exactly 150.00, a suspected placeholder, #645), bedrooms and
bathrooms (they disagree inside true duplicates), photos and descriptions
(they do not match across portals).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from services import coordinate_quality, owner_review
from services.cadastre_service import normalize_reference
from services.property_location_service import (
    _build_geocoding_queries,
    _normalize_query,
)
from services.recommendation_duplicates import CONFIRMED_ALIASES
from services.yaencontre_source import listing_id_from_url
from utils.idealista_extractors import extract_idealista_property_id
from utils.listing_source import (
    FOTOCASA,
    IDEALISTA,
    MILANUNCIOS,
    YAENCONTRE,
    source_of,
)
from utils.municipality_codes import normalize as fold_place
from utils.municipality_grouping import group_key

CONFIRMED = "confirmed"
PROBABLE = "probable"
POSSIBLE = "possible"
TIERS = (CONFIRMED, PROBABLE, POSSIBLE)
GROUPABLE_TIERS = (CONFIRMED, PROBABLE)

MET = "met"
FAILED = "failed"
ABSTAINED = "abstained"

# Coordinates corroborate only inside these radii. A portal pin is a ~500 m
# privacy circle, so two pins for one home on one portal sit well within 200 m
# of each other; across portals each draws its own circle, hence 1 km.
SAME_PORTAL_MAX_DISTANCE_M = 200.0
CROSS_PORTAL_MAX_DISTANCE_M = 1_000.0

# Built areas are compared exactly: the 223-group measurement of 2026-11-10
# (2549 rows, 517 rows / 294 surplus; 76 groups cross-portal, 147 same-portal)
# grouped on equal stored values, and no wider band has been measured.
AREA_TOLERANCE_M2 = 0.0
_EPSILON = 1e-9

# Idealista `area` is disqualified: 108 of 118 stated values are exactly
# 150.00 (#645). It neither supports nor vetoes a match.
AREA_DISQUALIFIED_PORTALS: FrozenSet[str] = frozenset({IDEALISTA})

# Portals whose listing id is the same number for the same home (47 of 47).
SHARED_ID_PORTALS: FrozenSet[str] = frozenset({IDEALISTA, YAENCONTRE})

# 14 characters name a parcel (a whole building can share one); 18 and 20 name
# a unit inside it. Only a unit-level reference identifies one home.
UNIT_REFERENCE_MIN_LENGTH = 18

# Representative order across portals, best first.
PORTAL_ORDER: Tuple[str, ...] = (IDEALISTA, FOTOCASA, YAENCONTRE, MILANUNCIOS)

SOURCE_SEED = "seed"
SOURCE_SHARED_ID = "shared_listing_id"
SOURCE_CADASTRE = "cadastral_reference"

_YAENCONTRE_EMAIL_ID = re.compile(r"^yaencontre:(\d+)$")
_COUNTRY_TAIL = re.compile(r",\s*spain\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Edge:
    """Two rows judged to be the same home, at a tier, with its evidence."""

    a: int
    b: int
    tier: str
    evidence: Tuple[Tuple[str, str, Any], ...]  # (signal, status, value)

    def signals(self) -> Dict[str, Dict[str, Any]]:
        return {name: {"status": s, "value": v} for name, s, v in self.evidence}


@dataclass(frozen=True)
class Group:
    members: Tuple[Any, ...]  # rows, ordered by id
    edges: Tuple[Edge, ...]

    @property
    def ids(self) -> Tuple[int, ...]:
        return tuple(row.id for row in self.members)

    @property
    def tier(self) -> str:
        """`probable` if any probable edge was needed to join it."""
        return PROBABLE if any(e.tier == PROBABLE for e in self.edges) else CONFIRMED


# --- readings of one row ----------------------------------------------------


def listing_id(row: Any) -> Optional[Tuple[str, int]]:
    """`(portal, id)` for an idealista or yaencontre row, else None.

    Idealista: the `idealista_property_id` column, else `/inmueble/<id>` in
    the URL. Yaencontre: the second number of `inmueble-<a>-<b>` in the URL,
    else the `yaencontre:<id>` `source_email_id`.
    """
    portal = source_of(row)
    url = (getattr(row, "url", None) or "").strip()
    if portal == IDEALISTA:
        stored = getattr(row, "idealista_property_id", None)
        if stored is not None:
            return portal, int(stored)
        path = urlsplit(url if "//" in url else f"//{url}").path if url else ""
        found = extract_idealista_property_id(path)
        return (portal, found) if found is not None else None
    if portal == YAENCONTRE:
        found = listing_id_from_url(url) if url else None
        if found is None:
            email = _YAENCONTRE_EMAIL_ID.match(
                (getattr(row, "source_email_id", None) or "").strip()
            )
            found = int(email.group(1)) if email else None
        return (portal, found) if found is not None else None
    return None


def _seed_reference(key: Optional[Tuple[str, int]]) -> Optional[Tuple[str, int]]:
    return CONFIRMED_ALIASES.get(key, key) if key is not None else None


def cadastral_reference(row: Any) -> Optional[str]:
    return normalize_reference(getattr(row, "cadastral_reference", None))


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


def prices(row: Any) -> FrozenSet[float]:
    """The prices the row has asked: current and previous, positive only."""
    found = (
        _number(getattr(row, "price", None)),
        _number(getattr(row, "previous_price", None)),
    )
    return frozenset(round(v, 2) for v in found if v is not None)


def built_area(row: Any) -> Optional[float]:
    """The row's area as evidence, or None when unknown or disqualified."""
    if source_of(row) in AREA_DISQUALIFIED_PORTALS:
        return None
    value = _number(getattr(row, "area", None))
    return round(value, 2) if value is not None else None


def place_phrase(row: Any) -> Optional[str]:
    """The street / locality phrase the title names, folded; None to abstain.

    Reuses the geocoder's own title reader (`_build_geocoding_queries`), so the
    portals' grammars ("X en venta en <place>", "<type> en <place>",
    "<type> in <place>", "<district> en <municipality>") are read in one place.
    Then, per comma component, `municipality_codes.normalize` (casefold,
    accents, punctuation, articles); components that are only numbers (house
    numbers differ between portals) or that are the row's own municipality are
    dropped. What is left must be non-empty. A title with no place marker comes
    back from the reader as the whole title, which is prose and not a place, so
    it abstains.
    """
    title = getattr(row, "title", None)
    if not title or not str(title).strip():
        return None
    queries = _build_geocoding_queries(SimpleNamespace(title=title, municipality=None))
    if not queries:
        return None
    phrase = _COUNTRY_TAIL.sub("", queries[0])
    if fold_place(phrase) == fold_place(_normalize_query(str(title)) or ""):
        # No place marker: the reader handed the whole title back, which is
        # prose and not a place.
        return None
    municipality = group_key(getattr(row, "municipality", None))
    parts = []
    for component in phrase.split(","):
        folded = " ".join(
            tok for tok in fold_place(component).split() if not tok.isdigit()
        )
        if folded and folded != municipality:
            parts.append(folded)
    return ", ".join(parts) or None


def _usable_point(row: Any) -> Optional[Tuple[float, float]]:
    """A coordinate that says something about this advert, else None.

    A locality centroid is shared by every row of the town and would
    corroborate anything, so only `precise` points and portal / hand-set pins
    count (`coordinate_quality.coordinate_tier`).
    """
    lat, lon = getattr(row, "location_lat", None), getattr(row, "location_lon", None)
    if lat is None or lon is None:
        return None
    if coordinate_quality.coordinate_tier(row) == coordinate_quality.TIER_LOCALITY:
        return None
    return float(lat), float(lon)


# --- the pair evaluation ----------------------------------------------------


def _signal(name: str, status: str, value: Any = None):
    return (name, status, value)


def _identity_signals(a: Any, b: Any) -> List[Tuple[str, str, Any]]:
    key_a, key_b = listing_id(a), listing_id(b)
    out = []
    if key_a is None or key_b is None:
        out.append(_signal("listing_id", ABSTAINED, "a portal id is unreadable"))
    elif key_a[0] == key_b[0] or not {key_a[0], key_b[0]} <= SHARED_ID_PORTALS:
        out.append(_signal("listing_id", ABSTAINED, "not an idealista/yaencontre pair"))
    elif key_a[1] == key_b[1]:
        out.append(_signal("listing_id", MET, key_a[1]))
    else:
        out.append(_signal("listing_id", FAILED, [key_a[1], key_b[1]]))

    if (
        key_a is not None
        and key_b is not None
        and key_a != key_b
        and _seed_reference(key_a) == _seed_reference(key_b)
        and (key_a in CONFIRMED_ALIASES or key_b in CONFIRMED_ALIASES)
    ):
        out.append(_signal(SOURCE_SEED, MET, list(_seed_reference(key_a))))
    else:
        out.append(_signal(SOURCE_SEED, ABSTAINED, "no seed link"))

    ref_a, ref_b = cadastral_reference(a), cadastral_reference(b)
    if ref_a is None or ref_b is None:
        out.append(_signal(SOURCE_CADASTRE, ABSTAINED, "no reference on a side"))
    elif ref_a != ref_b:
        out.append(_signal(SOURCE_CADASTRE, FAILED, [ref_a, ref_b]))
    elif len(ref_a) < UNIT_REFERENCE_MIN_LENGTH:
        out.append(_signal(SOURCE_CADASTRE, ABSTAINED, "parcel-level reference"))
    else:
        out.append(_signal(SOURCE_CADASTRE, MET, ref_a))
    return out


def _three_field_signals(a: Any, b: Any) -> List[Tuple[str, str, Any]]:
    out = []
    key_a, key_b = group_key(a.municipality), group_key(b.municipality)
    if key_a is None or key_b is None:
        out.append(_signal("municipality", ABSTAINED, "no municipality key"))
    elif key_a == key_b:
        out.append(_signal("municipality", MET, key_a))
    else:
        out.append(_signal("municipality", FAILED, [key_a, key_b]))

    pa, pb = prices(a), prices(b)
    if not pa or not pb:
        out.append(_signal("price", ABSTAINED, "no price on a side"))
    elif pa & pb:
        out.append(_signal("price", MET, sorted(pa & pb)))
    else:
        out.append(_signal("price", FAILED, [sorted(pa), sorted(pb)]))

    area_a, area_b = built_area(a), built_area(b)
    if area_a is None or area_b is None:
        disqualified = AREA_DISQUALIFIED_PORTALS & {source_of(a), source_of(b)}
        out.append(
            _signal(
                "area",
                ABSTAINED,
                "idealista area is disqualified"
                if disqualified
                else "no area on a side",
            )
        )
    elif abs(area_a - area_b) <= AREA_TOLERANCE_M2 + _EPSILON:
        out.append(_signal("area", MET, area_a))
    else:
        out.append(_signal("area", FAILED, [area_a, area_b]))
    return out


def _corroboration_signals(a: Any, b: Any) -> List[Tuple[str, str, Any]]:
    out = []
    place_a, place_b = place_phrase(a), place_phrase(b)
    if place_a is None or place_b is None:
        out.append(_signal("title_place", ABSTAINED, "no place in a title"))
    elif place_a == place_b:
        out.append(_signal("title_place", MET, place_a))
    else:
        out.append(_signal("title_place", FAILED, [place_a, place_b]))

    point_a, point_b = _usable_point(a), _usable_point(b)
    if point_a is None or point_b is None:
        out.append(_signal("coordinates", ABSTAINED, "no usable point on a side"))
    else:
        metres = coordinate_quality._haversine_m(*point_a, *point_b)
        limit = (
            SAME_PORTAL_MAX_DISTANCE_M
            if source_of(a) == source_of(b)
            else CROSS_PORTAL_MAX_DISTANCE_M
        )
        status = MET if metres <= limit else FAILED
        out.append(_signal("coordinates", status, round(metres, 1)))
    return out


def _evaluate(a: Any, b: Any) -> Tuple[Optional[str], Tuple[Tuple[str, str, Any], ...]]:
    identity = _identity_signals(a, b)
    three = _three_field_signals(a, b)
    corroboration = _corroboration_signals(a, b)
    signals = tuple(identity + three + corroboration)
    if any(status == MET for _, status, _ in identity):
        return CONFIRMED, signals
    if all(status == MET for _, status, _ in three):
        if any(status == MET for _, status, _ in corroboration):
            return PROBABLE, signals
        return POSSIBLE, signals
    return None, signals


def explain(a: Any, b: Any) -> Dict[str, Any]:
    """The tier (or None) and every signal with its value and status."""
    tier, signals = _evaluate(a, b)
    return {
        "a": a.id,
        "b": b.id,
        "tier": tier,
        "signals": {n: {"status": s, "value": v} for n, s, v in signals},
    }


# --- edges and groups --------------------------------------------------------


def _candidate_pairs(rows: Sequence[Any]) -> Set[Tuple[int, int]]:
    """Pairs worth evaluating, from indexes; never the full cross product."""
    buckets: Dict[Any, List[int]] = {}

    def add(key: Any, row_id: int) -> None:
        buckets.setdefault(key, []).append(row_id)

    for row in rows:
        key = listing_id(row)
        if key is not None:
            if key[0] in SHARED_ID_PORTALS:
                add(("id", key[1]), row.id)
            add(("seed", _seed_reference(key)), row.id)
        ref = cadastral_reference(row)
        if ref is not None:
            add(("ref", ref), row.id)
        municipality = group_key(row.municipality)
        if municipality is not None:
            for price in prices(row):
                add(("mp", municipality, price), row.id)

    pairs: Set[Tuple[int, int]] = set()
    for ids in buckets.values():
        ordered = sorted(set(ids))
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                pairs.add((left, right))
    return pairs


def edges(rows: Iterable[Any]) -> List[Edge]:
    """Every pair that earns a tier, ordered by (a, b)."""
    by_id = {row.id: row for row in rows}
    found = []
    for left, right in sorted(_candidate_pairs(list(by_id.values()))):
        tier, signals = _evaluate(by_id[left], by_id[right])
        if tier is not None:
            found.append(Edge(left, right, tier, signals))
    return found


class _Union:
    def __init__(self, ids: Iterable[int]):
        self.parent = {i: i for i in ids}

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _probable_components(
    by_id: Dict[int, Any], probable: List[Edge]
) -> List[List[Edge]]:
    """Probable edges grouped so a component stays inside one three-field block.

    Block: municipality key + built area. Inside it, a component is kept whole
    only when every member shares one price value (current or previous); a
    component that walks across prices (A and B share a drop, A and C share
    another) is split into the edges between rows of the same current price.
    """
    blocks: Dict[Any, List[Edge]] = {}
    for edge in probable:
        row = by_id[edge.a]
        blocks.setdefault(
            (
                group_key(row.municipality),
                built_area(by_id[edge.a]) or built_area(by_id[edge.b]),
            ),
            [],
        ).append(edge)

    components: List[List[Edge]] = []
    for block_edges in blocks.values():
        for component in _components(block_edges):
            members = {i for e in component for i in (e.a, e.b)}
            shared = frozenset.intersection(*(prices(by_id[i]) for i in members))
            if shared:
                components.append(component)
                continue
            same_price = [
                e
                for e in component
                if _current_price(by_id[e.a]) == _current_price(by_id[e.b])
            ]
            components.extend(_components(same_price))
    return components


def _current_price(row: Any) -> Optional[float]:
    value = _number(getattr(row, "price", None))
    return round(value, 2) if value is not None else None


def _components(edge_list: List[Edge]) -> List[List[Edge]]:
    ids = {i for e in edge_list for i in (e.a, e.b)}
    union = _Union(sorted(ids))
    for e in edge_list:
        union.join(e.a, e.b)
    grouped: Dict[int, List[Edge]] = {}
    for e in edge_list:
        grouped.setdefault(union.find(e.a), []).append(e)
    return [grouped[k] for k in sorted(grouped)]


def groups(
    rows: Iterable[Any], *, tiers: Sequence[str] = GROUPABLE_TIERS
) -> List[Group]:
    """Connected components over `tiers`; `possible` is never groupable."""
    unknown = [t for t in tiers if t not in GROUPABLE_TIERS]
    if unknown:
        raise ValueError(f"tiers {unknown} cannot form a group")
    row_list = list(rows)
    by_id = {row.id: row for row in row_list}
    all_edges = edges(row_list)

    used: List[Edge] = [
        e for e in all_edges if e.tier == CONFIRMED and CONFIRMED in tiers
    ]
    if PROBABLE in tiers:
        probable = [e for e in all_edges if e.tier == PROBABLE]
        for component in _probable_components(by_id, probable):
            used.extend(component)

    union = _Union(sorted(by_id))
    for e in used:
        union.join(e.a, e.b)
    members: Dict[int, Set[int]] = {}
    edge_map: Dict[int, List[Edge]] = {}
    for e in used:
        root = union.find(e.a)
        members.setdefault(root, set()).update((e.a, e.b))
        edge_map.setdefault(root, []).append(e)
    result = [
        Group(
            tuple(by_id[i] for i in sorted(members[root])),
            tuple(sorted(edge_map[root], key=lambda e: (e.a, e.b))),
        )
        for root in members
    ]
    result.sort(key=lambda g: g.ids)
    return result


# --- representative and folding ---------------------------------------------


def _portal_rank(row: Any) -> int:
    portal = source_of(row)
    return PORTAL_ORDER.index(portal) if portal in PORTAL_ORDER else len(PORTAL_ORDER)


def _decided(row: Any) -> bool:
    return bool(owner_review.read_decision(row)["decided"])


def representative(group: Group) -> Any:
    """Favourite, owner decision, live status, precise, portal order, lowest id."""

    def priority(row: Any):
        return (
            bool(getattr(row, "is_favorite", False)),
            _decided(row),
            getattr(row, "listing_status", None) not in {"removed", "sold"},
            coordinate_quality.is_precise(getattr(row, "location_accuracy", None)),
            -_portal_rank(row),
            -row.id,
        )

    return max(group.members, key=priority)


def protection(row: Any, activity_ids: FrozenSet[int] = frozenset()) -> Optional[str]:
    """Why this row may never be folded, or None."""
    if getattr(row, "is_favorite", False):
        return "favorite"
    if _decided(row):
        return "owner_decision"
    if owner_review.read_action(row)["state"] != owner_review.ACTION_NONE:
        return "outstanding_action"
    if row.id in activity_ids:
        return "timeline_entry"
    return None


def folded_ids(
    group_list: Iterable[Group], *, activity_ids: Iterable[int] = ()
) -> Set[int]:
    """Members that would fold into their group's representative.

    `activity_ids` is the set of property ids with a live timeline entry; it
    is injected so this function stays pure. A protected row is never in the
    result, whatever the group says.
    """
    activity = frozenset(activity_ids)
    folded: Set[int] = set()
    for group in group_list:
        keep = representative(group)
        for row in group.members:
            if row.id != keep.id and protection(row, activity) is None:
                folded.add(row.id)
    return folded
