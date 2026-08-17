"""How far a property is from the sea, as a scoring input.

Distance to the shoreline is what the hedonic literature on coastal premiums
actually measures -- straight-line metres, not travel time to a beach -- and it
could not come from Google here anyway: that billing is off, which is why every
travel target still returns empty (issue #98).

The coastline itself is *not* fetched here. `services/sea_view_service.py`
already owns that: one Overpass query per grid cell, cached for a month,
throttled, with the User-Agent and 504 handling its own comments explain. This
module is the thin part -- nearest-node distance, the statuses below, and the
record on the property -- so the repository keeps one coastline client instead
of two drifting apart.

What it will not do is answer for a property whose coordinate is not the
property: `location_accuracy` decides whether the measured distance may be
called this parcel's at all, on the same reasoning and the same slack
`sea_view_service` applies to a view verdict.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

from sqlalchemy.orm.attributes import flag_modified

from services.enrichment_write import check_writable, locked_write

from config import Config
from models import Property
from services.coordinate_quality import (
    APPROXIMATE_COORD_SLACK_M,
    coordinate_slack_m,
    distance_bounds_m,
    normalize_accuracy,
)
from services.sea_view_service import (
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
# The coastline was found, and it is not this property's distance to it: the
# row's coordinate is a locality centroid, so what was measured is the distance
# from a point the parcel is merely near. Kept apart from `unavailable` for the
# #98 reason -- the source answered perfectly, and re-running the lookup will
# never improve it. Only a re-geocode will.
STATUS_APPROXIMATE_ORIGIN = "approximate_origin"
# Not a measurement outcome: the row has no sea block at all. Named here with
# the rest so a reader finds every value `parcel_measurement` can return in one
# place.
STATUS_MISSING = "missing_sea_distance"

# A measured status is one the data can be trusted for; it survives a later
# outage as last-known-good. The coastline does not move -- and neither does
# the fact that a centroid cannot answer for the parcel, which is why the
# approximate verdict is kept over a later refusal too.
MEASURED_STATUSES = (STATUS_OK, STATUS_NO_COASTLINE, STATUS_APPROXIMATE_ORIGIN)

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

    def measure(
        self, lat: float, lon: float, accuracy: Optional[str]
    ) -> Dict[str, Any]:
        """Measure distance to the coastline for one point.

        `accuracy` is the row's `location_accuracy`, and it decides what the
        measurement is allowed to claim.

        **Required, with no default**, and that is the lesson of the defect
        this signature shipped with. #358 wrote it `accuracy: Optional[str] =
        None`, reasoning that a caller who does not say gets the honest reading
        of silence — a point that may be a centroid. But silence is not
        something a caller *says*; it is what an argument looks like when
        somebody forgets it. The `--dry-run` arm of
        `utils/recalc_sea_distance.py` forgot it, and every row it previewed
        came back `approximate_origin`, precise ones included — in the report
        an operator reads before authorising a rewrite of every located row. A
        default that is safe for the data and wrong for the report is still
        wrong; a required argument cannot be forgotten quietly. Every caller
        has the value to hand: there are two, and both read it off the row.

        `searched_m` is what the answer is guaranteed for *around the parcel*,
        so an approximate origin shrinks it by the slack -- the radius was
        searched around the centroid, and the parcel may sit that far outside
        it. `services/property_scoring_service.py` reads exactly that field to
        decide whether a profile's horizon reaches past the measurement.
        """
        accuracy = normalize_accuracy(accuracy)
        slack = coordinate_slack_m(accuracy)
        guaranteed_m = SEARCH_RADIUS_M - slack
        base = {
            "searched_m": guaranteed_m,
            "source": SOURCE,
            "origin_accuracy": accuracy,
            "slack_m": slack,
        }

        try:
            points = fetch_coastline_points(lat, lon)
        except SeaViewSourceError as exc:
            logger.warning("Coastline lookup unavailable for %s,%s: %s", lat, lon, exc)
            return {**base, "status": STATUS_UNAVAILABLE, "distance_m": None}

        distance = _nearest_point_distance_m(lat, lon, points)
        if distance is None or distance > SEARCH_RADIUS_M:
            # Either the cell held no coastline, or the nearest one sits beyond
            # the radius the query guarantees. Both are measured facts rather
            # than failures: the property is simply not near the sea. This is
            # the negative the slack keeps honest -- nothing within 17 km of
            # the centroid means nothing within 12 km of the parcel, whichever
            # point inside the locality it turns out to be.
            return {**base, "status": STATUS_NO_COASTLINE, "distance_m": None}

        distance = round(distance, 1)
        if not slack:
            return {**base, "status": STATUS_OK, "distance_m": distance}

        # The coastline was found and measured, and the result is a fact about
        # the centroid rather than about this property. `distance_m` stays None
        # because that key means "how far this property is from the sea";
        # `origin_distance_m` says what was actually measured, and the bounds
        # say the only thing the geometry supports about the parcel.
        lower, upper = distance_bounds_m(distance, slack)
        return {
            **base,
            "status": STATUS_APPROXIMATE_ORIGIN,
            "distance_m": None,
            "origin_distance_m": distance,
            "min_distance_m": lower,
            "max_distance_m": upper,
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

        # First, before *any* attribute of `prop` is touched. Two reasons, and
        # the second is not obvious: a cheap raise beats a billed Overpass
        # round for a write that could not persist (#352) -- and reading an
        # expired attribute emits a SELECT, which autoflushes, which writes out
        # the pending change this guard exists to refuse. Validating after
        # `prop.location_lat` therefore reported a clean session every time,
        # having just cleaned it.
        locked = check_writable(prop, commit)

        lat = _coordinate(prop.location_lat, limit=90.0)
        lon = _coordinate(prop.location_lon, limit=180.0)
        accuracy = normalize_accuracy(getattr(prop, "location_accuracy", None))
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
                "origin_accuracy": accuracy,
                "updated_at": now,
                "last_attempt_status": STATUS_NO_COORDINATES,
                "last_attempt_at": now,
            }
            with locked_write(prop, locked=locked, commit=commit):
                self._store(prop, payload)
            return payload

        measurement = self.measure(lat, lon, accuracy)

        # `previous` is read here, under the lock, and not before the
        # measurement: a refusal must yield to whatever is *stored* when the
        # write happens, not to whatever this session loaded a minute ago
        # while Overpass was retrying (#339/#352).
        with locked_write(prop, locked=locked, commit=commit):
            previous = self._stored_payload(prop)

            if measurement["status"] == STATUS_UNAVAILABLE:
                kept = self._last_known_good(previous, lat, lon, accuracy)
                if kept is not None:
                    payload = {
                        **kept,
                        "last_attempt_status": STATUS_UNAVAILABLE,
                        "last_attempt_at": now,
                    }
                    self._store(prop, payload)
                    return payload

            payload = {
                **measurement,
                "origin": {"lat": lat, "lon": lon},
                "updated_at": now,
                "last_attempt_status": measurement["status"],
                "last_attempt_at": now,
            }
            self._store(prop, payload)
        return payload

    @staticmethod
    def _stored_payload(prop: Property) -> Optional[Dict[str, Any]]:
        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        stored = enrichment.get("sea")
        return stored if isinstance(stored, dict) else None

    @staticmethod
    def _last_known_good(
        previous: Optional[Dict[str, Any]],
        lat: float,
        lon: float,
        accuracy: str,
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
        # A row re-geocoded from centroid to address has not moved, and its
        # stored verdict is still about the centroid. Same point, different
        # claim: keeping it would serve "we cannot say" for a row that can now
        # be answered, and the reverse would serve a parcel distance for a row
        # that has just lost the right to one.
        if normalize_accuracy(previous.get("origin_accuracy")) != accuracy:
            return None

        # Every key except the attempt pair, which the caller restamps: the
        # payload grew `origin_distance_m` and the bounds, and a copy that
        # names its keys one by one silently drops whatever it was written
        # before.
        kept = {k: v for k, v in previous.items() if not k.startswith("last_attempt")}
        kept.setdefault("searched_m", SEARCH_RADIUS_M)
        kept.setdefault("source", SOURCE)
        kept["origin"] = {"lat": origin_lat, "lon": origin_lon}
        return kept

    @staticmethod
    def _store(prop: Property, payload: Dict[str, Any]) -> None:
        """Assign the block. The caller holds the lock and owns the commit."""
        # `enrichment` is a plain JSON column, not a MutableDict: mutating the
        # nested dict in place would not reach the UPDATE.
        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        enrichment["sea"] = payload
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")


def parcel_measurement(prop: Property) -> Dict[str, Any]:
    """The stored measurement, restated as a claim about *this parcel*.

    One home for the slack arithmetic, because there are two ways a stored
    payload can disagree with the row it sits on and both must come out the
    same:

    * 264 rows on the live database hold `status: ok` with a distance measured
      before this rule existed, from a coordinate that is a locality centroid.
      Rewriting them is a free Overpass recalc, but a score must not wait for
      somebody to remember to run one, so the restatement happens on read.
    * a row re-geocoded since the measurement carries a payload whose
      `slack_m` no longer matches its accuracy, in either direction.

    So the raw measurement is recovered (`searched_m` and the bounds both have
    the stored slack added back) and the current slack is applied to it. A
    precise row gets `min == max` and comes out as the plain `ok` it always
    was; nothing about the precise path is special-cased.
    """
    stored = prop.enrichment.get("sea") if isinstance(prop.enrichment, dict) else None
    accuracy = normalize_accuracy(getattr(prop, "location_accuracy", None))
    slack = coordinate_slack_m(accuracy)
    base = {"origin_accuracy": accuracy, "slack_m": slack}

    if not isinstance(stored, dict):
        return {**base, "status": STATUS_MISSING}

    status = stored.get("status")
    applied_slack = _safe_float(stored.get("slack_m")) or 0.0
    guaranteed_m = _safe_float(stored.get("searched_m"))
    if guaranteed_m is not None:
        guaranteed_m = guaranteed_m + applied_slack - slack

    if status == STATUS_NO_COASTLINE:
        return {**base, "status": STATUS_NO_COASTLINE, "searched_m": guaranteed_m}

    if status not in (STATUS_OK, STATUS_APPROXIMATE_ORIGIN):
        # unavailable / no_coordinates / anything a future version writes: no
        # measurement to restate, and inventing one is the #98 defect.
        return {**base, "status": status or STATUS_MISSING}

    measured = _safe_float(stored.get("origin_distance_m"))
    if measured is None:
        measured = _safe_float(stored.get("distance_m"))
    if measured is None:
        return {**base, "status": "missing_distance"}

    lower, upper = distance_bounds_m(measured, slack)
    return {
        **base,
        "status": STATUS_OK if not slack else STATUS_APPROXIMATE_ORIGIN,
        "distance_m": measured if not slack else None,
        "origin_distance_m": measured,
        "min_distance_m": lower,
        "max_distance_m": upper,
        "searched_m": guaranteed_m,
    }
