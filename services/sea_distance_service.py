"""How far a property is from the sea, as a scoring input.

Distance to the shoreline is what the hedonic literature on coastal premiums
actually measures -- straight-line metres, not travel time to a beach -- and it
could not come from Google here anyway: that billing is off, which is why every
travel target still returns empty (issue #98).

The coastline itself is *not* fetched here. `services/sea_view_service.py`
already owns that: one Overpass query per grid cell, cached for a month,
throttled, with the User-Agent and 504 handling its own comments explain. This
module is the thin part -- nearest-node distance, the four statuses, and the
record on the property -- so the repository keeps one coastline client instead
of two drifting apart.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

from sqlalchemy.orm.attributes import flag_modified

from config import Config
from models import Property
from services.sea_view_service import (
    APPROXIMATE_COORD_SLACK_M,
    MAX_SEA_DISTANCE_M,
    ORIGIN_TOLERANCE_DEG,
    SeaViewSourceError,
    fetch_coastline_points,
    haversine_m,
)

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

# The shared fetch queries a box around the whole grid cell, so the radius
# guaranteed for *any* point inside that cell is the query radius minus the
# cell's half-diagonal -- which is what the sea-view module already carries on
# top of its own maximum.
SEARCH_RADIUS_M = MAX_SEA_DISTANCE_M + APPROXIMATE_COORD_SLACK_M

# `ORIGIN_TOLERANCE_DEG` (about a metre) is imported above rather than
# redefined: the sea-view verdict applies the same "does this stored result
# still belong to these coordinates" rule to the same coastline, and two
# copies of that number would drift apart.


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


def _coordinate(value: Any, *, limit: float) -> Optional[float]:
    """A coordinate, or None when it is missing or off the globe.

    An out-of-range latitude would still find no coastline nearby and be filed
    as the measured fact "not near the sea"; it is bad input, not a location.
    """
    result = _safe_float(value)
    if result is None or abs(result) > limit:
        return None
    return result


def _nearest_point_distance_m(
    lat: float, lon: float, points: Sequence[Tuple[float, float]]
) -> Optional[float]:
    """Metres to the nearest coastline node.

    Node-to-node rather than point-to-segment: OSM coastline nodes sit tens of
    metres apart, so the overshoot stays well inside the accuracy this score
    needs, and it keeps a second geometry stack out of the repository.
    """
    best: Optional[float] = None
    for point in points:
        try:
            distance = haversine_m(lat, lon, float(point[0]), float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if best is None or distance < best:
            best = distance
    return best


class SeaDistanceService:
    """Straight-line distance to the OSM coastline, per property."""

    def measure(self, lat: float, lon: float) -> Dict[str, Any]:
        """Measure distance to the coastline for one point."""
        try:
            points = fetch_coastline_points(lat, lon)
        except SeaViewSourceError as exc:
            logger.warning("Coastline lookup unavailable for %s,%s: %s", lat, lon, exc)
            return {
                "status": STATUS_UNAVAILABLE,
                "distance_m": None,
                "searched_m": SEARCH_RADIUS_M,
                "source": SOURCE,
            }

        distance = _nearest_point_distance_m(lat, lon, points)
        if distance is None or distance > SEARCH_RADIUS_M:
            # Either the cell held no coastline, or the nearest one sits beyond
            # the radius the query guarantees. Both are measured facts rather
            # than failures: the property is simply not near the sea.
            return {
                "status": STATUS_NO_COASTLINE,
                "distance_m": None,
                "searched_m": SEARCH_RADIUS_M,
                "source": SOURCE,
            }

        return {
            "status": STATUS_OK,
            "distance_m": round(distance, 1),
            "searched_m": SEARCH_RADIUS_M,
            "source": SOURCE,
        }

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

        lat = _coordinate(prop.location_lat, limit=90.0)
        lon = _coordinate(prop.location_lon, limit=180.0)
        previous = self._stored_payload(prop)
        now = _now_iso()

        if lat is None or lon is None:
            # Geocoding is a paid Google call with billing off; do not reach for
            # it just to measure the sea.
            payload = {
                "status": STATUS_NO_COORDINATES,
                "distance_m": None,
                "searched_m": SEARCH_RADIUS_M,
                "source": SOURCE,
                "origin": None,
                "updated_at": now,
                "last_attempt_status": STATUS_NO_COORDINATES,
                "last_attempt_at": now,
            }
            self._store(prop, payload, commit=commit)
            return payload

        measurement = self.measure(lat, lon)

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
            "searched_m": previous.get("searched_m", SEARCH_RADIUS_M),
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
