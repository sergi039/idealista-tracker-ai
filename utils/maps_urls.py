"""Google Maps deep links, one builder for every surface.

Only the officially documented URL forms
(https://developers.google.com/maps/documentation/urls/get-started):
`dir/?api=1` for routes, `search/?api=1` for place pins. A directions link
needs both endpoints, so the builders return None rather than a half-built
URL — the template renders plain text instead of a link that opens Google
Maps pointed at nothing. A place id rides along only when it looks like one;
a malformed id is dropped and the coordinate query stands on its own, which
pins the right spot instead of whatever the broken id resolves to.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from urllib.parse import urlencode

# Place ids are opaque, but every real one is a single url-safe token.
_PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")

_TRAVEL_MODES = frozenset({"driving", "walking", "bicycling", "transit", "two-wheeler"})


def _coord(value) -> float | None:
    """A finite latitude/longitude as float, or None.

    Booleans and strings are rejected rather than coerced: a coordinate that
    arrives as text means the caller read the wrong field, and a silent
    float("43,55") style guess is exactly the fragility the old free-text
    directions URLs had.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        as_float = float(value)
        if math.isfinite(as_float):
            return as_float
    return None


def _clean_place_id(place_id) -> str | None:
    if not isinstance(place_id, str):
        return None
    candidate = place_id.strip()
    return candidate if _PLACE_ID_RE.match(candidate) else None


def _point(lat: float, lon: float) -> str:
    # 6 decimals ≈ 0.1 m — plenty, and it keeps URLs stable across float noise.
    return f"{lat:.6f},{lon:.6f}"


def maps_directions_url(
    origin_lat,
    origin_lon,
    dest_lat,
    dest_lon,
    place_id=None,
    travelmode: str = "driving",
) -> str | None:
    """Route from the property to a destination, or None without both ends."""
    o_lat, o_lon = _coord(origin_lat), _coord(origin_lon)
    d_lat, d_lon = _coord(dest_lat), _coord(dest_lon)
    if None in (o_lat, o_lon, d_lat, d_lon):
        return None

    params = {
        "api": "1",
        "origin": _point(o_lat, o_lon),
        "destination": _point(d_lat, d_lon),
        "travelmode": travelmode if travelmode in _TRAVEL_MODES else "driving",
    }
    clean_id = _clean_place_id(place_id)
    if clean_id:
        params["destination_place_id"] = clean_id
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def maps_place_url(lat, lon, place_id=None) -> str | None:
    """Pin a place on the map, or None without a coordinate to pin."""
    p_lat, p_lon = _coord(lat), _coord(lon)
    if None in (p_lat, p_lon):
        return None

    params = {"api": "1", "query": _point(p_lat, p_lon)}
    clean_id = _clean_place_id(place_id)
    if clean_id:
        params["query_place_id"] = clean_id
    return "https://www.google.com/maps/search/?" + urlencode(params)
