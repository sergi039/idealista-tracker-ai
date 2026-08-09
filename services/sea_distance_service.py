"""Distance from a property to the sea, measured against OSM coastline geometry.

Why OSM and not Google: the Google APIs this project also talks to have billing
switched off, which is why every travel target comes back empty (issue #98). A
scoring criterion built on them would be born dead. Overpass is free, already a
primitive of this repository (`services/enrichment_service.py`), and coastline
geometry is exactly what the hedonic literature measures against -- straight-line
distance to the shoreline, not travel time to a beach.

The expensive part is the Overpass round trip, so the cache holds *geometry per
grid cell*, not a result per property: every listing in the same region reuses
one response, which turns a full backfill into a handful of requests.
"""

import json
import logging
import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from sqlalchemy.orm.attributes import flag_modified

from config import Config
from models import Property
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from utils.http import request_with_retries

logger = logging.getLogger(__name__)

# Measurement outcomes. The split exists because issue #98 showed what happens
# when an API refusal is indistinguishable from "there is nothing nearby".
STATUS_OK = "ok"
STATUS_NO_COASTLINE = "no_coastline_within_radius"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_COORDINATES = "no_coordinates"

# A measured status is one the data can be trusted for; it survives a later
# outage as last-known-good. The coastline does not move.
MEASURED_STATUSES = (STATUS_OK, STATUS_NO_COASTLINE)

SOURCE = "osm_coastline"

EARTH_RADIUS_M = 6371008.8

# Cache granularity. 0.5 degrees is roughly 55 km of latitude; the queried box is
# the cell grown by MAX_SEARCH_M on every side, so any point inside the cell has
# full coverage out to the search radius.
GRID_DEG = 0.5
MAX_SEARCH_M = 50_000

# Coastline geometry is stable, so a long TTL is honest rather than risky.
CACHE_TYPE = "sea_coastline_v1_50km"
CACHE_TTL_SECONDS = 60 * 60 * 24 * 30

# The Overpass body is untrusted input: bound it before parsing.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024

OVERPASS_TIMEOUT_SECONDS = 60

# Coordinates are compared to decide whether a stored measurement still belongs
# to this property; 1e-5 degrees is about a metre.
ORIGIN_TOLERANCE_DEG = 1e-5


class _CoastlineUnavailable(Exception):
    """Overpass did not return usable geometry."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def grid_cell_center(lat: float, lon: float) -> Tuple[float, float]:
    """Center of the GRID_DEG cell holding the point, rounded for a stable key."""
    cell_lat = math.floor(lat / GRID_DEG) * GRID_DEG + GRID_DEG / 2
    cell_lon = math.floor(lon / GRID_DEG) * GRID_DEG + GRID_DEG / 2
    return round(cell_lat, 6), round(cell_lon, 6)


def _cell_bbox(cell_lat: float, cell_lon: float) -> Tuple[float, float, float, float]:
    """(south, west, north, east) for the cell grown by the search radius."""
    lat_margin = GRID_DEG / 2 + math.degrees(MAX_SEARCH_M / EARTH_RADIUS_M)

    # Longitude degrees shrink towards the poles; use the widest edge of the cell
    # so the margin holds across the whole box.
    widest_lat = max(abs(cell_lat) - GRID_DEG / 2, 0.0)
    cos_lat = max(math.cos(math.radians(min(widest_lat, 89.0))), 1e-6)
    lon_margin = GRID_DEG / 2 + math.degrees(MAX_SEARCH_M / (EARTH_RADIUS_M * cos_lat))

    south = max(cell_lat - lat_margin, -90.0)
    north = min(cell_lat + lat_margin, 90.0)
    west = max(cell_lon - lon_margin, -180.0)
    east = min(cell_lon + lon_margin, 180.0)
    return south, west, north, east


def _point_to_segment_m(
    lat: float,
    lon: float,
    a_lat: float,
    a_lon: float,
    b_lat: float,
    b_lon: float,
) -> float:
    """Distance in metres from a point to a segment.

    Equirectangular projection around the query point: over the tens of
    kilometres this service looks at, the error is far below the accuracy the
    score needs, and it keeps the inner loop cheap enough to run over every
    coastline segment in the box.
    """
    cos_lat = math.cos(math.radians(lat))

    def project(plat: float, plon: float) -> Tuple[float, float]:
        x = math.radians(plon - lon) * cos_lat * EARTH_RADIUS_M
        y = math.radians(plat - lat) * EARTH_RADIUS_M
        return x, y

    ax, ay = project(a_lat, a_lon)
    bx, by = project(b_lat, b_lon)

    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(ax, ay)

    # Projection of the origin onto the segment, clamped to its ends.
    t = -(ax * dx + ay * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    nearest_x = ax + t * dx
    nearest_y = ay + t * dy
    return math.hypot(nearest_x, nearest_y)


def _min_distance_to_ways(
    lat: float, lon: float, ways: Sequence[Sequence[Sequence[float]]]
) -> Optional[float]:
    best: Optional[float] = None
    for way in ways:
        previous: Optional[Sequence[float]] = None
        for point in way:
            if previous is not None:
                distance = _point_to_segment_m(
                    lat, lon, previous[0], previous[1], point[0], point[1]
                )
                if best is None or distance < best:
                    best = distance
            previous = point
        # A degenerate one-node way still carries a position worth measuring.
        if previous is not None and len(way) == 1:
            distance = _point_to_segment_m(
                lat, lon, previous[0], previous[1], previous[0], previous[1]
            )
            if best is None or distance < best:
                best = distance
    return best


class SeaDistanceService:
    """Straight-line distance to the OSM coastline, cached per grid cell."""

    def __init__(self, overpass_url: Optional[str] = None):
        self.overpass_url = overpass_url or Config.OSM_OVERPASS_URL
        # Politeness towards a free public endpoint: pause between requests, not
        # before the first one, so a single measurement stays snappy.
        self.throttle_range_seconds = (1.0, 2.0)
        self._made_request = False

    # -- measurement ----------------------------------------------------

    def measure(self, lat: float, lon: float) -> Dict[str, Any]:
        """Measure distance to the coastline for one point."""
        cell = grid_cell_center(lat, lon)
        try:
            ways = self._coastline_for_cell(cell)
        except _CoastlineUnavailable as exc:
            logger.warning(
                "Coastline lookup unavailable for cell %s: %s",
                cell,
                exc,
            )
            return {
                "status": STATUS_UNAVAILABLE,
                "distance_m": None,
                "searched_m": MAX_SEARCH_M,
                "source": SOURCE,
            }

        distance = _min_distance_to_ways(lat, lon, ways)
        if distance is None or distance > MAX_SEARCH_M:
            # Either the box held no coastline at all, or the nearest one sits
            # beyond the radius this query guarantees coverage for. Both mean
            # "no sea nearby" -- a measured fact, not a failure.
            return {
                "status": STATUS_NO_COASTLINE,
                "distance_m": None,
                "searched_m": MAX_SEARCH_M,
                "source": SOURCE,
            }

        return {
            "status": STATUS_OK,
            "distance_m": round(distance, 1),
            "searched_m": MAX_SEARCH_M,
            "source": SOURCE,
        }

    def _coastline_for_cell(self, cell: Tuple[float, float]) -> List[List[List[float]]]:
        cell_lat, cell_lon = cell
        cached = get_cached_enrichment_data(cell_lat, cell_lon, CACHE_TYPE)
        if cached is not None:
            if isinstance(cached, list):
                return cached
            logger.warning("Discarding malformed coastline cache entry for %s", cell)

        ways = self._fetch_coastline(_cell_bbox(cell_lat, cell_lon))
        # Only a real Overpass answer is cached; a refusal must never be
        # remembered as "no coastline here".
        cache_enrichment_data(
            cell_lat, cell_lon, CACHE_TYPE, ways, timeout=CACHE_TTL_SECONDS
        )
        return ways

    def _fetch_coastline(
        self, bbox: Tuple[float, float, float, float]
    ) -> List[List[List[float]]]:
        south, west, north, east = bbox
        query = (
            f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS - 35}];"
            f'way["natural"="coastline"]({south},{west},{north},{east});'
            "out geom;"
        )

        self._throttle()

        try:
            response = request_with_retries(
                requests.post,
                self.overpass_url,
                data=query.encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=OVERPASS_TIMEOUT_SECONDS,
                stream=True,
                logger=logger,
            )
        except requests.RequestException as exc:
            raise _CoastlineUnavailable(f"request failed: {exc}") from exc

        try:
            if response.status_code != 200:
                raise _CoastlineUnavailable(f"HTTP {response.status_code}")
            body = self._read_bounded(response)
        finally:
            response.close()

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _CoastlineUnavailable(f"unparsable body: {exc}") from exc

        return self._ways_from_payload(payload)

    def _read_bounded(self, response: requests.Response) -> bytes:
        chunks: List[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=RESPONSE_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise _CoastlineUnavailable(
                    f"response exceeds {MAX_RESPONSE_BYTES} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _ways_from_payload(payload: Any) -> List[List[List[float]]]:
        if not isinstance(payload, dict):
            raise _CoastlineUnavailable("payload is not an object")

        # Overpass reports timeouts and runtime errors in-band, with HTTP 200.
        remark = payload.get("remark")
        if remark:
            raise _CoastlineUnavailable(f"overpass remark: {str(remark)[:200]}")

        elements = payload.get("elements")
        if not isinstance(elements, list):
            raise _CoastlineUnavailable("payload has no elements list")

        ways: List[List[List[float]]] = []
        for element in elements:
            if not isinstance(element, dict):
                raise _CoastlineUnavailable("element is not an object")
            geometry = element.get("geometry")
            if not isinstance(geometry, list):
                # A coastline way without geometry means the answer is partial;
                # treating it as "no coastline" would invent a fact.
                raise _CoastlineUnavailable("element without geometry")
            points: List[List[float]] = []
            for node in geometry:
                if not isinstance(node, dict):
                    raise _CoastlineUnavailable("geometry node is not an object")
                node_lat = _safe_float(node.get("lat"))
                node_lon = _safe_float(node.get("lon"))
                if node_lat is None or node_lon is None:
                    raise _CoastlineUnavailable("geometry node without coordinates")
                points.append([node_lat, node_lon])
            if points:
                ways.append(points)
        return ways

    def _throttle(self) -> None:
        if not self._made_request:
            self._made_request = True
            return
        low, high = self.throttle_range_seconds
        if high > 0:
            time.sleep(random.uniform(low, high))

    # -- persistence ----------------------------------------------------

    def update_property(
        self, prop: Property, *, commit: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Measure and store the sea distance for one property.

        Returns the stored payload, or None when the feature is off.
        """
        if not prop:
            return None
        if not getattr(Config, "SEA_DISTANCE_ENABLED", True):
            return None

        lat = _safe_float(prop.location_lat)
        lon = _safe_float(prop.location_lon)
        previous = self._stored_payload(prop)

        if lat is None or lon is None:
            # Geocoding is a paid Google call with billing off; do not reach for
            # it just to measure the sea.
            payload = {
                "status": STATUS_NO_COORDINATES,
                "distance_m": None,
                "searched_m": MAX_SEARCH_M,
                "source": SOURCE,
                "origin": None,
                "updated_at": _now_iso(),
                "last_attempt_status": STATUS_NO_COORDINATES,
                "last_attempt_at": _now_iso(),
            }
            self._store(prop, payload, commit=commit)
            return payload

        measurement = self.measure(lat, lon)
        now = _now_iso()

        if measurement["status"] == STATUS_UNAVAILABLE:
            kept = self._last_known_good(previous, lat, lon)
            if kept is not None:
                payload = {
                    **kept,
                    "last_attempt_status": STATUS_UNAVAILABLE,
                    "last_attempt_at": now,
                }
                self._store(prop, payload, commit=commit)
                return payload

        payload = {
            **measurement,
            "origin": {"lat": lat, "lon": lon},
            "updated_at": now,
            "last_attempt_status": measurement["status"],
            "last_attempt_at": now,
        }
        self._store(prop, payload, commit=commit)
        return payload

    @staticmethod
    def _stored_payload(prop: Property) -> Optional[Dict[str, Any]]:
        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        stored = enrichment.get("sea")
        return stored if isinstance(stored, dict) else None

    @staticmethod
    def _last_known_good(
        previous: Optional[Dict[str, Any]], lat: float, lon: float
    ) -> Optional[Dict[str, Any]]:
        """Reusable previous measurement, if it belongs to these coordinates."""
        if not previous:
            return None
        if previous.get("status") not in MEASURED_STATUSES:
            return None

        origin = previous.get("origin")
        if not isinstance(origin, dict):
            return None
        origin_lat = _safe_float(origin.get("lat"))
        origin_lon = _safe_float(origin.get("lon"))
        if origin_lat is None or origin_lon is None:
            return None
        # A property that moved has a distance measured from somewhere else.
        if abs(origin_lat - lat) > ORIGIN_TOLERANCE_DEG:
            return None
        if abs(origin_lon - lon) > ORIGIN_TOLERANCE_DEG:
            return None

        return {
            "status": previous.get("status"),
            "distance_m": previous.get("distance_m"),
            "searched_m": previous.get("searched_m", MAX_SEARCH_M),
            "source": previous.get("source", SOURCE),
            "origin": {"lat": origin_lat, "lon": origin_lon},
            "updated_at": previous.get("updated_at"),
        }

    @staticmethod
    def _store(prop: Property, payload: Dict[str, Any], *, commit: bool) -> None:
        # `enrichment` is a plain JSON column, not a MutableDict: mutating the
        # nested dict in place would not reach the UPDATE.
        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        enrichment["sea"] = payload
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

        if commit:
            from app import db

            db.session.commit()
