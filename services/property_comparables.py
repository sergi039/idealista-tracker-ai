"""What counts as a comparable listing — one home, two consumers (#386).

`_value_score` and the AI prompt both answer "what do the neighbours ask per
m²", and until this module they each built their own peer set. #378 measured
why that matters and #383 fixed the scorer's half; the prompt's half went on
averaging price per m² over every plot size at once, which is how property 351
(1,300 m², €46/m²) came to be judged `OVERPRICED` against a "local peer
average" of €26/m² carried by two four-thousand-square-metre parcels.

Measured on production 2026-08-17 over all 459 priced plots:

    area band     n     median €/m²        Spearman(area, €/m²) = -0.842
    <800         24        120.5
    800-1499    110         56.3
    1500-2999   212         31.0
    3000-5999    84         15.4
    >=6000       29          4.4

A factor of 27 across the range. So a peer set that ignores size is not a
weaker comparison, it is a different question — and the answer to it reads as
an answer to this one.

The ladder below is #383's, moved rather than reimplemented: geographic scopes
from strict to relaxed, each tried **at a comparable size first**, the band
given up only when no scope finds `min_peers` at that size, and the scope name
recorded so a caller can say which happened. Geography relaxes inside the
banded pass before the band does, because size is the confound this is about:
a 1,200 m² plot in the next municipality is a closer comparable than a
40,000 m² parcel next door.
"""

from typing import Any, Dict, List, Optional, Tuple

from models import Property

# Measured in #378, not chosen: run through this ladder over the 319
# production land rows it is the minimum of the confound curve, removing 86%
# of the double count while still finding a median of 22 comparables. Tighter
# thins the municipality tier until the ladder falls through to a wider scope,
# looser lets the confound back in. See `_collect_peer_ppm2` for the table.
PEER_AREA_BAND_FACTOR = 1.25


def same_municipality(name: str):
    """Peers of a municipality however the row spells it (#377).

    `properties.municipality` is the free text the alert email carried, and
    the same place arrives as `Gijón` and `Gijon`, `Soto del Barco` and `Soto
    Del Barco`. Comparing the raw string made a listing in `Carreño` no peer of
    one in `Carreno`, so the municipality tier silently thinned or fell through
    to the whole region: measured 2026-08-17, 8 rows lost the tier outright
    (property 462 in Castrillón: 1 raw peer against 20) and 56 more compared
    against a fraction of it. The key is `utils.municipality_grouping.group_key`
    -- the same one the /properties dropdown and /municipalities use -- so no
    consumer's idea of "same municipality" can drift from the pages'. A value
    with no key (a truncated `Ovi...`) keeps the exact match: folding it into
    Oviedo by prefix is the wrong-pick hazard the grouping module refuses.
    """
    from utils.municipality_grouping import stored_spellings_of

    spellings = stored_spellings_of(name)
    if not spellings:
        return Property.municipality == name
    return Property.municipality.in_(spellings)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def area_band(area: Optional[float]) -> Optional[Tuple[float, float]]:
    """The comparable-size window around `area`, or None when it has no area."""
    if area is None or area <= 0:
        return None
    return (area / PEER_AREA_BAND_FACTOR, area * PEER_AREA_BAND_FACTOR)


def collect_comparables(
    prop: Property,
    *,
    category: Optional[str],
    min_peers: int,
    limit: int,
    require_price: bool = True,
    band: bool = True,
) -> Tuple[List[Property], Dict[str, Any]]:
    """Comparable rows for `prop`, with the scope that produced them.

    Returns `(rows, meta)`. `meta["comparable_scope"]` names the tier and
    carries a `+area_band` suffix when the rows really are of a comparable
    size; `meta["area_band_m2"]` is present in that case. `meta["size_comparable"]`
    is the same fact as a boolean, because a caller that has to *say* whether
    it compared like with like should not be parsing a scope name to find out.

    `band=False` is for a caller ranking by area itself, where a band would be
    circular: the size component asks "how big is this against the others",
    which is not a question a band around its own area can answer.

    `meta` also names the **population** the answer came from (UNIVERSE-001):
    `peers_used` is what the caller can average, `peers_matched` is what the
    winning scope really holds, `peers_cap` is the ceiling the query stopped
    at, and `profile_scope` says that the pool is the subject's own
    subscription (decision #410 -- a hidden or retired subject keeps its
    same-profile pool, and nothing leaks across subscriptions). Only a capped
    result costs a second query: `peers_matched` is the fetched length unless
    the ceiling was actually reached, so the ordinary path is unchanged.
    """
    scopes: List[Tuple[str, Dict[str, bool]]] = []
    # Strict -> relaxed: municipality+subtype -> subtype -> category-only.
    if prop.municipality and prop.property_subtype:
        scopes.append(("municipality+subtype", {"municipality": True, "subtype": True}))
    if prop.property_subtype:
        scopes.append(("subtype", {"municipality": False, "subtype": True}))
    scopes.append(("category", {"municipality": False, "subtype": False}))

    bounds = area_band(_safe_float(prop.area)) if band else None

    passes: List[Tuple[Optional[Tuple[float, float]], str]] = []
    if bounds is not None:
        passes.append((bounds, "+area_band"))
    passes.append((None, ""))

    profile_scope = (
        "own_subscription"
        if prop.search_profile_id is not None
        else "every_subscription"
    )

    best_rows: List[Property] = []
    best_meta: Dict[str, Any] = {
        "comparable_scope": None,
        "size_comparable": False,
        "peers_used": 0,
        "peers_matched": 0,
        "peers_cap": limit,
        "profile_scope": profile_scope,
    }

    for pass_bounds, suffix in passes:
        for scope_name, cfg in scopes:
            q = Property.query
            if prop.search_profile_id is not None:
                q = q.filter(Property.search_profile_id == prop.search_profile_id)
            q = q.filter(Property.id != prop.id)
            if category:
                q = q.filter(Property.property_category == category)
            if cfg.get("subtype") and prop.property_subtype:
                q = q.filter(Property.property_subtype == prop.property_subtype)
            if cfg.get("municipality") and prop.municipality:
                # #377's normalised key, inside *both* passes: the banded
                # municipality scope is the one most listings land in, so
                # leaving the raw string here would keep #377's worst case
                # alive in the strictest tier while looking closed.
                q = q.filter(same_municipality(prop.municipality))
            if require_price:
                q = q.filter(Property.price.isnot(None))
            q = q.filter(Property.area.isnot(None), Property.area > 0)
            if pass_bounds is not None:
                q = q.filter(
                    Property.area >= pass_bounds[0], Property.area <= pass_bounds[1]
                )

            fetched = q.limit(limit).all()
            rows = [
                p
                for p in fetched
                if _safe_float(p.area) not in (None, 0)
                and (not require_price or _safe_float(p.price) is not None)
            ]
            # What the scope really holds, so a capped pool can say it was
            # capped. The count is asked for only when the ceiling was
            # reached -- otherwise the fetched length *is* the total.
            matched = q.count() if len(fetched) == limit else len(fetched)

            meta: Dict[str, Any] = {
                "comparable_scope": f"{scope_name}{suffix}",
                "size_comparable": pass_bounds is not None,
                "peers_used": len(rows),
                "peers_matched": matched,
                "peers_cap": limit,
                "profile_scope": profile_scope,
            }
            if pass_bounds is not None:
                meta["area_band_m2"] = [
                    round(pass_bounds[0], 1),
                    round(pass_bounds[1], 1),
                ]

            # A banded scope with too few peers must not win on count alone:
            # the fallback exists to produce an answer, not to be preferred.
            if len(rows) >= min_peers:
                return rows, meta
            if len(rows) > len(best_rows):
                best_rows = rows
                best_meta = meta

    return best_rows, best_meta
