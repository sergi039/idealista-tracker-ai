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
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select

from services import coordinate_quality, owner_review
from services.cadastre_service import normalize_reference
from services.property_location_service import (
    _build_geocoding_queries,
    _normalize_query,
)
from services.recommendation_duplicates import CONFIRMED_ALIASES
from services.yaencontre_source import listing_id_from_url
from utils.idealista_extractors import extract_idealista_property_id
from utils.listing_search import listing_id_clause
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
SOURCE_OWNER = "owner_link"

# The owner's word on a pair (table `property_duplicate_link`, migration 029).
VERDICT_SAME = "same"
VERDICT_DIFFERENT = "different"
LINK_VERDICTS = (VERDICT_SAME, VERDICT_DIFFERENT)
LINK_SOURCES = ("owner", "seed")
MAX_NOTE_LENGTH = 500

_YAENCONTRE_EMAIL_ID = re.compile(r"^yaencontre:(\d+)$")
_COUNTRY_TAIL = re.compile(r",\s*spain\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Edge:
    """Two rows judged to be the same home, at a tier, with its evidence."""

    a: int
    b: int
    tier: str
    evidence: Tuple[Tuple[str, str, Any], ...]  # (signal, status, value)

    def explain(self) -> Dict[str, Any]:
        """The same answer `explain(a, b)` gives, without re-evaluating."""
        return _explanation(self.a, self.b, self.tier, self.evidence)

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


# A bare postcode component ("15165") is not part of the place.
_POSTCODE = re.compile(r"^\d{5}$")

# Folded (accents stripped) words that name a kind of place and nothing more.
# A phrase made only of these identifies nothing and abstains.
GENERIC_PLACE_WORDS: FrozenSet[str] = frozenset(
    {
        "calle", "c", "lugar", "rua", "avenida", "avda", "camino", "barrio",
        "carretera", "plaza", "paseo", "travesia", "urbanizacion", "estrada",
        "aldea", "barriada", "ronda", "via", "pasaje", "de", "del", "la", "el",
        "los", "las", "do", "da", "n", "s", "sn",
    }
)  # fmt: skip


def place_phrase(row: Any) -> Optional[str]:
    """The street / locality phrase the title names, folded; None to abstain.

    Reuses the geocoder's own title reader (`_build_geocoding_queries`), so the
    portals' grammars ("X en venta en <place>", "<type> en <place>",
    "<type> in <place>", "<district> en <municipality>") are read in one place.
    Then, per comma component, `municipality_codes.normalize` (casefold,
    accents, punctuation, articles); postcode components, generic-street-word
    components and the row's own municipality are dropped. Digits that are part
    of the place are kept ("calle 1", "km 3", a house number). What is left must
    be non-empty. A title with no place marker comes
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
        folded = fold_place(component)
        if _POSTCODE.match(folded) or folded == municipality:
            continue
        # Digits stay: "calle 1" and "calle 2" are different streets. A
        # component of nothing but generic street words names no place.
        if all(tok in GENERIC_PLACE_WORDS for tok in folded.split()):
            continue
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
    elif min(len(ref_a), len(ref_b)) < UNIT_REFERENCE_MIN_LENGTH:
        out.append(_signal(SOURCE_CADASTRE, ABSTAINED, "parcel-level reference"))
    elif ref_a != ref_b:
        out.append(_signal(SOURCE_CADASTRE, FAILED, [ref_a, ref_b]))
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


def _vetoed(signals: Iterable[Tuple[str, str, Any]]) -> bool:
    """Two different unit-level cadastral references are two different homes.

    The signal is FAILED only when both sides carry a unit-level reference and
    they differ; a parcel-level reference on either side abstains. A veto
    outranks every other signal, a shared listing id included.
    """
    return any(n == SOURCE_CADASTRE and st == FAILED for n, st, _ in signals)


def _evaluate(a: Any, b: Any) -> Tuple[Optional[str], Tuple[Tuple[str, str, Any], ...]]:
    identity = _identity_signals(a, b)
    three = _three_field_signals(a, b)
    corroboration = _corroboration_signals(a, b)
    signals = tuple(identity + three + corroboration)
    if _vetoed(signals):
        return None, signals
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
    return _explanation(a.id, b.id, tier, signals)


def _explanation(a_id: int, b_id: int, tier: Optional[str], signals) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "a": a_id,
        "b": b_id,
        "tier": tier,
        "signals": {n: {"status": s, "value": v} for n, s, v in signals},
    }
    if _vetoed(signals):
        result["verdict"] = "different"
        result["vetoed_by"] = SOURCE_CADASTRE
    return result


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
        # The three-field key. A row is indexed under each of its price
        # values (current and previous); a row whose area is unknown or
        # disqualified cannot meet the three fields, so it is not indexed.
        municipality, area = group_key(row.municipality), built_area(row)
        if municipality is not None and area is not None:
            for price in prices(row):
                add(("mpa", municipality, price, area), row.id)

    pairs: Set[Tuple[int, int]] = set()
    for ids in buckets.values():
        ordered = sorted(set(ids))
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                pairs.add((left, right))
    return pairs


def edges(rows: Iterable[Any], *, stats: Optional[Dict[str, int]] = None) -> List[Edge]:
    """Every pair that earns a tier, ordered by (a, b).

    Compute once and hand the result to `groups(found_edges=...)`. `stats`, if
    given, receives `evaluated`: how many pairs were looked at.
    """
    by_id = {row.id: row for row in rows}
    pairs = sorted(_candidate_pairs(list(by_id.values())))
    if stats is not None:
        stats["evaluated"] = len(pairs)
    found = []
    for left, right in pairs:
        tier, signals = _evaluate(by_id[left], by_id[right])
        if tier is not None:
            found.append(Edge(left, right, tier, signals))
    return found


def unit_reference(row: Any) -> Optional[str]:
    """The row's unit-level cadastral reference, or None (parcel-level abstains)."""
    ref = cadastral_reference(row)
    return ref if ref is not None and len(ref) >= UNIT_REFERENCE_MIN_LENGTH else None


class _Union:
    """Union-find that refuses a join which would put a vetoed pair together.

    Each component tracks its member ids and its set of unit-level cadastral
    references: two distinct references in one component means a vetoed pair
    (linear, no all-pairs scan). `forbidden` is an injected set of id pairs
    (e.g. the owner's "different" decisions), applied the same way.
    """

    def __init__(
        self,
        rows: Dict[int, Any],
        forbidden: Iterable[Tuple[int, int]] = (),
    ):
        self.parent = {i: i for i in rows}
        self.members = {i: {i} for i in rows}
        self.refs = {
            i: ({r} if (r := unit_reference(row)) is not None else set())
            for i, row in rows.items()
        }
        self.apart: Dict[int, Set[int]] = {}
        for x, y in forbidden:
            self.apart.setdefault(x, set()).add(y)
            self.apart.setdefault(y, set()).add(x)

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def _allowed(self, a: int, b: int, *, force: bool = False) -> bool:
        # `force` is the owner's own `same`: it outranks a cadastral veto, but
        # never the owner's own `different`.
        if not force and len(self.refs[a] | self.refs[b]) > 1:
            return False
        small, large = sorted((a, b), key=lambda r: len(self.members[r]))
        return not any(
            self.apart.get(member, set()) & self.members[large]
            for member in self.members[small]
        )

    def join(self, left: int, right: int, *, force: bool = False) -> bool:
        """Merge the two components; False if that would join a vetoed pair."""
        a, b = self.find(left), self.find(right)
        if a == b:
            return True
        if not self._allowed(a, b, force=force):
            return False
        # The larger member set is kept, so a long chain copies each member a
        # logarithmic number of times rather than once per join; the lower id
        # breaks a tie, which keeps the result independent of input order.
        keep, drop = sorted((a, b), key=lambda r: (-len(self.members[r]), r))
        self.parent[drop] = keep
        self.members[keep] |= self.members.pop(drop)
        self.refs[keep] |= self.refs.pop(drop)
        return True


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
    union = _Union({i: SimpleNamespace() for i in sorted(ids)})
    for e in edge_list:
        union.join(e.a, e.b)
    grouped: Dict[int, List[Edge]] = {}
    for e in edge_list:
        grouped.setdefault(union.find(e.a), []).append(e)
    return [grouped[k] for k in sorted(grouped)]


def groups(
    rows: Iterable[Any],
    *,
    tiers: Sequence[str] = GROUPABLE_TIERS,
    found_edges: Optional[List[Edge]] = None,
    forbidden_pairs: Iterable[Tuple[int, int]] = (),
    owner_same_pairs: Iterable[Tuple[int, int]] = (),
    refused: Optional[List[Edge]] = None,
) -> List[Group]:
    """Connected components over `tiers`; `possible` is never groupable.

    A vetoed pair never shares a group, directly or through an intermediate
    row: two different unit-level cadastral references, or a pair in the
    injected `forbidden_pairs`. Edges are applied strongest tier first, then by
    ids; an edge whose join would put a vetoed pair together is skipped (the
    weaker one loses) and appended to `refused` if given, so it stays visible.

    `owner_same_pairs` are the owner's "same home" decisions: confirmed edges
    with source ``owner_link``, applied before every rule edge and allowed to
    outrank a cadastral veto, but not a pair in `forbidden_pairs` (the owner's
    "different"): that join is refused and reported like any other. A pair
    with an end outside `rows` is ignored.
    """
    unknown = [t for t in tiers if t not in GROUPABLE_TIERS]
    if unknown:
        raise ValueError(f"tiers {unknown} cannot form a group")
    row_list = list(rows)
    by_id = {row.id: row for row in row_list}
    all_edges = edges(row_list) if found_edges is None else found_edges

    used: List[Edge] = [
        e for e in all_edges if e.tier == CONFIRMED and CONFIRMED in tiers
    ]
    if PROBABLE in tiers:
        probable = [e for e in all_edges if e.tier == PROBABLE]
        for component in _probable_components(by_id, probable):
            used.extend(component)

    owner_edges = [
        Edge(a, b, CONFIRMED, ((SOURCE_OWNER, MET, VERDICT_SAME),))
        for a, b in sorted({_ordered(x, y) for x, y in owner_same_pairs if x != y})
        if a in by_id and b in by_id
    ]
    owner_keys = {(e.a, e.b) for e in owner_edges}
    used = [e for e in used if (e.a, e.b) not in owner_keys]

    union = _Union(by_id, forbidden_pairs)
    accepted: List[Edge] = []
    ranked = [(0, e) for e in owner_edges] + [
        (1, e) for e in sorted(used, key=lambda e: (e.tier != CONFIRMED, e.a, e.b))
    ]
    for rank, e in ranked:
        if union.join(e.a, e.b, force=rank == 0):
            accepted.append(e)
        elif refused is not None:
            refused.append(e)
    members: Dict[int, Set[int]] = {}
    edge_map: Dict[int, List[Edge]] = {}
    for e in accepted:
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


def _ordered(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a < b else (b, a)


def groups_with_links(
    rows: Iterable[Any], links: "Links", **kwargs: Any
) -> List[Group]:
    """`groups` with the owner's decisions applied.

    Owner `same` pairs join first; owner `different` pairs are forbidden, so
    they can never share a group, directly or through an intermediate row.
    """
    forbidden = list(kwargs.pop("forbidden_pairs", ())) + sorted(links.different_pairs)
    return groups(
        rows,
        forbidden_pairs=forbidden,
        owner_same_pairs=links.same_pairs,
        **kwargs,
    )


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


# --- the owner's decisions ---------------------------------------------------
#
# `property_duplicate_link` (migration 029) holds the owner's word on a pair:
# "same" or "different". `set_link` is the ONE writer. Nothing here merges,
# folds or deletes a listing.


class LinkError(ValueError):
    """A rejected link write. The route turns it into a flash, never a 500."""


@dataclass(frozen=True)
class Links:
    """The owner's decisions over a set of rows, as ordered (low, high) pairs."""

    same_pairs: FrozenSet[Tuple[int, int]] = frozenset()
    different_pairs: FrozenSet[Tuple[int, int]] = frozenset()

    def __len__(self) -> int:
        return len(self.same_pairs) + len(self.different_pairs)


def _property_id(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LinkError(f"{what} is not a listing id")
    return value


def set_link(
    a_id: int,
    b_id: int,
    *,
    verdict: Optional[str],
    note: Optional[str] = None,
    source: str = "owner",
) -> Dict[str, Any]:
    """Record, change or clear the owner's word on one pair. The one writer.

    ``verdict`` is ``"same"``, ``"different"`` or ``None``; ``None`` deletes the
    link and so restores the computed reading. The pair is stored ordered
    (low < high) and both property rows are locked ``FOR UPDATE`` in id order,
    so two concurrent calls on one pair, in either argument order, queue
    instead of deadlocking, and the second sees the first's row.

    It owns its transaction, with no ``commit=False`` mode (the stance of
    ``owner_review.set_review``): a lock whose release the callee cannot see is
    worse than the race it was taken against.

    The stored ``evidence`` is the rule's ``explain`` output at decision time.
    A ``same`` over a cadastral veto is allowed -- the owner outranks the rule
    -- and the evidence records ``vetoed_by``, so the override is never silent.
    """
    from app import db
    from models import Property, PropertyDuplicateLink

    a_id = _property_id(a_id, "the first listing")
    b_id = _property_id(b_id, "the second listing")
    if a_id == b_id:
        raise LinkError("a listing cannot be linked to itself")
    if verdict is not None and verdict not in LINK_VERDICTS:
        raise LinkError("the decision must be 'same' or 'different'")
    if source not in LINK_SOURCES:
        raise LinkError("unknown source of a link")
    clean_note = (note or "").strip() or None
    if clean_note is not None and len(clean_note) > MAX_NOTE_LENGTH:
        raise LinkError(f"the note is longer than {MAX_NOTE_LENGTH} characters")
    if db.session.new or db.session.dirty or db.session.deleted:
        # This function ends the transaction on every exit, so anything else
        # in flight would be committed or discarded wholesale.
        raise LinkError("a link write needs a session with nothing pending")

    low, high = _ordered(a_id, b_id)
    try:
        # One statement per row, ascending: the lock order is the id order
        # whatever order the caller named them in.
        for property_id in (low, high):
            locked = db.session.execute(
                select(Property.id).where(Property.id == property_id).with_for_update()
            ).scalar_one_or_none()
            if locked is None:
                raise LinkError("that listing does not exist")

        existing = db.session.execute(
            select(PropertyDuplicateLink).where(
                PropertyDuplicateLink.property_low_id == low,
                PropertyDuplicateLink.property_high_id == high,
            )
        ).scalar_one_or_none()

        if verdict is None:
            changed = existing is not None
            if existing is not None:
                db.session.delete(existing)
            db.session.commit()
            return {"changed": changed, "verdict": None, "low": low, "high": high}

        if (
            existing is not None
            and existing.verdict == verdict
            and existing.source == source
            and existing.note == clean_note
        ):
            db.session.rollback()  # release the locks; nothing changed
            return {"changed": False, "verdict": verdict, "low": low, "high": high}

        rows = {
            row.id: row
            for row in db.session.execute(
                select(Property)
                .where(Property.id.in_((low, high)))
                .execution_options(populate_existing=True)
            ).scalars()
        }
        evidence = explain(rows[low], rows[high])
        if existing is None:
            existing = PropertyDuplicateLink(property_low_id=low, property_high_id=high)
            db.session.add(existing)
        existing.verdict = verdict
        existing.source = source
        existing.note = clean_note
        existing.evidence = evidence
        existing.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        return {
            "changed": True,
            "verdict": verdict,
            "low": low,
            "high": high,
            "vetoed_by": evidence.get("vetoed_by"),
        }
    except Exception:
        db.session.rollback()
        raise


def links_for(property_id: int) -> List[Any]:
    """Every link that names this row, ordered by the other end."""
    from models import PropertyDuplicateLink

    return (
        PropertyDuplicateLink.query.filter(
            or_(
                PropertyDuplicateLink.property_low_id == property_id,
                PropertyDuplicateLink.property_high_id == property_id,
            )
        )
        .order_by(
            PropertyDuplicateLink.property_low_id,
            PropertyDuplicateLink.property_high_id,
        )
        .all()
    )


_IN_CHUNK = 500


def load_links(ids: Iterable[int]) -> Links:
    """The owner's decisions between rows of `ids` (both ends must be in it)."""
    from models import PropertyDuplicateLink

    wanted = sorted({int(i) for i in ids})
    members = set(wanted)
    same: Set[Tuple[int, int]] = set()
    different: Set[Tuple[int, int]] = set()
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start : start + _IN_CHUNK]
        for link in PropertyDuplicateLink.query.filter(
            PropertyDuplicateLink.property_low_id.in_(chunk)
        ):
            if link.property_high_id not in members:
                continue
            pair = (link.property_low_id, link.property_high_id)
            (same if link.verdict == VERDICT_SAME else different).add(pair)
    return Links(frozenset(same), frozenset(different))


# --- the detail page's panel -------------------------------------------------

# Each index lookup is capped; a subscription's own twins are a handful.
CANDIDATE_LIMIT = 100

_SIGNAL_ORDER = ("listing_id", SOURCE_SEED, SOURCE_CADASTRE)


def _id_clause(model: Any, listing_ids: Iterable[int]):
    clauses = []
    for n in sorted(set(listing_ids)):
        clauses.append(listing_id_clause(model, n))
        clauses.extend(
            [
                model.source_email_id == f"yaencontre:{n}",
                model.url.ilike(f"%inmueble-%-{n}"),
                model.url.ilike(f"%inmueble-%-{n}/%"),
                model.url.ilike(f"%inmueble-%-{n}?%"),
            ]
        )
    return or_(*clauses)


def _related_listing_ids(row: Any) -> Set[int]:
    """The row's own listing id and every id the seed table ties to it."""
    key = listing_id(row)
    if key is None:
        return set()
    reference = _seed_reference(key)
    related = {key[1]}
    for alias, target in CONFIRMED_ALIASES.items():
        if reference in (alias, target) or _seed_reference(alias) == reference:
            related.update((alias[1], target[1]))
    return related


def candidate_rows(prop: Any) -> List[Any]:
    """Rows that could be this one's twins, from indexed lookups only.

    Bounded: three queries, each limited to `CANDIDATE_LIMIT`, all inside the
    row's own subscription -- the three-field block (price and built area),
    the shared / seeded listing id, and the same unit-level cadastral
    reference. The rule decides; these queries only narrow.
    """
    from models import Property

    if prop.search_profile_id is None:
        return []
    scope = [
        Property.search_profile_id == prop.search_profile_id,
        Property.id != prop.id,
    ]
    found: Dict[int, Any] = {}

    def take(clause: Any) -> None:
        query = Property.query.filter(*scope, clause).order_by(Property.id)
        for row in query.limit(CANDIDATE_LIMIT):
            found[row.id] = row

    area, row_prices = built_area(prop), prices(prop)
    if area is not None and row_prices:
        take(
            and_(
                Property.area.between(area - 0.005, area + 0.005),
                or_(
                    Property.price.in_(row_prices),
                    Property.previous_price.in_(row_prices),
                ),
            )
        )
    ids = _related_listing_ids(prop)
    if ids:
        take(_id_clause(Property, ids))
    reference = unit_reference(prop)
    if reference is not None:
        take(Property.cadastral_reference == reference)
    return list(found.values())


def met_signals(explanation: Dict[str, Any]) -> List[str]:
    """Names of the signals that were met, identity ones first."""
    signals = explanation["signals"]
    met = [n for n, s in signals.items() if s["status"] == MET]
    return sorted(
        met, key=lambda n: _SIGNAL_ORDER.index(n) if n in _SIGNAL_ORDER else 9
    )


def publications_for(prop: Any) -> List[Dict[str, Any]]:
    """The other publications of this home, for the detail page's panel.

    Bounded by `candidate_rows` plus the rows this one is linked to. Each item:
    ``row``, ``tier`` (None when only the owner links them), ``explanation``,
    ``met`` (names of the signals met), ``link`` (the owner's stored link or None). Strongest tier first.
    """
    from models import Property

    links = {
        (
            link.property_high_id
            if link.property_low_id == prop.id
            else link.property_low_id
        ): link
        for link in links_for(prop.id)
    }
    by_id = {row.id: row for row in candidate_rows(prop)}
    missing = [i for i in links if i not in by_id]
    if missing:
        for row in Property.query.filter(Property.id.in_(missing)):
            by_id[row.id] = row

    items = []
    for other_id, other in by_id.items():
        low, high = (prop, other) if prop.id < other.id else (other, prop)
        tier, signals = _evaluate(low, high)
        link = links.get(other_id)
        if tier is None and link is None:
            continue
        explanation = _explanation(low.id, high.id, tier, signals)
        items.append(
            {
                "row": other,
                "tier": tier,
                "explanation": explanation,
                "met": met_signals(explanation),
                "link": link,
            }
        )
    rank = {name: i for i, name in enumerate(TIERS)}
    items.sort(key=lambda item: (rank.get(item["tier"], len(TIERS)), item["row"].id))
    return items
