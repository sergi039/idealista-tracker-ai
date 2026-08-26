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
``no``       computed and negative -- no coastline in range, or no sea surface
             visible from the point. The second one takes a fan of rays past
             the shore, not one ray to it: the nearest water's edge is the
             first thing rising ground hides, so answering on that alone made
             120 of the 124 computable production rows negative (see the
             comment on SEA_PROBE_RAYS).
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
    OVERPASS_BREAKERS,
    OVERPASS_GATE,
    LookupBudgetExceeded,
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
#
# **Keep only what a silence cannot contradict.** A stored verdict survives a
# re-run on exactly one condition: the subject is unchanged *and* this run
# learned nothing, because a source did not answer. That is what this set is,
# and the origin check in `repaired_with_stored_geometry` is the other half of
# it -- a refusal on a row that has since moved reuses nothing.
#
# The three reasons outside it are outside it for two different arguments, and
# the difference matters if anyone ever tries to relax one of them:
#
# `no_coordinates` and `approximate_coordinates` are the **subject** changing.
# The row no longer has the place the stored verdict is a claim about, so the
# claim is about somewhere else. Measured on production 2026-08-26: a full
# backfill moved six rows from a measured `no` to `unknown`, and the pre-run
# snapshot says five of them (128, 132, 170, 174, 175) were measured at
# 40.463667,-3.74922 -- the centre of Spain, which is what geocoding a #298
# truncated title fragment ("Finca Offers For Sale This Buildi") returns. Row
# 132 is Carreno, on the coast, and its stored `no_coastline_in_range` was a
# false negative that survived only because it was measured 400 km inland.
# Preserving those would have preserved a claim about Madrid.
#
# `approximate_coordinates` is not the milder half of that and must not be
# treated as one. A verdict measured at a `precise` coordinate that has since
# decayed to `approximate` sits on the *same point*, so an origin check waves
# it through -- and it is still wrong to keep, for #196's reason:
# `sea_distance_service._last_known_good` refuses the identical case in the
# identical words, "same point, different claim". The stored verdict was a
# claim about the parcel; the row has just lost the right to one.
#
# `no_elevation_at_property` is neither: the subject did not move and the
# source did not go quiet -- EU-DEM answered, and its answer was that it has no
# ground at this point. A computed answer is not a silence, so it lands. It has
# never fired on production (0 rows, 2026-08-26), and it is named here so that
# its absence from this set reads as a decision rather than an oversight.
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

# --- the sea probe ----------------------------------------------------------
#
# The nearest coastline node is the hardest target on the whole coast, and for
# a long time it was the only one asked about. It is the water's edge closest
# to the house -- which from anywhere above sea level is the *first* thing the
# intervening ground hides, because the near brow, the dune or the neighbour's
# hillock occludes the shore under it long before it occludes the open water
# beyond. Measured on production 2026-08-26: of the 124 rows where a profile
# actually ran, 120 answered `terrain_blocks_line_of_sight` and 4 answered
# `clear_line_of_sight`. A model that says "no sea view" for 97% of the coastal
# rows it can compute is not answering the question it is asked.
#
# Property 1282 (Seiruga, Malpica) is the shape of it: eye at 50.2 m, the
# water's edge 394.7 m out at bearing 21 deg, a brow of 41.0 m at 91.1 m.
# The arithmetic is right -- the shore under the house really is hidden -- and
# the bay and the Sisargas are in the listing's own photographs. Measured by
# hand over 21 bearings on EU-DEM, open water is visible from ~600 m out across
# roughly a 60-degree sector.
#
# So the fan asks the other question: **is any sea surface visible**, rather
# than **is that one node visible**. It runs only where the shoreline ray was
# blocked, so nothing that already answered is re-measured, and it fits in one
# more OpenTopoData request because that endpoint batches locations: 1 observer
# + 5 rays x 19 samples = 96, inside the 100-location cap.
SEA_PROBE_RAYS = 5
SEA_PROBE_SAMPLES_PER_RAY = 19

# How far out to look. The sea is at least `distance` away, so the ray has to
# reach past it; twice that is the sight line grazing a brow halfway up the eye
# height, which is the geometry this exists for. The 3 km floor covers the case
# above, where the water is close and the obstruction is closer still.
SEA_PROBE_MIN_DISTANCE_M = 3_000
SEA_PROBE_DISTANCE_FACTOR = 2.0

# Coastline nodes a metre apart are the same ray. Collapsing them to whole
# degrees keeps the bearing search over a few hundred candidates instead of the
# hundred thousand a cell query can hold, and one degree at 3 km is 52 m --
# finer than the model the rays are sampled against.
SEA_PROBE_BEARING_BUCKET_DEG = 1.0

# EU-DEM has no value over open water, which is what makes a `None` sample the
# water detector -- the same reading `null_elevation_samples` already records.
# `None` is also what a hole in the model looks like, so a single one is not
# enough: a run of two consecutive nulls is several hundred metres of
# continuous no-data, which is a sea and not a pinhole.
MIN_WATER_RUN_SAMPLES = 2

COASTLINE_CACHE_TIMEOUT_S = 60 * 60 * 24 * 30
GEOMETRY_CACHE_TIMEOUT_S = 60 * 60 * 24 * 7

# `HTTP_USER_AGENT` lives in utils.http, imported above: overpass-api.de
# refuses the default `python-requests` User-Agent, and the OSM amenity call in
# services/enrichment_service.py needs the same token for the same reason.

# --- text signals -----------------------------------------------------------

# An unambiguous claim of a *view*. Used directly when the AI bridge is down.
#
# This is a list of literal substrings, so it recognises a phrasing and not an
# idea: every entry is a form somebody actually wrote. The two at the end were
# added after they were measured missing on production rows -- listing 111186983
# offers "Building plot with sea and city views" and the Villahormes plot
# "vistas abiertas y despejadas, con presencia del mar en el horizonte", and
# both scored `unknown` because "sea view" and "vistas al mar" require an
# adjacency neither sentence has. Nothing read them as "no sea": an absent
# keyword leaves the text saying nothing, which is why the miss was quiet.
#
# Both are cut back to the part that carries the claim, so they generalise as
# far as a substring can -- "sea and city view" also matches the plural, and
# "mar en el horizonte" matches "se ve el mar en el horizonte" as well as the
# sentence above. Neither is cut back further: "and city views" would match a
# park, and "el horizonte" says nothing about the sea.
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
    "sea and city view",
    "mar en el horizonte",
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


def _destination(
    lat: float, lon: float, heading_deg: float, distance_m: float
) -> Tuple[float, float]:
    """The point `distance_m` away on a compass bearing.

    Equirectangular, for the same reason `_interpolate` is linear: over the
    <=12 km these rays span the error against a great-circle projection is a
    few metres, and the model being sampled has 25 m cells.
    """
    theta = math.radians(heading_deg)
    d_lat = (distance_m * math.cos(theta)) / EARTH_RADIUS_M
    d_lon = (distance_m * math.sin(theta)) / (
        EARTH_RADIUS_M * math.cos(math.radians(lat))
    )
    return (lat + math.degrees(d_lat), lon + math.degrees(d_lon))


def _bearing_gap_deg(a: float, b: float) -> float:
    """The smaller of the two arcs between two compass bearings."""
    gap = abs(a - b) % 360.0
    return min(gap, 360.0 - gap)


def _sight_slope(
    elevation_m: float, distance_m: float, observer_height_m: float
) -> float:
    """How high this sample reaches, as a slope from the eye.

    A sample blocks a target at distance `D` exactly when this slope exceeds
    `-observer_height_m / D`, which is the same test the shoreline profile
    applies against its endpoint: `elev - drop > H*(1 - d/D) + clearance`
    rearranges into it once both sides are divided by `d`. Written as a slope
    so a whole ray can be walked once, keeping a running maximum, instead of
    comparing every pair of samples.
    """
    apparent = elevation_m - _curvature_drop_m(distance_m)
    return (apparent - observer_height_m - TERRAIN_CLEARANCE_M) / distance_m


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


class SeaViewBudgetExceeded(SeaViewSourceError):
    """The caller's lookup budget ran out before Overpass could answer (#434).

    A `SeaViewSourceError` so every existing handler still degrades the
    verdict to `unknown` -- but its own type, because the breaker must not
    count it. A spent clock says nothing about whether the instance would
    have answered, and recording it as a refusal would arm five minutes of
    silence against a healthy host on the strength of somebody else's slow
    run.
    """


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

    # This client dials one instance -- it never adopted #415's fallback list --
    # so a primary that is down costs its whole budget with no chance of
    # success. Until that is fixed the breaker is what bounds it: three
    # refusals and later calls answer immediately for five minutes, and the
    # registry is shared with the amenity client, so whichever of the two
    # learns the outage first spares the other from re-discovering it.
    #
    # A skip raises rather than returning `[]`, because this function's whole
    # contract is that an empty list means "Overpass answered and there is no
    # coastline in range". A refusal that read as an empty coastline is the
    # #98 defect this module was built around.
    breaker = OVERPASS_BREAKERS.for_url(Config.OSM_OVERPASS_URL)
    if breaker.should_skip():
        raise SeaViewSourceError(
            f"{OVERPASS_BREAKERS.host_of(Config.OSM_OVERPASS_URL)} refused the "
            f"last {OVERPASS_BREAKERS.threshold} attempts; not dialled"
        )
    try:
        points = _coastline_round_trip(cell_lat, cell_lon, session)
    except SeaViewBudgetExceeded:
        # Neither a refusal nor a success: this instance was never asked.
        raise
    except SeaViewSourceError:
        breaker.record_refusal("coastline")
        raise
    breaker.record_success()

    _cache_set(
        cell_lat, cell_lon, cache_type, points, timeout=COASTLINE_CACHE_TIMEOUT_S
    )
    return points


def _coastline_round_trip(
    cell_lat: float, cell_lon: float, session: Optional[requests.Session] = None
) -> List[Tuple[float, float]]:
    """The trip itself. Every refusal here is observed by the caller above."""
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
            # Connect and read split for the reason the amenity transport
            # gives; this one keeps its longer read budget, which a coastline
            # query over a 25 km box genuinely needs.
            timeout=(
                float(getattr(Config, "OSM_OVERPASS_CONNECT_TIMEOUT_S", 3.0)),
                120,
            ),
            # A `504` is worth all five attempts -- the instance is alive and
            # busy. Silence is not: this client has no fallback instance to
            # move to, so the only thing a second attempt buys is another
            # 120 s of the caller's clock (#434).
            silence_max_attempts=1,
            # Bounded by whatever budget the run that asked opened, if any.
            deadline=lookup_deadline(),
            # Streamed so the size ceiling is enforced as the body arrives,
            # not after it is already in memory.
            stream=True,
            logger=logger,
            # Shared with the amenity query, and it covers the retries too.
            gate=OVERPASS_GATE,
        )
    except LookupBudgetExceeded as exc:
        raise SeaViewBudgetExceeded(f"Overpass not dialled: {exc}") from exc
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
                float(getattr(Config, "OSM_OVERPASS_CONNECT_TIMEOUT_S", 3.0)),
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


def probe_distance_m(nearest_distance_m: float, decisive_distance_m: float) -> float:
    """How far out the fan looks, for a shore this far away."""
    return min(
        decisive_distance_m,
        max(SEA_PROBE_MIN_DISTANCE_M, nearest_distance_m * SEA_PROBE_DISTANCE_FACTOR),
    )


def probe_bearings(
    lat: float,
    lon: float,
    coastline: Sequence[Tuple[float, float]],
    radius_m: float,
    count: int,
) -> List[float]:
    """Bearings to aim the fan along, nearest coastline first, then spread out.

    The bearings come from the coastline itself rather than from a sector
    invented around the nearest node, because the sea is not reliably in one:
    a house on a headland has water at 20 deg and at 200 deg with land between
    them, and a fixed sector would look at one of them and call the other
    absent. Every ray therefore points at a mapped water edge -- it is then
    extended past it, since the edge is the thing that was already found to be
    hidden.

    Selection is farthest-point sampling on the circle, seeded with the nearest
    node: the first ray is the one the shoreline profile already used, and each
    ray after it is the one furthest in bearing from every ray chosen so far,
    ties going to the nearer water. Spread is the objective because the fan's
    whole failure mode is looking five times in one direction.
    """
    nearest_by_bucket: Dict[int, Tuple[float, float]] = {}
    for point in coastline:
        distance = haversine_m(lat, lon, point[0], point[1])
        if distance > radius_m or distance <= 0:
            continue
        heading = bearing_deg(lat, lon, point[0], point[1])
        bucket = int(heading // SEA_PROBE_BEARING_BUCKET_DEG)
        current = nearest_by_bucket.get(bucket)
        if current is None or distance < current[0]:
            nearest_by_bucket[bucket] = (distance, heading)

    candidates = sorted(nearest_by_bucket.values())
    if not candidates:
        return []

    chosen_idx = [0]
    while len(chosen_idx) < count and len(chosen_idx) < len(candidates):
        best_idx = None
        best_key = None
        for index, (distance, heading) in enumerate(candidates):
            if index in chosen_idx:
                continue
            spread = min(
                _bearing_gap_deg(heading, candidates[picked][1])
                for picked in chosen_idx
            )
            key = (spread, -distance)
            if best_key is None or key > best_key:
                best_key, best_idx = key, index
        if best_idx is None:
            break
        chosen_idx.append(best_idx)

    return [round(candidates[index][1], 1) for index in chosen_idx]


def _probe_plan(ray_count: int) -> Tuple[int, int]:
    """How many rays and how many samples each, inside one elevation request.

    Derived from the endpoint's own cap rather than asserted against it: a
    deployment that lowers `SEA_VIEW_ELEVATION_MAX_LOCATIONS` gets a smaller
    fan, not a `ValueError` out of `fetch_elevations` on every blocked row.
    That promise was not kept by the first version, which derived only
    `per_ray` from the cap and floored it at 1: below a cap of six it still
    asked for `1 + 5 x 1` locations, `fetch_elevations` raised on every blocked
    row, and the fan silently never ran anywhere. The test that was supposed to
    pin this used a cap of 26, which the arithmetic happens to handle -- it
    stepped around the defect instead of at it.

    **Rays are given up before samples.** A ray with fewer than
    `MIN_WATER_RUN_SAMPLES` samples cannot form a water run at all, so it can
    only ever answer "no water", which is a wrong answer rather than a coarse
    one. When even one ray cannot be afforded that, this returns `(0, 0)` and
    the caller refuses: a fan that cannot answer must say so, not answer `no`.
    """
    cap = int(getattr(Config, "SEA_VIEW_ELEVATION_MAX_LOCATIONS", 100))
    # One slot is the observer: it is re-sampled rather than carried over so
    # the request is self-describing, and it costs one location in a hundred.
    budget = cap - 1
    widest = min(SEA_PROBE_RAYS, ray_count, budget)
    for rays in range(max(1, widest), 0, -1):
        per_ray = min(SEA_PROBE_SAMPLES_PER_RAY, budget // rays)
        if per_ray >= MIN_WATER_RUN_SAMPLES:
            return rays, per_ray
    return 0, 0


def probe_fractions(count: int) -> List[float]:
    """Where along a ray to sample: dense near the eye, coarse far away.

    Quadratic rather than even, because the two things a ray has to do want
    opposite spacing. Near the observer it has to *find the brow* -- 19 evenly
    spaced samples over 3 km start at 158 m and would step straight over the
    41 m hillock at 91 m that this whole feature exists because of. Far away it
    only has to *find water*, and the sea is not a feature you can miss between
    samples. Squaring the fraction puts the first four samples inside 135 m and
    still reaches the end, which is also roughly uniform in *angle* as seen
    from the eye -- the resolution that actually decides a sight line.

    The far field stays coarse, and that is the honest cost: a narrow ridge at
    2.5 km can fall between samples. It bounds nothing, because a fan that sees
    water is only ever allowed to say `likely`, which already means "bare earth
    does not rule it out".
    """
    return [(index / count) ** 2 for index in range(1, count + 1)]


def _first_visible_water_m(
    distances: Sequence[float],
    elevations: Sequence[Optional[float]],
    observer_height_m: float,
    min_water_distance_m: float = 0.0,
) -> Optional[float]:
    """Distance to the nearest visible open water along one ray, or None.

    Walks the ray keeping the highest slope seen so far, so a sample is visible
    when nothing closer to the eye reaches over the line to it. A `None`
    elevation is open water at sea level; it is also what a hole in the model
    looks like, so only a sample inside a run of `MIN_WATER_RUN_SAMPLES`
    consecutive nulls counts as sea.

    Being in the run and being *visible* are separate questions, and collapsing
    them is wrong in exactly the case this whole feature is about. Water gets
    easier to see the further out it is -- the line to sea level at `d` falls
    away as `-H/d`, which rises toward zero -- so a ray that leaves the land at
    a hidden shore and continues over open sea has an invisible first sample
    and visible ones behind it. Requiring the run to *start* visible reported
    the whole ray as blocked; requiring only membership reports the nearest
    water the eye actually reaches.

    `min_water_distance_m` is what keeps a *reservoir* from being reported as
    the Atlantic. EU-DEM carries no value over water generally, not over the
    sea specifically, so a lake, a quarry pond, a wide river or a coastal
    lagoon reads exactly like open sea -- and because the ray is walked
    near-to-far and returns on the first qualifying run, a nearer inland gap
    masked the real, further answer about the sea, which might still be
    blocked. The bound is the distance to the nearest mapped coastline node:
    **no sea is nearer than the nearest sea**, so a null run inside it is some
    other water or a hole in the model. It is exact rather than tuned, it is
    already computed for the shoreline profile, and it is free. Measured
    against the eleven production rows the fan flipped on 2026-08-26, every one
    of them found its water beyond that distance, so the guard costs nothing
    real and closes the hole. What it does not close, and cannot from
    elevation alone: an inland body lying *beyond* the coastline distance on a
    seaward bearing. The rays are aimed at mapped coastline nodes, which is
    what makes that narrow, and the verdict it can reach is `likely`.
    """
    # Which samples sit in a long enough run of nulls. Independent of
    # visibility, so it is settled first and the sight line is walked once.
    corroborated = [False] * len(elevations)
    run_start: Optional[int] = None
    # The trailing 0.0 closes a run that reaches the end of the ray.
    for index, elevation in enumerate(list(elevations) + [0.0]):
        if elevation is None:
            if run_start is None:
                run_start = index
            continue
        if run_start is not None and index - run_start >= MIN_WATER_RUN_SAMPLES:
            for member in range(run_start, index):
                corroborated[member] = True
        run_start = None

    max_slope = -math.inf
    for index, (distance, elevation) in enumerate(zip(distances, elevations)):
        if distance <= 0:
            continue
        if elevation is None:
            if (
                corroborated[index]
                and distance >= min_water_distance_m
                and max_slope <= -observer_height_m / distance
            ):
                return distance
            # Sea level still occludes, and by less than any land would.
            elevation = 0.0
        max_slope = max(max_slope, _sight_slope(elevation, distance, observer_height_m))

    return None


def probe_sea_visibility(
    lat: float,
    lon: float,
    coastline: Sequence[Tuple[float, float]],
    observer_height_m: float,
    nearest_distance_m: float,
    decisive_distance_m: float,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Is any sea surface visible from this point, past the near ground?

    A different question from the one the shoreline profile answers, and the
    one a buyer is actually asking. Raises `SeaViewSourceError` if the
    elevation model refuses: a fan that did not run is not a fan that found
    nothing.
    """
    reach = probe_distance_m(nearest_distance_m, decisive_distance_m)
    headings = probe_bearings(lat, lon, coastline, reach, SEA_PROBE_RAYS)
    result: Dict[str, Any] = {
        "probe_distance_m": round(reach, 1),
        "bearings_deg": headings,
        "visible": False,
    }
    if not headings:
        # Unreachable while a coastline node inside `decisive_distance` is what
        # got us here -- `probe_distance_m` never returns less than that -- but
        # a fan with nothing to aim at is "found no water", never a crash.
        result["reason"] = "no_bearings_in_range"
        return result

    rays, per_ray = _probe_plan(len(headings))
    if not rays:
        # The location cap cannot afford a ray long enough to form a water run,
        # so this fan could only ever answer "no water" -- which is a wrong
        # answer, not a coarse one. Refuse, and let the caller record `unknown`.
        raise SeaViewSourceError(
            f"elevation location cap {Config.SEA_VIEW_ELEVATION_MAX_LOCATIONS} "
            "is too small for a sea probe"
        )
    headings = headings[:rays]
    result["bearings_deg"] = headings
    result["samples_per_ray"] = per_ray
    # No sea is nearer than the nearest sea: a null run inside this is some
    # other water, or a hole in the model. See `_first_visible_water_m`.
    result["min_water_distance_m"] = round(nearest_distance_m, 1)

    fractions = probe_fractions(per_ray)
    distances = [reach * fraction for fraction in fractions]
    points: List[Tuple[float, float]] = [(lat, lon)]
    for heading in headings:
        points.extend(_destination(lat, lon, heading, span) for span in distances)

    elevations = fetch_elevations(points, session=session)

    null_samples = 0
    for index, heading in enumerate(headings):
        start = 1 + index * per_ray
        ray = list(elevations[start : start + per_ray])
        null_samples += sum(1 for value in ray if value is None)
        seen_at = _first_visible_water_m(
            distances, ray, observer_height_m, nearest_distance_m
        )
        # The nearest water any ray can see, not the first ray that sees any:
        # what the card reports is how far off the visible sea is, and the fan
        # is walked in bearing order, not in distance order.
        if seen_at is not None and (
            not result["visible"] or seen_at < result["visible_at_m"]
        ):
            # The point this half of the verdict is *about*, recorded for the
            # reason #334 records the shoreline node: a distance and a bearing
            # are stored rounded, so casting the ray back out lands somewhere
            # else, and "what water is this?" is exactly the question an
            # estuary channel makes worth asking.
            water_lat, water_lon = _destination(lat, lon, heading, seen_at)
            result.update(
                {
                    "visible": True,
                    "visible_at_m": round(seen_at, 1),
                    "visible_bearing_deg": heading,
                    "visible_lat": round(water_lat, 6),
                    "visible_lon": round(water_lon, 6),
                }
            )
    result["null_elevation_samples"] = null_samples
    return result


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
    # `_v2` because the verdict itself changed: `_v1` entries were decided by
    # the shoreline ray alone, so a cached `no` is one of the false negatives
    # this version exists to stop serving. The #334 rule -- additive fields do
    # not earn a bump, because re-fetching a cell costs real pacing -- cuts the
    # other way when the answer moves.
    cache_type = f"sea_view_geometry_v2_{'approximate' if approximate else 'precise'}"
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
        #
        # This lands even over a stored `likely` or `no` measured at the same
        # point while it was still `precise`: that verdict was a claim about
        # the parcel and the row has just lost the right to one. See
        # SOURCE_REFUSAL_REASONS for why the subject changing is not a refusal.
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
            # The shore under the house is hidden. That is a fact, and it is
            # kept under the names 120 production rows already carry -- but it
            # is *not* the verdict, because it is the answer to the easiest
            # question to get wrong: the nearest water's edge is the first
            # thing any rise in the ground occludes.
            detail.update(
                {
                    "shoreline_visible": False,
                    "blocked_at_m": round(sample_distance, 1),
                    "blocking_elevation_m": round(elevation, 1),
                    "null_elevation_samples": null_samples,
                }
            )
            try:
                probe = probe_sea_visibility(
                    lat,
                    lon,
                    coastline,
                    observer_height,
                    distance,
                    decisive_distance,
                    session=session,
                )
            except (SeaViewSourceError, ValueError) as exc:
                # Half a measurement is not a measurement. The shoreline being
                # hidden was never enough for `no` on its own, so a fan that
                # could not run leaves this row unanswered rather than
                # promoting the weak half into a confident negative (#98).
                # `elevation_source_unavailable` is in SOURCE_REFUSAL_REASONS,
                # so `repaired_with_stored_geometry` keeps whatever an earlier
                # run measured; and nothing is cached.
                logger.warning("Sea probe unavailable for %.5f,%.5f: %s", lat, lon, exc)
                detail.update(
                    {"state": UNKNOWN, "reason": "elevation_source_unavailable"}
                )
                return detail

            detail["sea_probe"] = probe
            if probe.get("visible"):
                # Open water over the near ground. Bare earth again, so
                # `likely` and never `yes` -- the same ceiling the clear
                # shoreline gets, for the same reason.
                detail.update({"state": LIKELY, "reason": "sea_visible_beyond_terrain"})
            else:
                detail.update({"state": NO, "reason": "terrain_blocks_line_of_sight"})
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
            # Nothing hides the water's edge itself, so both facts agree here
            # and the fan has nothing to add. Recorded rather than left to be
            # inferred from the reason: it is the answer to a question the
            # blocked branch answers explicitly.
            "shoreline_visible": True,
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

    A geometry `likely` now covers two different sight lines and they are not
    worth the same to a buyer. One sees the water's edge under the house; the
    other sees open water *over* nearer ground that hides that edge, which is
    the ordinary hillside house and is what the shoreline-only verdict used to
    call "No sea view". Naming them alike would put the second back where it
    started, in a label nobody would look twice at.
    """
    state = normalize_state(verdict.get("state"))
    if state == LIKELY and verdict.get("source") == "geometry":
        detail = verdict.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        geometry = detail.get("geometry")
        geometry = geometry if isinstance(geometry, dict) else {}
        if geometry.get("reason") == "sea_visible_beyond_terrain":
            return "likely_geometry_over_terrain"
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
        # Not a refusal: the subject is gone, so any stored verdict is a claim
        # about a point nothing connects to this listing any more, and it is
        # overwritten. `SOURCE_REFUSAL_REASONS` carries the argument and the
        # production measurement behind it; `services/hazard_service.py` kept
        # the measurement here until 2026-08-26 and now does not.
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
