"""Origin provenance shared by every enrichment block that records the
coordinate it was measured from (issue #346).

`services/sea_view_service.py` solved this first for
`enrichment["environment"]["sea_view_detail"]`: capture `{lat, lon}` at
computation time, and compare a stored origin against a fresh one at a
tolerance loose enough to absorb float round-trips through JSON but tight
enough to catch a real move (roughly 1 m at this latitude). This module holds
that primitive once so a second enrichment block (`enrichment["pool"]`, #346)
does not grow a second copy of it. `sea_view_service` now imports from here;
its own behaviour is unchanged.
"""

from typing import Any, Dict, Optional

ORIGIN_TOLERANCE_DEG = 1e-5


def origin_of(prop) -> Optional[Dict[str, float]]:
    """The property's own {lat, lon} at computation time, or None.

    Read straight off `prop`, not off any block already stored on it -- this
    is provenance for a *new* computation, not a copy of an old one.
    """
    lat = getattr(prop, "location_lat", None)
    lon = getattr(prop, "location_lon", None)
    if lat is None or lon is None:
        return None
    try:
        return {"lat": float(lat), "lon": float(lon)}
    except (TypeError, ValueError):
        return None


def origins_agree(
    stored_origin: Optional[Dict[str, Any]], new_origin: Optional[Dict[str, Any]]
) -> Optional[bool]:
    """True/False when both origins are readable, None when one is missing.

    An unreadable origin proves nothing either way -- neither a match nor a
    move.
    """
    if not isinstance(stored_origin, dict) or not isinstance(new_origin, dict):
        return None
    try:
        return (
            abs(float(stored_origin["lat"]) - float(new_origin["lat"]))
            <= ORIGIN_TOLERANCE_DEG
            and abs(float(stored_origin["lon"]) - float(new_origin["lon"]))
            <= ORIGIN_TOLERANCE_DEG
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        # `OverflowError` because a JSON integer has no width limit and
        # PostgreSQL stores it happily: a 310-digit `lat` raises out of
        # `float()` and out of every caller of this function (codex review,
        # 2026-08-20). An origin nobody can read is "cannot tell", which is
        # what this returns for every other unreadable shape.
        return None
