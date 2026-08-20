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
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from config import Config
from services.coordinate_quality import (
    APPROXIMATE_COORD_SLACK_M,
    is_precise,
)
from services.enrichment_origin import (
    ORIGIN_TOLERANCE_DEG as ORIGIN_TOLERANCE_DEG,  # re-exported: services/sea_distance_service.py
)
from services.enrichment_origin import origin_of, origins_agree
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from utils.http import (
    HTTP_USER_AGENT,
    OVERPASS_GATE,
    RateGate,
    lookup_deadline,
    request_with_retries,
)

logger = logging.getLogger(__name__)

# --- verdict states ---------------------------------------------------------

YES = "yes"
LIKELY = "likely"
NO = "no"
UNKNOWN = "unknown"

VALID_STATES = (YES, LIKELY, NO, UNKNOWN)

# A *geometry* verdict the data can be trusted for. It survives a later outage
# as last-known-good, exactly as a measured sea distance does: the coastline
# does not move and the terrain does not either. Geometry never reaches `yes`
# (bare-earth model), and `unknown` has nothing to lend.
MEASURED_GEOMETRY_STATES = (LIKELY, NO)

# The two `unknown` reasons that mean "a source refused", as opposed to the
# ones that mean "we looked and cannot say" (`approximate_coordinates`,
# `no_coordinates`, `no_elevation_at_property`). Only a refusal may be barred
# from overwriting an earlier verdict -- a computed unknown is an answer about
# this property and must land.
SOURCE_REFUSAL_REASONS = frozenset(
    {"coastline_source_unavailable", "elevation_source_unavailable"}
)

# Coordinates are compared to decide whether a stored verdict still belongs to
# this property; 1e-5 degrees is about a metre. Defined in
# services/enrichment_origin.py (issue #346) and re-exported here for
# backward compatibility -- services/sea_distance_service.py still imports it
# from this module, which applies the same rule to the same coastline.

# --- tuning constants -------------------------------------------------------

# Past this the sea stops being a view and starts being a horizon smudge; it is
# also where a 25 m terrain model stops being trustworthy.
MAX_SEA_DISTANCE_M = 12_000

# An "approximate" coordinate is a Nominatim centroid: two different plots in
# Siero share one point. Geometry on that point is meaningless *unless* the
# whole neighbourhood is far inland, so a negative verdict needs this much slack
# on top of MAX_SEA_DISTANCE_M before it is honest.
#
# The number itself moved to `services/coordinate_quality.py` and is imported
# above: sea distance and travel refuse an approximate origin on exactly this
# reasoning, and three copies of one tolerance is three chances to disagree
# about the same plot. Re-exported here because this module is where every
# reader has looked for it since #196.

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
# Pacing lives in `utils.http.OVERPASS_GATE`, shared with the amenity query in
# services/enrichment_service.py: both spend the same two per-IP slots, so a
# private interval here would only pace half of the traffic (#152).

# The reply is untrusted input, however well-known the endpoint: bound what is
# parsed and what is kept. A 25 km coastline query measured about 220 KB, so
# these ceilings are two orders of magnitude of headroom and still refuse a
# body that would fill the process. Neither truncates silently -- both raise.
MAX_COASTLINE_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_COASTLINE_POINTS = 200_000
COASTLINE_CHUNK_BYTES = 64 * 1024
# Wall-clock ceiling on reading one body. `timeout=120` bounds each socket
# read; this bounds their sum, so a server dripping a chunk every few seconds
# cannot hold the connection indefinitely. A real 25 km reply (~220 KB)
# arrives in seconds -- two minutes is generous, not tight.
MAX_COASTLINE_READ_S = 120.0

MIN_PROFILE_SAMPLES = 12
MAX_PROFILE_SAMPLES = 60
PROFILE_SAMPLE_SPACING_M = 150

COASTLINE_CACHE_TIMEOUT_S = 60 * 60 * 24 * 30
GEOMETRY_CACHE_TIMEOUT_S = 60 * 60 * 24 * 7

# `HTTP_USER_AGENT` lives in utils.http, imported above: overpass-api.de
# refuses the default `python-requests` User-Agent, and the OSM amenity call in
# services/enrichment_service.py needs the same token for the same reason.

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
            # Split into `(connect, read)` for the reason
            # `services/enrichment_service.py` gives; this one keeps the longer
            # read allowance, which a coastline query over a 25 km box needs.
            timeout=(
                float(getattr(Config, "OSM_OVERPASS_CONNECT_TIMEOUT_S", 5.0)),
                120,
            ),
            # A `504` is worth all five attempts -- the instance is alive and
            # busy. Silence is not: this query has no fallback instance to move
            # to, so the only thing a second attempt buys is another 120 s of
            # the caller's clock (#434).
            silence_max_attempts=1,
            # Bounded by whatever budget the run that asked opened, if any.
            # A refusal here already raises SeaViewSourceError and the verdict
            # already degrades to `unknown`, so a spent clock reads as "nobody
            # looked" rather than as "no sea".
            deadline=lookup_deadline(),
            # Streamed so the size ceiling is enforced as the body arrives,
            # not after it is already in memory.
            stream=True,
            logger=logger,
            # Shared with the amenity query, and it covers the retries too.
            gate=OVERPASS_GATE,
        )
    except requests.RequestException as exc:
        raise SeaViewSourceError(f"Overpass request failed: {exc}") from exc

    # One `finally` owns closing the streamed response for every path from
    # here on -- refusal, unreadable body, parse failure, success. Before this
    # the invariant was scattered: each failure path closed for itself, and
    # the parse-failure path relied on the body having been read to EOF for
    # urllib3 to return the connection (#196).
    #
    # Whatever goes wrong reading this body, the caller must see one exception
    # type. Anything else -- a TypeError from an unexpected node shape, a
    # RecursionError from a deeply nested body -- would escape
    # `evaluate_geometry`'s SeaViewSourceError handler and abort the row
    # instead of degrading it honestly to `unknown`. The decode is inside the
    # wrapper for exactly that reason.
    try:
        if response.status_code != 200:
            raise SeaViewSourceError(f"Overpass returned HTTP {response.status_code}")
        try:
            points = _parse_coastline_payload(json.loads(_read_bounded_body(response)))
        except SeaViewSourceError:
            raise
        except Exception as exc:
            raise SeaViewSourceError(
                f"Overpass returned an unreadable body: {exc}"
            ) from exc
    finally:
        try:
            response.close()
        except Exception:
            # Cleanup must not replace the verdict-bearing exception (or a
            # successful return) with a close() failure of its own.
            logger.debug("Closing the Overpass response failed", exc_info=True)

    _cache_set(
        cell_lat, cell_lon, cache_type, points, timeout=COASTLINE_CACHE_TIMEOUT_S
    )
    return points


def _read_bounded_body(response) -> bytes:
    """Read a response body, refusing one that is too large or too slow.

    Reading `response.content` would have already materialised the whole thing
    before any check could run, so the size limit has to be enforced while the
    body is still arriving. Both the advertised length and the bytes actually
    received are checked -- a header can lie.

    The wall-clock deadline exists because `timeout=120` on the request bounds
    each socket read, not the whole body: a server dripping one chunk every
    few seconds would pass every per-read timeout and still hold the
    connection for as long as it liked (#196). The deadline is checked between
    chunks, so the worst case is one per-read timeout past it.

    Closing the response on failure is the caller's job -- the one `finally`
    in `fetch_coastline_points` owns it for every path, rather than each
    failure closing for itself.
    """
    declared = response.headers.get("Content-Length") if response.headers else None
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            # An unparseable header decides nothing; the read below does.
            # Only the int() sits in the try -- a wider net would also have
            # caught the refusal itself if its class ever grew a ValueError
            # ancestry, silently demoting this check to the chunk path.
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > MAX_COASTLINE_RESPONSE_BYTES:
            raise SeaViewSourceError(
                f"Overpass announced {declared} bytes, over the "
                f"{MAX_COASTLINE_RESPONSE_BYTES}-byte ceiling"
            )

    deadline = time.monotonic() + MAX_COASTLINE_READ_S
    body = bytearray()
    for chunk in response.iter_content(chunk_size=COASTLINE_CHUNK_BYTES):
        if time.monotonic() > deadline:
            raise SeaViewSourceError(
                f"Overpass body still arriving after {MAX_COASTLINE_READ_S:g} s"
            )
        if not chunk:
            continue
        # Check before extending, not after: appending first would put the
        # oversized body in memory, which is the thing being prevented.
        if len(body) + len(chunk) > MAX_COASTLINE_RESPONSE_BYTES:
            raise SeaViewSourceError(
                f"Overpass sent more than {MAX_COASTLINE_RESPONSE_BYTES} bytes"
            )
        body.extend(chunk)
    return bytes(body)


def _parse_coastline_payload(payload: Any) -> List[Tuple[float, float]]:
    """Coastline coordinates from an Overpass reply, or a refusal.

    The return value decides whether a property may be told there is no sea
    near it, so every shape that is not a complete, well-formed answer raises.
    Exactly one shape returns an empty list: a reply with no `remark` whose
    `elements` array is itself empty.
    """
    if not isinstance(payload, dict):
        raise SeaViewSourceError(
            f"Overpass returned a {type(payload).__name__}, not an object"
        )

    # Overpass reports a query that ran out of time or memory as HTTP 200 with
    # a top-level `remark` and whatever it had managed to collect. Reading that
    # as "no coastline here" would write a truncated answer to the database as
    # a computed negative -- the #98 mistake with a different source. Presence
    # is the test, not truthiness: an empty remark is still a remark.
    if "remark" in payload:
        raise SeaViewSourceError(
            f"Overpass returned a partial result: {payload.get('remark')!r}"
        )

    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise SeaViewSourceError(
            f"Overpass returned no `elements` array (got {type(elements).__name__})"
        )

    # An answer that is present but unusable is not an answer. A coastline way
    # whose geometry is missing, empty, or unparsable would otherwise be dropped
    # silently, and the caller would read the shortened list -- or the empty one
    # -- as the measured fact "no coastline here".
    points: List[Tuple[float, float]] = []
    for element in elements:
        if not isinstance(element, dict):
            raise SeaViewSourceError("Overpass returned a malformed element")
        geometry = element.get("geometry")
        if not isinstance(geometry, list) or not geometry:
            raise SeaViewSourceError("Overpass returned a way without geometry")
        for node in geometry:
            if not isinstance(node, dict):
                raise SeaViewSourceError("Overpass returned a malformed geometry node")
            node_lat, node_lon = node.get("lat"), node.get("lon")
            if node_lat is None or node_lon is None:
                raise SeaViewSourceError("Overpass returned a node without coordinates")
            # `float(True)` is 1.0, which would sail through as a latitude.
            if isinstance(node_lat, bool) or isinstance(node_lon, bool):
                raise SeaViewSourceError("Overpass returned a boolean coordinate")
            try:
                lat_value, lon_value = float(node_lat), float(node_lon)
            except (TypeError, ValueError) as exc:
                raise SeaViewSourceError(
                    "Overpass returned an unparsable coordinate"
                ) from exc
            # NaN and infinity fail the range test too: any comparison with NaN
            # is False, so `not (-90.0 <= nan <= 90.0)` refuses it.
            if not (-90.0 <= lat_value <= 90.0) or not (-180.0 <= lon_value <= 180.0):
                raise SeaViewSourceError("Overpass returned a coordinate out of range")
            points.append((lat_value, lon_value))
            if len(points) > MAX_COASTLINE_POINTS:
                raise SeaViewSourceError(
                    f"Overpass returned more than {MAX_COASTLINE_POINTS} "
                    "coastline points"
                )

    return points


# OpenTopoData's public instance asks for one call a second. Its own gate
# rather than Overpass's: two different endpoints, two different budgets, and
# waiting for one because the other is busy would slow a run for no reason.
ELEVATION_GATE = RateGate(Config.SEA_VIEW_ELEVATION_MIN_INTERVAL_S, name="opentopodata")


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

    locations = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    http = session or requests
    try:
        response = request_with_retries(
            http.get,
            Config.SEA_VIEW_ELEVATION_URL,
            params={"locations": locations},
            headers={"User-Agent": HTTP_USER_AGENT},
            timeout=(
                float(getattr(Config, "OSM_OVERPASS_CONNECT_TIMEOUT_S", 5.0)),
                60,
            ),
            silence_max_attempts=1,
            deadline=lookup_deadline(),
            logger=logger,
            # OpenTopoData's public instance asks for one call a second, and
            # the retries count towards that as much as the first attempt.
            gate=ELEVATION_GATE,
        )
    except requests.RequestException as exc:
        raise SeaViewSourceError(f"Elevation request failed: {exc}") from exc

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
    approximate = not is_precise(coordinate_accuracy)
    # Accuracy belongs in the key: the same point answers differently depending
    # on whether it is a surveyed address or a municipality centroid, and two
    # rows can share coordinates to four decimals while disagreeing about that.
    # A shared key would serve one row's `likely` as the other's verdict.
    cache_type = f"sea_view_geometry_v1_{'approximate' if approximate else 'precise'}"
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
    # The point this verdict is *about*. A distance and a bearing describe it
    # only if you are willing to do spherical trigonometry, which is why
    # answering "what water did it actually look at?" for one property
    # (#334) took a day of OSM archaeology rather than one SQL query. Six
    # decimals is
    # about 0.1 m -- far finer than the coastline itself is mapped, and the
    # point is a stored OSM node, so rounding it further would move the
    # verdict's subject off the node it was measured to.
    #
    # Additive on purpose: no cache version bump. Entries written by an earlier
    # run simply lack these keys, and every reader below treats a missing
    # target as "not recorded" rather than as an error. Bumping the key would
    # force every cell through Overpass again to gain a field that changes no
    # verdict, and 5 s of pacing per cell is a real cost to spend on that.
    detail["target_lat"] = round(nearest[0], 6)
    detail["target_lon"] = round(nearest[1], 6)

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


def _origin_of(prop) -> Optional[Dict[str, float]]:
    """The coordinates a verdict was computed at, or None.

    Stored beside the verdict so a later run can tell "this property's own
    verdict" from one measured somewhere else -- the same provenance
    `Property.enrichment["sea"]` keeps for the distance. Delegates to
    `services/enrichment_origin.py` (issue #346), which `enrichment["pool"]`
    now shares -- same primitive, same behaviour, one definition.
    """
    return origin_of(prop)


def _geometry_refusal_reason(verdict: Dict[str, Any]) -> Optional[str]:
    """The refusal that stopped the geometry half, or None if it was computed.

    Read off the *geometry* detail, never off the top-level state. `combine()`
    turns a refused geometry into `likely` whenever the text claims a view --
    "listing claims a view, terrain not computable" -- so a rule keyed on the
    verdict being `unknown` misses the most common listing on this coast
    (review of PR #306; the first version of this guard had exactly that hole).
    """
    detail = verdict.get("sea_view_detail")
    detail = detail if isinstance(detail, dict) else {}
    geometry = detail.get("geometry")
    geometry = geometry if isinstance(geometry, dict) else {}
    reason = geometry.get("reason")
    reason = str(reason) if reason else None
    return reason if reason in SOURCE_REFUSAL_REASONS else None


def _origins_agree(
    stored_detail: Dict[str, Any], new_detail: Dict[str, Any]
) -> Optional[bool]:
    """True/False when both origins are readable, None when one is missing."""
    return origins_agree(stored_detail.get("origin"), new_detail.get("origin"))


def repaired_with_stored_geometry(
    environment: Dict[str, Any], verdict: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Rebuild a verdict whose geometry refused, or None to store as computed.

    A source that refused knows nothing about this property, so it must not
    erase what an earlier run measured -- the rule `SeaDistanceService`
    already applies to `enrichment["sea"]`, and the one #98 exists for. The
    dangerous path is the *second* look at a row, not the first: a new listing
    has no verdict to lose, but `utils/backfill_sea_view.py` re-evaluates
    every row on an ordinary run and the Enrich button does the same on every
    press, so one busy Overpass would rewrite verdicts that had been measured.

    What is preserved is the **measured geometry half**, not the whole
    verdict, and the verdict is then recombined with this run's *fresh* text
    signal. That matters three ways, all of them wrong under a whole-verdict
    rule:

    * a stored `yes` (text claimed a view, terrain allowed it) survives a
      refusal as `yes`, rather than decaying to the `likely` that
      `combine()` produces from text alone;
    * a text signal that legitimately changed -- the description was edited,
      or the AI now reads "primera línea" as proximity -- is still honoured;
    * the recorded reason stays the specific one the terrain gave
      ("terrain disagrees (no_coastline_in_range)") instead of being replaced
      by the generic "terrain not computable".

    Only a geometry that actually decided something is reusable (`likely` or
    `no`); a stored `unknown` geometry has nothing to lend. Reuse requires the
    stored verdict to belong to these coordinates: a stored origin that
    *disagrees* was measured somewhere else and is refused outright. A stored
    verdict from before origins were recorded cannot be checked -- it is
    reused, because erasing real verdicts on exactly the rows this rule
    protects is the worse failure, and the unverified provenance is stamped
    on the geometry (`origin_unverified`) rather than assumed away. That stamp
    is sticky: it describes where the terrain was measured, so it rides with
    the terrain through repeated outages and is cleared only by a successful
    re-measurement.

    The refusal is never silent: the rebuilt verdict carries what this run
    would have said and why it was not trusted, the way a kept QoL part does.
    """
    reason = _geometry_refusal_reason(verdict)
    if reason is None:
        return None

    stored_detail = environment.get("sea_view_detail")
    if not isinstance(stored_detail, dict):
        return None
    stored_geometry = stored_detail.get("geometry")
    if not isinstance(stored_geometry, dict):
        # Nothing measured to reuse. A legacy row carrying only the mirrored
        # `Land` boolean lands here, and recomputing it is right: that boolean
        # is the weak keyword pass this module replaced.
        return None
    if stored_geometry.get("state") not in MEASURED_GEOMETRY_STATES:
        return None

    new_detail = verdict.get("sea_view_detail")
    new_detail = new_detail if isinstance(new_detail, dict) else {}
    agree = _origins_agree(stored_detail, new_detail)
    if agree is False:
        return None

    text_detail = new_detail.get("text")
    text_detail = text_detail if isinstance(text_detail, dict) else {}

    reused_geometry = dict(stored_geometry)
    reused_geometry["reused_measurement"] = True
    # When the terrain is reused a second time -- two outages in a row -- the
    # stored detail's `computed_at` is the *previous repair*, not the
    # measurement. Keep the first one, or the age of the terrain creeps
    # forward every time a source refuses and ends up claiming to be current.
    reused_geometry["measured_at"] = stored_geometry.get(
        "measured_at"
    ) or stored_detail.get("computed_at")
    # Unverified provenance travels *with the terrain*, and is sticky.
    #
    # It is a fact about where this measurement was taken, not about today's
    # run, so it belongs on the geometry rather than beside it -- and deriving
    # it afresh each time silently lost it on the second outage: repair #1
    # stamps the verdict with today's `origin`, so repair #2 compares that
    # synthetic origin against the same coordinates, finds them equal, and
    # drops the flag while reusing the very same unverified terrain. The row
    # then reads as better-provenanced than it is, which is #98's shape.
    #
    # Writing today's `origin` is still right: it is the coordinate this
    # verdict describes, and it gives every later run real move-detection.
    # The flag is what must survive, and only a successful re-measurement
    # clears it -- a fresh `evaluate_geometry` result carries no flag at all.
    # `dict(stored_geometry)` already carries the flag forward once it lives on
    # the geometry; both `get`s below are deliberate anyway. The first states
    # the intent, so a later rewrite that rebuilds this dict field by field
    # cannot drop the stamp by accident. The second reads the flag's *previous*
    # home: the first version of this repair stamped the top-level detail, and
    # a row repaired by it would otherwise lose the label on its next outage --
    # the same defect, one shape further back.
    if (
        agree is None
        or stored_geometry.get("origin_unverified")
        or stored_detail.get("origin_unverified")
    ):
        reused_geometry["origin_unverified"] = True

    state, source, combined_reason = combine(text_detail, reused_geometry)
    now = datetime.now(timezone.utc).isoformat()
    repaired_detail: Dict[str, Any] = {
        "source": source,
        "reason": combined_reason,
        "text": text_detail,
        "geometry": reused_geometry,
        "origin": new_detail.get("origin") or stored_detail.get("origin"),
        "computed_at": now,
        # What this run would have concluded had its refusal been trusted,
        # and why it was not.
        "last_attempt_state": normalize_state(verdict.get("sea_view")),
        "last_attempt_reason": reason,
        "last_attempt_at": now,
    }
    return {"sea_view": state, "sea_view_detail": repaired_detail}


def _coerce_coordinate(value: Any, limit: float) -> Optional[float]:
    """A stored coordinate as a float, or None if it is not usable as one.

    `enrichment` is a JSON column that anything may have written, so a target
    read back out is untrusted input and must not become a map link pointing
    somewhere the verdict never looked. Only real numbers are accepted --
    `float("43,55")` style guessing at text is refused for the reason
    `utils/maps_urls._coord` refuses it, and a bool is refused because
    `float(True)` is 1.0, a perfectly plausible latitude.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = float(value)
    # NaN and infinity fail this too: every comparison against NaN is False.
    if not (-limit <= number <= limit):
        return None
    return number


def geometry_target(detail: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """The coastline point a verdict's geometry was measured to, or None.

    None covers three different rows and deliberately does not distinguish
    them, because a caller can only do one thing with any of them: a verdict
    from before `target_lat` was recorded (#334), one whose geometry found no
    coastline at all, and one whose coordinates did not survive the round trip
    through the JSON column.
    """
    geometry = detail.get("geometry")
    geometry = geometry if isinstance(geometry, dict) else {}
    lat = _coerce_coordinate(geometry.get("target_lat"), 90.0)
    lon = _coerce_coordinate(geometry.get("target_lon"), 180.0)
    if lat is None or lon is None:
        return None
    return {"lat": lat, "lon": lon}


def state_label_key(verdict: Dict[str, Any]) -> str:
    """How a verdict should be *named*, which is not always its state.

    `likely` covers two different claims and the page said "Sea view likely"
    for both. One of them is a listing that says so; the other is terrain that
    merely fails to rule it out -- and on this coast that is a weaker statement
    than the words suggest. Property 125 looks 4.2 km up the ría de
    Villaviciosa and reaches open sea through the mouth at Rodiles: measured,
    correct, and not what a buyer reads into "sea view likely" (#334).

    So a `likely` that rests on geometry alone is named for what was actually
    computed -- the terrain permits it -- and every other state keeps its own
    name. Returned as a key *suffix* so the `sea_view_state_` prefix and the
    wording stay in the presentation layer; what lives here is the
    distinction, in one place, for the three templates that draw this badge.
    """
    state = normalize_state(verdict.get("state"))
    if state == LIKELY and verdict.get("source") == "geometry":
        return "likely_geometry"
    return state


def read_verdict(prop) -> Dict[str, Any]:
    """The effective verdict for a property, for templates and the API."""
    environment = prop.environment if isinstance(prop.environment, dict) else {}
    detail = environment.get("sea_view_detail")
    detail = detail if isinstance(detail, dict) else {}
    geometry = detail.get("geometry")
    geometry = geometry if isinstance(geometry, dict) else {}
    return {
        "state": normalize_state(environment.get("sea_view")),
        "source": detail.get("source") or ("legacy" if environment else "none"),
        "reason": detail.get("reason") or "",
        "detail": detail,
        # Lifted out of `detail` so the page, the list and the CSV export read
        # the shape of the geometry half in one place rather than three.
        "target": geometry_target(detail),
        "distance_m": _coerce_coordinate(geometry.get("distance_m"), 1e9),
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
    origin = _origin_of(prop)

    if manual is not None:
        # An owner who looked at the listing outranks both models. The computed
        # opinion rides along in the return value so a caller can show the
        # disagreement -- `apply_to_property` will not store any of this, since
        # writing beside a hand-set verdict means another stale read-modify-
        # write of the whole JSON column.
        return {
            "sea_view": manual,
            "sea_view_detail": {
                "source": "manual",
                "reason": "set by hand",
                "computed_state": state,
                "computed_source": source,
                "text": text_detail,
                "geometry": geometry_detail,
                "origin": origin,
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
            # The coordinates this verdict describes, so a later refused run
            # can tell whether the stored terrain is still about this place
            # (`repaired_with_stored_geometry`).
            "origin": origin,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def apply_to_property(prop, verdict: Dict[str, Any], commit: bool = True) -> None:
    """Persist a verdict into `Property.enrichment["environment"]`.

    `enrichment` is one JSON column, so writing it is a read-modify-write over
    everything in it. Evaluation spends seconds on external calls, and the
    environment endpoint can land inside that window: without a lock a backfill
    quietly overwrites the verdict the owner just set by hand, taking the rest
    of the column with it.

    A plain `refresh()` is not enough -- it is a read, so another session can
    still commit between it and ours. With `commit=True` the row is read under
    `FOR UPDATE`, and this function owns the transaction outright: every exit
    ends it -- the write commits, the hand-set skip and the failure path roll
    back. It can afford to, because the session is required to hold nothing
    else (below), so a rollback discards only this function's own locked read.
    Nothing survives past the return: no row lock, no open transaction for a
    backfill to drag across the rows that follow (#196).

    With `commit=False` the caller owns the transaction, so no lock is taken:
    taking one on their behalf, for an interval this function cannot see the
    end of, is worse than the race it would close. That mode is for callers
    that already hold the row, and it makes no concurrency promise.

    A mapped property that this session does not hold -- another session's, or
    one that was expunged or detached -- is a caller error and raises when
    `commit=True`: writing to it and committing here would persist nothing at
    all, silently.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import NoInspectionAvailable

    from app import db

    try:
        state = sa_inspect(prop)
    except NoInspectionAvailable:
        state = None  # a plain object (a test double); nothing to lock

    if state is not None and commit:
        # `db.session` is a scoped-session proxy, so comparing it to
        # `state.session` -- a real Session -- is always unequal. Ask the proxy
        # whether it holds this object instead. Note this covers a *detached*
        # object too, whose `state.session` is simply None.
        if prop not in db.session:
            raise RuntimeError(
                "apply_to_property was asked to commit a property this session "
                "does not hold; the write would not be persisted"
            )
        # The locked `refresh()` below autoflushes, which would write out
        # anything else pending in the session -- including a stale
        # `enrichment` assigned before this call, which would erase a hand-set
        # verdict a moment before the locked read goes looking for it, and
        # including an unrelated half-built object whose IntegrityError would
        # surface mid-lock. And since every exit ends the transaction, a
        # caller's uncommitted work would be committed or discarded wholesale.
        # So this mode requires a clean session and says so instead of
        # flushing on the caller's behalf.
        #
        # In-place mutation of a JSON column is invisible to SQLAlchemy and
        # therefore *cannot* be detected here. This function owns the column
        # write; that is the contract, not an oversight.
        if db.session.new or db.session.dirty or db.session.deleted:
            raise RuntimeError(
                "apply_to_property(commit=True) needs a session with nothing "
                "pending: it ends the transaction on every exit, which would "
                "commit or discard whatever else is in flight"
            )

    try:
        if commit and state is not None:
            # Read the persisted row under a lock rather than trusting the
            # copy in memory. No savepoint: it once scoped the skip path's
            # rollback to "only what this function did", but with the
            # clean-session requirement above there is nothing else in the
            # transaction to protect, and rolling back to a savepoint left
            # the outer transaction open across every hand-set row of a
            # backfill (#196).
            db.session.refresh(prop, with_for_update=True)

        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        environment = enrichment.get("environment")
        environment = dict(environment) if isinstance(environment, dict) else {}

        stored_detail = environment.get("sea_view_detail")
        if isinstance(stored_detail, dict) and stored_detail.get("source") == "manual":
            # A hand-set verdict is only ever written by the environment
            # endpoint, which writes directly. Anything arriving here was
            # computed from a read of this row -- including a `manual` verdict
            # `evaluate_property` echoed back -- so it is at best as old as the
            # row and can only make it worse.
            logger.info(
                "Sea view for property %s is hand-set; leaving it alone",
                getattr(prop, "id", None),
            )
            if commit and state is not None:
                # Nothing to write, but the FOR UPDATE is still held. End the
                # transaction: the session holds nothing but this function's
                # own locked read, so rolling back discards nothing and
                # releases both the row lock and the transaction itself.
                db.session.rollback()
            return

        # A refused source must not erase a measurement an earlier run made
        # (#98's rule, as `SeaDistanceService` applies it to the distance).
        # This lives here, in the one writer, rather than at a call site:
        # `utils/backfill_sea_view.py` and every future caller would otherwise
        # reopen the same hole.
        repaired = repaired_with_stored_geometry(environment, verdict)
        if repaired is not None:
            logger.info(
                "Sea view for property %s: geometry unavailable (%s); reusing "
                "the terrain measured earlier, verdict %s",
                getattr(prop, "id", None),
                repaired["sea_view_detail"].get("last_attempt_reason"),
                repaired["sea_view"],
            )
            verdict = repaired

        environment.update(verdict)
        enrichment = dict(enrichment)
        enrichment["environment"] = environment
        prop.enrichment = enrichment

        if commit:
            db.session.commit()
    except Exception:
        if commit:
            # A failed commit leaves the transaction -- and the row lock -- open,
            # and every later row in a backfill loop would then fail on a
            # poisoned session. Put it back in a usable state before the caller
            # sees the error.
            db.session.rollback()
        raise


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
