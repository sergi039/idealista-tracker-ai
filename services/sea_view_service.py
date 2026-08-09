"""Sea-view estimation for universal `Property` rows.

Why this exists
---------------
`Land` carried a boolean `environment.sea_view` produced by matching 17 Spanish
keywords against the listing text. On the 168 legacy rows that boolean is
`true` exactly **once**, and the text it reads is not the advertisement -- it is
the ~300-character fragment the alert email carries. A boolean built on that is
indistinguishable from "we have no idea", which is the same failure #98
documented for travel times: a refusal written to the database as a result.

So the verdict here is deliberately not a boolean:

``yes``      two independent sources agree -- the listing claims a sea view and
             the terrain allows one.
``likely``   one source says yes and nothing contradicts it. Geometry alone can
             never do better than this: EU-DEM is a *bare-earth* model, so
             trees and buildings are invisible to it.
``no``       computed and negative -- no coastline in range, or terrain blocks
             the line of sight.
``unknown``  we could not compute it. Approximate coordinates, or an external
             source that refused. Never silently folded into ``no``.

Both external sources are free and keyless: OpenStreetMap (`natural=coastline`)
and Copernicus EU-DEM 25 m through OpenTopoData. Google is not involved, so the
billing that blocks #98 does not block this.
"""

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from config import Config
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from utils.http import request_with_retries

logger = logging.getLogger(__name__)

# --- verdict states ---------------------------------------------------------

YES = "yes"
LIKELY = "likely"
NO = "no"
UNKNOWN = "unknown"

VALID_STATES = (YES, LIKELY, NO, UNKNOWN)

# --- tuning constants -------------------------------------------------------

# Past this the sea stops being a view and starts being a horizon smudge; it is
# also where a 25 m terrain model stops being trustworthy.
MAX_SEA_DISTANCE_M = 12_000

# An "approximate" coordinate is a Nominatim centroid: two different plots in
# Siero share one point. Geometry on that point is meaningless *unless* the
# whole neighbourhood is far inland, so a negative verdict needs this much slack
# on top of MAX_SEA_DISTANCE_M before it is honest.
APPROXIMATE_COORD_SLACK_M = 5_000

# Observer sits above the plot: a house, or simply standing on rising ground.
EYE_HEIGHT_M = 5.0

# EU-DEM is 25 m and bare-earth. Terrain has to clear the sight line by this
# much before it counts as blocking, otherwise DEM noise decides the verdict.
TERRAIN_CLEARANCE_M = 2.0

# Standard atmospheric refraction: light bends, so the earth behaves as if it
# were 7/6 of its radius.
EARTH_RADIUS_M = 6_371_000.0
EFFECTIVE_EARTH_RADIUS_M = EARTH_RADIUS_M * 7.0 / 6.0

# Coastline is fetched per grid cell rather than per property, because
# overpass-api.de grants two concurrent slots per IP. A 0.1 degree cell is about
# 11 km by 8 km at Spanish latitudes, so its half-diagonal is under 8 km; the
# query radius carries that on top of the widest per-property search, which is
# what makes "no coastline in the cell" a sound negative for every point in it.
COASTLINE_CELL_DEGREES = 0.1
COASTLINE_CELL_HALF_DIAGONAL_M = 8_000
COASTLINE_QUERY_RADIUS_M = (
    MAX_SEA_DISTANCE_M + APPROXIMATE_COORD_SLACK_M + COASTLINE_CELL_HALF_DIAGONAL_M
)
OVERPASS_MIN_INTERVAL_S = 2.0

MIN_PROFILE_SAMPLES = 12
MAX_PROFILE_SAMPLES = 60
PROFILE_SAMPLE_SPACING_M = 150

COASTLINE_CACHE_TIMEOUT_S = 60 * 60 * 24 * 30
GEOMETRY_CACHE_TIMEOUT_S = 60 * 60 * 24 * 7

# overpass-api.de answers `406 Not Acceptable` to the default `python-requests`
# User-Agent, and also to any UA carrying a parenthetical comment -- measured,
# not guessed: `IdealistaRank/1.0 (personal property tracker)` is refused while
# the bare product token is served. Keep this a plain token.
HTTP_USER_AGENT = "IdealistaRank/1.0"

# --- text signals -----------------------------------------------------------

# An unambiguous claim of a *view*. Used directly when the AI bridge is down.
VIEW_KEYWORDS = (
    "vista al mar",
    "vistas al mar",
    "vista mar",
    "vistas mar",
    "vista del mar",
    "vistas del mar",
    "vistas sobre el mar",
    "vistas al cantabrico",
    "vistas al cantábrico",
    "sea view",
    "sea views",
    "ocean view",
    "ocean views",
    "views of the sea",
    "view of the sea",
)

# Mentions the sea without claiming a view. "primera línea" and "frente al mar"
# live here on purpose: they are the phrases that made the legacy flag
# untrustworthy, and deciding what they mean in context is exactly the job the
# AI filter is given.
PROXIMITY_KEYWORDS = (
    "frente al mar",
    "primera linea",
    "primera línea",
    "junto al mar",
    "cerca del mar",
    "cerca de la playa",
    "a pie de playa",
    "beach front",
    "beachfront",
    "waterfront",
)

TEXT_VIEW = "view"
TEXT_PROXIMITY = "proximity"
TEXT_NONE = "none"
TEXT_UNAVAILABLE = "unavailable"

_AI_SYSTEM = (
    "You classify Spanish and English real-estate listing text. Answer with one "
    "JSON object and nothing else."
)

_AI_PROMPT = """Does this listing text claim the property has a VIEW of the sea?

Rules:
- "view" only when the text says you can see the sea from the property
  (vistas al mar, sea views, frente al mar overlooking the water...).
- "proximity" when the sea or a beach is only mentioned as being nearby
  (cerca del mar, a pie de playa, a 5 minutos de la playa...).
- "none" when the sea is not really claimed at all, or the mention belongs to
  the town name or the agency boilerplate.
- Marketing text is not evidence of a view unless it actually says so.

Text:
\"\"\"{text}\"\"\"

Answer: {{"claim": "view" | "proximity" | "none", "quote": "<the phrase you used, or empty>"}}"""


# --- geometry helpers -------------------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, in degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        d_lambda
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _interpolate(
    lat1: float, lon1: float, lat2: float, lon2: float, fraction: float
) -> Tuple[float, float]:
    """Linear interpolation. Over the <=12 km spans used here the error against
    a great-circle interpolation is centimetres, well inside a 25 m DEM cell."""
    return (
        lat1 + (lat2 - lat1) * fraction,
        lon1 + (lon2 - lon1) * fraction,
    )


def _curvature_drop_m(distance_m: float) -> float:
    return (distance_m**2) / (2.0 * EFFECTIVE_EARTH_RADIUS_M)


# --- external sources -------------------------------------------------------


def _cache_get(lat: float, lon: float, data_type: str):
    """Cache read that cannot change a verdict.

    The cache needs a Flask application context and a configured backend; a
    script or a unit test may have neither. A cache that is merely absent must
    cost a round trip, not an exception.
    """
    try:
        return get_cached_enrichment_data(lat, lon, data_type)
    except Exception as exc:
        logger.debug("Sea-view cache read skipped: %s", exc)
        return None


def _cache_set(lat: float, lon: float, data_type: str, data, timeout: int) -> None:
    try:
        cache_enrichment_data(lat, lon, data_type, data, timeout=timeout)
    except Exception as exc:
        logger.debug("Sea-view cache write skipped: %s", exc)


class SeaViewSourceError(RuntimeError):
    """An external source refused or returned something unusable.

    Raised rather than returned so a refusal can never be mistaken for a
    computed negative -- the mistake #98 is about.
    """


_last_overpass_call_at = 0.0


def coastline_cell(lat: float, lon: float) -> Tuple[float, float]:
    """The grid cell a point belongs to, as (centre_lat, centre_lon)."""
    return (
        round(lat / COASTLINE_CELL_DEGREES) * COASTLINE_CELL_DEGREES,
        round(lon / COASTLINE_CELL_DEGREES) * COASTLINE_CELL_DEGREES,
    )


def fetch_coastline_points(
    lat: float, lon: float, session: Optional[requests.Session] = None
) -> List[Tuple[float, float]]:
    """Coastline node coordinates near a point, from OpenStreetMap.

    The query is issued for the point's *grid cell*, not the point, and cached
    per cell. overpass-api.de grants two query slots per IP and answers 504
    while both are busy, so one query per property would be both abusive and
    unworkable: the 351 rows occupy 67 cells, and the 34 in Siero share one.

    An empty list means Overpass answered and there is no coastline in range.
    A refusal raises -- it must never read as "no sea here".
    """
    cell_lat, cell_lon = coastline_cell(lat, lon)
    cache_type = f"sea_view_coastline_cell_r{COASTLINE_QUERY_RADIUS_M}_v1"
    cached = _cache_get(cell_lat, cell_lon, cache_type)
    if cached is not None:
        return [tuple(point) for point in cached]

    query = (
        "[out:json][timeout:90];"
        f"way(around:{COASTLINE_QUERY_RADIUS_M},{cell_lat:.4f},{cell_lon:.4f})"
        '["natural"="coastline"];'
        "out geom;"
    )
    http = session or requests

    global _last_overpass_call_at
    wait = OVERPASS_MIN_INTERVAL_S - (time.monotonic() - _last_overpass_call_at)
    if wait > 0:
        time.sleep(wait)

    try:
        response = request_with_retries(
            http.post,
            Config.OSM_OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": HTTP_USER_AGENT},
            # A 504 here means "both slots are busy", not a broken request.
            # Measured against the live instance, a slot frees up in roughly 80
            # seconds, so the backoff has to out-wait that: 8+16+32+64 covers it
            # with room to spare. Giving up early would turn a busy server into
            # `unknown` for a whole cell.
            max_attempts=5,
            backoff_base=8.0,
            backoff_max=90.0,
            timeout=120,
            logger=logger,
        )
    except requests.RequestException as exc:
        raise SeaViewSourceError(f"Overpass request failed: {exc}") from exc
    finally:
        _last_overpass_call_at = time.monotonic()

    if response.status_code != 200:
        raise SeaViewSourceError(f"Overpass returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SeaViewSourceError("Overpass returned a non-JSON body") from exc

    # An answer that is present but unusable is not an answer. A coastline way
    # whose geometry is missing, empty, or unparsable would otherwise be dropped
    # silently, and the caller would read the shortened list -- or the empty one
    # -- as the measured fact "no coastline here".
    points: List[Tuple[float, float]] = []
    for element in payload.get("elements", []):
        geometry = element.get("geometry")
        if not isinstance(geometry, list) or not geometry:
            raise SeaViewSourceError("Overpass returned a way without geometry")
        for node in geometry:
            node_lat, node_lon = node.get("lat"), node.get("lon")
            if node_lat is None or node_lon is None:
                raise SeaViewSourceError("Overpass returned a node without coordinates")
            try:
                lat_value, lon_value = float(node_lat), float(node_lon)
            except (TypeError, ValueError) as exc:
                raise SeaViewSourceError(
                    "Overpass returned an unparsable coordinate"
                ) from exc
            if not (-90.0 <= lat_value <= 90.0) or not (-180.0 <= lon_value <= 180.0):
                raise SeaViewSourceError("Overpass returned a coordinate out of range")
            points.append((lat_value, lon_value))

    _cache_set(
        cell_lat, cell_lon, cache_type, points, timeout=COASTLINE_CACHE_TIMEOUT_S
    )
    return points


_last_elevation_call_at = 0.0


def fetch_elevations(
    points: Sequence[Tuple[float, float]], session: Optional[requests.Session] = None
) -> List[Optional[float]]:
    """Ground elevation in metres for each point, in order.

    `None` for a point the model has no value at -- typically open water.
    A refusal raises rather than returning zeros.
    """
    if not points:
        return []
    if len(points) > Config.SEA_VIEW_ELEVATION_MAX_LOCATIONS:
        raise ValueError(
            f"{len(points)} locations exceeds the "
            f"{Config.SEA_VIEW_ELEVATION_MAX_LOCATIONS}-location request cap"
        )

    global _last_elevation_call_at
    wait = Config.SEA_VIEW_ELEVATION_MIN_INTERVAL_S - (
        time.monotonic() - _last_elevation_call_at
    )
    if wait > 0:
        time.sleep(wait)

    locations = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    http = session or requests
    try:
        response = request_with_retries(
            http.get,
            Config.SEA_VIEW_ELEVATION_URL,
            params={"locations": locations},
            headers={"User-Agent": HTTP_USER_AGENT},
            timeout=60,
            logger=logger,
        )
    except requests.RequestException as exc:
        raise SeaViewSourceError(f"Elevation request failed: {exc}") from exc
    finally:
        _last_elevation_call_at = time.monotonic()

    if response.status_code != 200:
        raise SeaViewSourceError(f"Elevation API returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SeaViewSourceError("Elevation API returned a non-JSON body") from exc

    if payload.get("status") != "OK":
        raise SeaViewSourceError(
            f"Elevation API status {payload.get('status')}: {payload.get('error')}"
        )
    results = payload.get("results") or []
    if len(results) != len(points):
        raise SeaViewSourceError(
            f"Elevation API returned {len(results)} results for {len(points)} points"
        )

    elevations: List[Optional[float]] = []
    for item in results:
        value = item.get("elevation")
        elevations.append(None if value is None else float(value))
    return elevations


# --- geometry verdict -------------------------------------------------------


def _profile_sample_count(distance_m: float) -> int:
    wanted = int(distance_m // PROFILE_SAMPLE_SPACING_M)
    return max(MIN_PROFILE_SAMPLES, min(MAX_PROFILE_SAMPLES, wanted))


def evaluate_geometry(
    lat: float,
    lon: float,
    coordinate_accuracy: Optional[str] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Can the sea be seen from this point, as far as terrain is concerned?

    Returns a dict with `state` in VALID_STATES plus the numbers behind it.
    """
    approximate = (coordinate_accuracy or "").lower() != "precise"
    cache_type = "sea_view_geometry_v1"
    if use_cache:
        cached = _cache_get(lat, lon, cache_type)
        if cached is not None:
            return dict(cached)

    # How far the sea has to be before a negative is safe. An approximate
    # coordinate is a municipality centroid, so the real plot may sit kilometres
    # from the point that was measured; only sea beyond this distance makes the
    # imprecision irrelevant.
    decisive_distance = MAX_SEA_DISTANCE_M + (
        APPROXIMATE_COORD_SLACK_M if approximate else 0
    )
    detail: Dict[str, Any] = {
        "coordinate_accuracy": coordinate_accuracy or "unknown",
        "decisive_distance_m": decisive_distance,
    }

    try:
        coastline = fetch_coastline_points(lat, lon, session=session)
    except SeaViewSourceError as exc:
        logger.warning("Coastline lookup unavailable for %.5f,%.5f: %s", lat, lon, exc)
        detail.update({"state": UNKNOWN, "reason": "coastline_source_unavailable"})
        return detail

    if not coastline:
        # The cell query reaches further than any per-property decision
        # distance, so an empty answer rules the sea out for every point in the
        # cell. This negative is earned.
        detail.update(
            {
                "state": NO,
                "reason": "no_coastline_in_range",
                "distance_m": None,
            }
        )
        if use_cache:
            _cache_set(lat, lon, cache_type, detail, timeout=GEOMETRY_CACHE_TIMEOUT_S)
        return detail

    nearest = min(coastline, key=lambda p: haversine_m(lat, lon, p[0], p[1]))
    distance = haversine_m(lat, lon, nearest[0], nearest[1])
    detail["distance_m"] = round(distance, 1)
    detail["bearing_deg"] = round(bearing_deg(lat, lon, nearest[0], nearest[1]), 1)

    if distance > decisive_distance:
        detail.update({"state": NO, "reason": "sea_too_far"})
        if use_cache:
            _cache_set(lat, lon, cache_type, detail, timeout=GEOMETRY_CACHE_TIMEOUT_S)
        return detail

    if approximate:
        # There *is* sea within reach of the municipality centroid, so the
        # centroid tells us nothing about this particular plot.
        detail.update({"state": UNKNOWN, "reason": "approximate_coordinates"})
        return detail

    sample_count = _profile_sample_count(distance)
    fractions = [i / (sample_count + 1) for i in range(1, sample_count + 1)]
    profile_points = [(lat, lon)] + [
        _interpolate(lat, lon, nearest[0], nearest[1], f) for f in fractions
    ]

    try:
        elevations = fetch_elevations(profile_points, session=session)
    except (SeaViewSourceError, ValueError) as exc:
        logger.warning("Elevation lookup unavailable for %.5f,%.5f: %s", lat, lon, exc)
        detail.update({"state": UNKNOWN, "reason": "elevation_source_unavailable"})
        return detail

    observer_ground = elevations[0]
    if observer_ground is None:
        detail.update({"state": UNKNOWN, "reason": "no_elevation_at_property"})
        return detail

    observer_height = observer_ground + EYE_HEIGHT_M
    detail["observer_elevation_m"] = round(observer_ground, 1)
    detail["eye_height_m"] = EYE_HEIGHT_M
    detail["profile_samples"] = sample_count

    # A point at or below sea level with the sea 12 km away sees nothing; the
    # sight line to sea level is flat and the earth curves away underneath it.
    if observer_height <= _curvature_drop_m(distance):
        detail.update({"state": NO, "reason": "below_the_horizon"})
        if use_cache:
            _cache_set(lat, lon, cache_type, detail, timeout=GEOMETRY_CACHE_TIMEOUT_S)
        return detail

    null_samples = 0
    for fraction, elevation in zip(fractions, elevations[1:]):
        if elevation is None:
            # EU-DEM has no value over open water. Treating it as sea level is
            # the physical reading, and it is counted so the bias is visible.
            null_samples += 1
            elevation = 0.0
        sample_distance = distance * fraction
        sight_line = observer_height * (1.0 - fraction)
        apparent_terrain = elevation - _curvature_drop_m(sample_distance)
        if apparent_terrain > sight_line + TERRAIN_CLEARANCE_M:
            detail.update(
                {
                    "state": NO,
                    "reason": "terrain_blocks_line_of_sight",
                    "blocked_at_m": round(sample_distance, 1),
                    "blocking_elevation_m": round(elevation, 1),
                    "null_elevation_samples": null_samples,
                }
            )
            if use_cache:
                _cache_set(
                    lat, lon, cache_type, detail, timeout=GEOMETRY_CACHE_TIMEOUT_S
                )
            return detail

    # Bare-earth terrain allows it. Trees and buildings are invisible to the
    # model, so this is "likely", never "yes".
    detail.update(
        {
            "state": LIKELY,
            "reason": "clear_line_of_sight",
            "null_elevation_samples": null_samples,
        }
    )
    if use_cache:
        _cache_set(lat, lon, cache_type, detail, timeout=GEOMETRY_CACHE_TIMEOUT_S)
    return detail


# --- text verdict -----------------------------------------------------------


def _matched_keywords(text: str) -> Tuple[List[str], List[str]]:
    lowered = (text or "").lower()
    view_hits = [word for word in VIEW_KEYWORDS if word in lowered]
    proximity_hits = [word for word in PROXIMITY_KEYWORDS if word in lowered]
    return view_hits, proximity_hits


def classify_text_with_ai(text: str) -> Dict[str, Any]:
    """Ask the subscription bridge whether the text claims a view.

    The bridge runs on the owner's Claude subscription, not an API key, and is
    only ever called for text that already mentions the sea.
    """
    from services import subscription_transport

    try:
        result = subscription_transport.complete(
            _AI_PROMPT.format(text=(text or "")[:2000]),
            provider="claude",
            system=_AI_SYSTEM,
        )
    except Exception as exc:
        logger.warning("Sea-view AI classification unavailable: %s", exc)
        return {"claim": TEXT_UNAVAILABLE, "error": str(exc)}

    raw = str(result.get("text") or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Sea-view AI returned unparseable output: %r", raw[:200])
        return {"claim": TEXT_UNAVAILABLE, "error": "unparseable AI response"}

    claim = str(parsed.get("claim") or "").strip().lower()
    if claim not in (TEXT_VIEW, TEXT_PROXIMITY, TEXT_NONE):
        return {"claim": TEXT_UNAVAILABLE, "error": f"unexpected claim {claim!r}"}
    return {"claim": claim, "quote": str(parsed.get("quote") or "")[:200]}


def evaluate_text(
    title: Optional[str], description: Optional[str], use_ai: bool = True
) -> Dict[str, Any]:
    """What the listing text itself claims, with AI used only to disambiguate.

    Keywords decide *whether the sea is mentioned*; the AI decides *what the
    mention means*. Without the bridge only the unambiguous phrases count, and
    the fallback says so.
    """
    text = f"{description or ''} {title or ''}".strip()
    view_hits, proximity_hits = _matched_keywords(text)
    detail: Dict[str, Any] = {
        "view_keywords": view_hits,
        "proximity_keywords": proximity_hits,
    }

    if not view_hits and not proximity_hits:
        detail.update({"claim": TEXT_NONE, "source": "keywords"})
        return detail

    if use_ai:
        ai = classify_text_with_ai(text)
        if ai.get("claim") != TEXT_UNAVAILABLE:
            detail.update(
                {"claim": ai["claim"], "source": "ai", "quote": ai.get("quote", "")}
            )
            return detail
        detail["ai_error"] = ai.get("error")

    detail.update(
        {
            "claim": TEXT_VIEW if view_hits else TEXT_PROXIMITY,
            "source": "keywords_only",
        }
    )
    return detail


# --- combination ------------------------------------------------------------


def combine(
    text_detail: Dict[str, Any], geometry_detail: Dict[str, Any]
) -> Tuple[str, str, str]:
    """Fold the two signals into (state, source, reason).

    Text claims a view AND terrain allows it        -> yes
    Text claims a view, terrain says otherwise      -> likely, conflict recorded
    Only terrain allows it                          -> likely (bare-earth model)
    Terrain computed and negative, text silent      -> no
    Nothing computable                              -> unknown
    """
    claim = text_detail.get("claim", TEXT_NONE)
    geometry = geometry_detail.get("state", UNKNOWN)

    if claim == TEXT_VIEW:
        if geometry == LIKELY:
            return YES, "text+geometry", "listing claims a view and terrain allows it"
        if geometry == NO:
            return (
                LIKELY,
                "text",
                f"listing claims a view, terrain disagrees ({geometry_detail.get('reason')})",
            )
        return LIKELY, "text", "listing claims a view, terrain not computable"

    if geometry == LIKELY:
        return (
            LIKELY,
            "geometry",
            "terrain allows a view, listing text does not claim one",
        )
    if geometry == NO:
        return NO, "geometry", str(geometry_detail.get("reason") or "geometry negative")
    return UNKNOWN, "none", str(geometry_detail.get("reason") or "not computable")


# --- reading and writing the stored verdict ---------------------------------


def normalize_state(value: Any) -> str:
    """Coerce whatever is stored into one of VALID_STATES.

    The legacy `Land` boolean is *not* mapped onto yes/no: `true` came from the
    same weak keyword pass this module is replacing, so it becomes `likely`, and
    `false` carries no evidence at all, so it becomes `unknown`.
    """
    if isinstance(value, str) and value in VALID_STATES:
        return value
    if value is True:
        return LIKELY
    return UNKNOWN


def read_verdict(prop) -> Dict[str, Any]:
    """The effective verdict for a property, for templates and the API."""
    environment = prop.environment if isinstance(prop.environment, dict) else {}
    detail = environment.get("sea_view_detail")
    detail = detail if isinstance(detail, dict) else {}
    return {
        "state": normalize_state(environment.get("sea_view")),
        "source": detail.get("source") or ("legacy" if environment else "none"),
        "reason": detail.get("reason") or "",
        "detail": detail,
    }


def evaluate_property(
    prop,
    *,
    use_ai: bool = True,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Compute the full verdict for one property. Does not write anything."""
    manual = None
    environment = prop.environment if isinstance(prop.environment, dict) else {}
    stored_detail = environment.get("sea_view_detail")
    if isinstance(stored_detail, dict) and stored_detail.get("source") == "manual":
        manual = normalize_state(environment.get("sea_view"))

    text_detail = evaluate_text(prop.title, prop.description, use_ai=use_ai)

    if prop.location_lat is None or prop.location_lon is None:
        geometry_detail = {"state": UNKNOWN, "reason": "no_coordinates"}
    else:
        geometry_detail = evaluate_geometry(
            float(prop.location_lat),
            float(prop.location_lon),
            coordinate_accuracy=prop.location_accuracy,
            session=session,
            use_cache=use_cache,
        )

    state, source, reason = combine(text_detail, geometry_detail)

    if manual is not None:
        # An owner who looked at the listing outranks both models, but the
        # computed opinion is kept so a disagreement stays visible.
        return {
            "sea_view": manual,
            "sea_view_detail": {
                "source": "manual",
                "reason": "set by hand",
                "computed_state": state,
                "computed_source": source,
                "text": text_detail,
                "geometry": geometry_detail,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    return {
        "sea_view": state,
        "sea_view_detail": {
            "source": source,
            "reason": reason,
            "text": text_detail,
            "geometry": geometry_detail,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def apply_to_property(prop, verdict: Dict[str, Any], commit: bool = True) -> None:
    """Persist a verdict into `Property.enrichment["environment"]`."""
    from app import db

    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    environment = enrichment.get("environment")
    environment = dict(environment) if isinstance(environment, dict) else {}
    environment.update(verdict)
    enrichment = dict(enrichment)
    enrichment["environment"] = environment
    prop.enrichment = enrichment
    if commit:
        db.session.commit()


def calculate_for_property(
    prop,
    *,
    use_ai: bool = True,
    session: Optional[requests.Session] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Evaluate one property and store the result. Returns the verdict."""
    verdict = evaluate_property(prop, use_ai=use_ai, session=session)
    apply_to_property(prop, verdict, commit=commit)
    return verdict
