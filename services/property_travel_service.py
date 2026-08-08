import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app import db
from config import Config
from models import Property, SearchProfile
from services.property_location_service import PropertyLocationService
from services.search_profile_service import SearchProfileService
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from utils.google_api import (
    REASON_MALFORMED_RESPONSE,
    REASON_NO_API_KEY,
    GoogleApiFailure,
    failure_from_exception,
    read_api_payload,
)
from utils.http import request_with_retries

logger = logging.getLogger(__name__)

# Per-target outcome, persisted in Property.travel["targets"][key]["status"].
# "not_found" and "unavailable" are deliberately different states: the first is
# an answer from Google, the second is the absence of one (#98).
TARGET_STATUS_OK = "ok"
TARGET_STATUS_ESTIMATED = "estimated"
TARGET_STATUS_NOT_FOUND = "not_found"
TARGET_STATUS_UNAVAILABLE = "unavailable"
TARGET_STATUS_DISABLED = "disabled"

# Why a target that Google answered about still has no value.
NOT_FOUND_NO_NEARBY_PLACE = "no_nearby_place"
NOT_FOUND_NO_ROUTE = "no_route"
NOT_FOUND_NO_PLACE_TYPES = "no_place_types"

# Which call failed, for the operator reading the JSON.
STAGE_PLACES = "places"
STAGE_DISTANCE_MATRIX = "distance_matrix"

# Run-level verdict, persisted in Property.travel["api_status"]["state"].
TRAVEL_STATE_OK = "ok"
TRAVEL_STATE_DEGRADED = "degraded"
TRAVEL_STATE_UNAVAILABLE = "unavailable"

# Bumped from v1 with #98: v1 entries can hold an all-None distance list
# produced by a refused request, which would keep serving an empty result for
# a week after the API is fixed.
_PLACES_CACHE_PREFIX = "places_nearest_v1"
_DISTANCE_CACHE_PREFIX = "property_travel_v2"

_PLACES_CACHE_TTL = 60 * 60 * 24 * 7
_DISTANCE_CACHE_TTL = 60 * 60 * 24 * 7
_ESTIMATE_CACHE_TTL = 60 * 60 * 24 * 3

# Distance Matrix accepts at most 25 destinations per request.
_MAX_DESTINATIONS_PER_REQUEST = 25


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    r = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return r * c * 1000.0


def _estimate_duration_seconds(distance_m: float, mode: str) -> int:
    # Rough fallback when Distance Matrix isn't available.
    mode = (mode or "driving").lower()
    speed_kmh = {
        "driving": 45.0,
        "walking": 5.0,
        "bicycling": 15.0,
        "transit": 28.0,
    }.get(mode, 45.0)
    hours = (distance_m / 1000.0) / max(speed_kmh, 1.0)
    return max(60, int(round(hours * 3600)))


@dataclass(frozen=True)
class PlaceLookup:
    """Outcome of a nearest-place search.

    `place is None and failure is None` means Google answered and there is
    nothing of that type nearby - a real result, not an error. `reason` then
    says why the answer was empty.
    """

    place: Optional[Dict[str, Any]] = None
    failure: Optional[GoogleApiFailure] = None
    reason: str = NOT_FOUND_NO_NEARBY_PLACE


@dataclass(frozen=True)
class DistanceResult:
    """Outcome for a single destination of a Distance Matrix batch."""

    distance_m: Optional[int] = None
    duration_s: Optional[int] = None
    failure: Optional[GoogleApiFailure] = None
    estimated: bool = False

    @property
    def resolved(self) -> bool:
        return self.distance_m is not None or self.duration_s is not None


@dataclass
class _RunTally:
    """Counts and reason codes collected across one property's targets."""

    total: int = 0
    resolved: int = 0
    estimated: int = 0
    not_found: int = 0
    unavailable: int = 0
    errors: Dict[str, int] = field(default_factory=dict)
    details: Dict[str, str] = field(default_factory=dict)

    def record_failure(self, failure: GoogleApiFailure) -> None:
        self.total += 1
        self.unavailable += 1
        self.errors[failure.reason] = self.errors.get(failure.reason, 0) + 1
        self.details.setdefault(failure.reason, failure.describe())

    def record_not_found(self) -> None:
        self.total += 1
        self.not_found += 1

    def record_resolved(self, estimated: bool = False) -> None:
        self.total += 1
        self.resolved += 1
        if estimated:
            self.estimated += 1

    @property
    def state(self) -> str:
        if not self.unavailable:
            return TRAVEL_STATE_OK
        if self.resolved or self.not_found:
            return TRAVEL_STATE_DEGRADED
        return TRAVEL_STATE_UNAVAILABLE

    def summary(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "targets": {
                "total": self.total,
                "resolved": self.resolved,
                "estimated": self.estimated,
                "not_found": self.not_found,
                "unavailable": self.unavailable,
            },
            "errors": dict(self.errors),
        }

    def error_report(self) -> str:
        return "; ".join(
            f"{count}x {self.details.get(reason, reason)}"
            for reason, count in sorted(self.errors.items())
        )


def travel_api_state(prop: Property) -> Optional[str]:
    """Read back the verdict the last travel run stored on a property."""
    travel = getattr(prop, "travel", None)
    if not isinstance(travel, dict):
        return None
    api_status = travel.get("api_status")
    if not isinstance(api_status, dict):
        return None
    state = api_status.get("state")
    return str(state) if state else None


def _has_resolved_targets(travel: Any) -> bool:
    """True when a stored travel blob still holds at least one real value."""
    if not isinstance(travel, dict):
        return False
    targets = travel.get("targets")
    if not isinstance(targets, dict):
        return False
    for target in targets.values():
        if not isinstance(target, dict):
            continue
        if target.get("duration_min") is not None:
            return True
        if target.get("distance_m") is not None:
            return True
    return False


class PropertyTravelService:
    """Profile-driven travel enrichment for Property."""

    def __init__(
        self,
        google_maps_key: Optional[str] = None,
        google_places_key: Optional[str] = None,
        location_service: Optional[PropertyLocationService] = None,
    ):
        self.google_maps_key = (
            google_maps_key
            if google_maps_key is not None
            else Config.GOOGLE_MAPS_API_KEY
        )
        self.google_places_key = (
            google_places_key
            if google_places_key is not None
            else Config.GOOGLE_PLACES_API_KEY
        )
        self.location_service = location_service or PropertyLocationService()

    def calculate_for_property(self, prop: Property, commit: bool = False) -> bool:
        """Recompute Property.travel.

        Returns False when Google refused or never answered *every* target: a
        run that produced no data must not look like a successful one. An
        answered run with nothing nearby is still a success.
        """
        if not prop:
            return False

        if not self.location_service.ensure_coordinates(prop):
            logger.info("Property %s has no coordinates; skipping travel", prop.id)
            return False

        try:
            origin_lat = float(prop.location_lat)
            origin_lon = float(prop.location_lon)
        except Exception:
            return False

        profile = self._resolve_profile(prop)
        config = SearchProfileService.get_travel_targets_config(profile)
        preset_defs = {
            d["key"]: d for d in SearchProfileService.get_travel_preset_defs()
        }

        destinations: List[Dict[str, Any]] = []
        targets: Dict[str, Any] = {}
        tally = _RunTally()

        # Presets
        presets_cfg = config.get("presets") if isinstance(config, dict) else {}
        if isinstance(presets_cfg, dict):
            for preset_key, preset_cfg in presets_cfg.items():
                if preset_key not in preset_defs:
                    continue
                enabled = bool((preset_cfg or {}).get("enabled", True))
                mode = (
                    str((preset_cfg or {}).get("mode") or "driving").strip()
                    or "driving"
                )
                base = {
                    "kind": "preset",
                    "enabled": enabled,
                    "mode": mode,
                    "label": preset_defs[preset_key].get("label"),
                }
                if not enabled:
                    targets[preset_key] = {**base, "status": TARGET_STATUS_DISABLED}
                    continue

                lookup = self._nearest_place_for_preset(
                    origin_lat, origin_lon, preset_key, preset_defs[preset_key]
                )
                if lookup.failure is not None:
                    targets[preset_key] = {
                        **base,
                        "status": TARGET_STATUS_UNAVAILABLE,
                        "error": lookup.failure.reason,
                        "stage": STAGE_PLACES,
                    }
                    tally.record_failure(lookup.failure)
                    continue
                if not lookup.place:
                    targets[preset_key] = {
                        **base,
                        "status": TARGET_STATUS_NOT_FOUND,
                        "reason": lookup.reason,
                    }
                    tally.record_not_found()
                    continue

                place = lookup.place
                destinations.append(
                    {
                        "key": preset_key,
                        "mode": mode,
                        "lat": place["lat"],
                        "lon": place["lon"],
                    }
                )
                targets[preset_key] = {**base, "place": place}

        # Custom targets
        custom_cfg = config.get("custom") if isinstance(config, dict) else None
        if isinstance(custom_cfg, list):
            for item in custom_cfg:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                try:
                    lat = float(item.get("lat"))
                    lon = float(item.get("lon"))
                except Exception:
                    continue
                mode = str(item.get("mode") or "driving").strip() or "driving"
                raw_id = str(item.get("id") or "").strip()
                custom_id = (
                    raw_id
                    or hashlib.md5(f"{name}:{lat}:{lon}".encode()).hexdigest()[:10]
                )
                key = f"custom:{custom_id}"
                destinations.append({"key": key, "mode": mode, "lat": lat, "lon": lon})
                targets[key] = {
                    "kind": "custom",
                    "mode": mode,
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "address": item.get("address"),
                    "formatted_address": item.get("formatted_address"),
                }

        # Compute distances & durations (grouped by mode).
        by_mode: Dict[str, List[Dict[str, Any]]] = {}
        for d in destinations:
            by_mode.setdefault(str(d.get("mode") or "driving").lower(), []).append(d)

        for mode, group in by_mode.items():
            if not group:
                continue
            results = self._get_distances(origin_lat, origin_lon, group, mode=mode)
            for entry, res in zip(group, results):
                target = targets.setdefault(entry["key"], {})
                self._apply_distance(target, res, tally)

        # Invariant: every target carries a status. A target that reached the
        # distance stage and came back without one means the reply was short;
        # that is a missing answer, never a search result.
        for target in targets.values():
            if target.get("status"):
                continue
            failure = GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE,
                message="no Distance Matrix entry for this destination",
            )
            target.update(
                {
                    "status": TARGET_STATUS_UNAVAILABLE,
                    "error": failure.reason,
                    "stage": STAGE_DISTANCE_MATRIX,
                }
            )
            tally.record_failure(failure)

        travel = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "origin": {"lat": origin_lat, "lon": origin_lon},
            "profile_id": profile.id if profile else None,
            "targets": targets,
            "api_status": tally.summary(),
        }

        if tally.unavailable:
            logger.error(
                "Travel enrichment for property %s could not reach Google: "
                "%s of %s targets unavailable (%s)",
                prop.id,
                tally.unavailable,
                tally.total,
                tally.error_report(),
            )

        if tally.state == TRAVEL_STATE_UNAVAILABLE and _has_resolved_targets(
            prop.travel
        ):
            # Never trade real travel times for an empty blob because the API
            # was down for one run; record the refusal next to the old data.
            preserved = dict(prop.travel)
            preserved["api_status"] = travel["api_status"]
            prop.travel = preserved
        else:
            prop.travel = travel

        if commit:
            db.session.commit()

        return tally.state != TRAVEL_STATE_UNAVAILABLE

    def calculate_for_property_id(self, property_id: int, commit: bool = True) -> bool:
        prop = db.session.get(Property, property_id)
        if not prop:
            return False
        return self.calculate_for_property(prop, commit=commit)

    def _apply_distance(
        self, target: Dict[str, Any], res: DistanceResult, tally: _RunTally
    ) -> None:
        """Fold one Distance Matrix outcome into its target dict."""
        if res.failure is not None:
            target.update(
                {
                    "status": TARGET_STATUS_UNAVAILABLE,
                    "error": res.failure.reason,
                    "stage": STAGE_DISTANCE_MATRIX,
                }
            )
            tally.record_failure(res.failure)
            return

        if not res.resolved:
            target.update(
                {
                    "status": TARGET_STATUS_NOT_FOUND,
                    "reason": NOT_FOUND_NO_ROUTE,
                }
            )
            tally.record_not_found()
            return

        target.update(
            {
                "status": TARGET_STATUS_ESTIMATED
                if res.estimated
                else TARGET_STATUS_OK,
                "estimated": res.estimated,
                "distance_m": res.distance_m,
                "distance_km": (res.distance_m / 1000.0)
                if res.distance_m is not None
                else None,
                "duration_s": res.duration_s,
                "duration_min": int(round(res.duration_s / 60.0))
                if res.duration_s is not None
                else None,
            }
        )
        tally.record_resolved(estimated=res.estimated)

    def _resolve_profile(self, prop: Property) -> Optional[SearchProfile]:
        if prop.search_profile:
            return prop.search_profile
        if prop.search_profile_id:
            try:
                profile = db.session.get(SearchProfile, prop.search_profile_id)
                if profile:
                    return profile
            except Exception:
                pass
        return SearchProfileService.get_default_profile(create=True)

    def _nearest_place_for_preset(
        self, lat: float, lon: float, preset_key: str, preset_def: Dict[str, Any]
    ) -> PlaceLookup:
        place_types = (
            preset_def.get("place_types") if isinstance(preset_def, dict) else None
        )
        if not isinstance(place_types, list) or not place_types:
            return PlaceLookup(reason=NOT_FOUND_NO_PLACE_TYPES)

        best: Optional[Tuple[float, Dict[str, Any]]] = None
        failure: Optional[GoogleApiFailure] = None
        for place_type in place_types:
            lookup = self._nearest_place(lat, lon, place_type=str(place_type))
            if lookup.failure is not None and failure is None:
                failure = lookup.failure
            place = lookup.place
            if not place:
                continue
            distance_m = _haversine_m(
                lat, lon, float(place["lat"]), float(place["lon"])
            )
            if best is None or distance_m < best[0]:
                best = (distance_m, place)

        if best is not None:
            # A hit from one place type beats a refusal on another.
            best_place = dict(best[1])
            best_place["preset_key"] = preset_key
            return PlaceLookup(place=best_place)

        return PlaceLookup(failure=failure)

    def _nearest_place(self, lat: float, lon: float, place_type: str) -> PlaceLookup:
        place_type = (place_type or "").strip()
        if not place_type:
            return PlaceLookup()

        cache_type = f"{_PLACES_CACHE_PREFIX}:{place_type}"
        cached = get_cached_enrichment_data(lat, lon, cache_type)
        if (
            isinstance(cached, dict)
            and cached.get("lat") is not None
            and cached.get("lon") is not None
        ):
            return PlaceLookup(place=cached)

        if not self.google_places_key:
            return PlaceLookup(failure=GoogleApiFailure(reason=REASON_NO_API_KEY))

        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lon}",
            "rankby": "distance",
            "type": place_type,
            "key": self.google_places_key,
        }
        try:
            response = request_with_retries(
                requests.get, url, params=params, timeout=12, logger=logger
            )
        except Exception as e:
            failure = failure_from_exception(e)
            logger.warning(
                "Places lookup failed (%s): %s", place_type, failure.describe()
            )
            return PlaceLookup(failure=failure)

        payload, failure = read_api_payload(response)
        if failure is not None:
            logger.warning(
                "Places lookup refused (%s): %s", place_type, failure.describe()
            )
            return PlaceLookup(failure=failure)

        results = payload.get("results") or []
        if not isinstance(results, list) or not results:
            # Google answered: nothing of this type nearby.
            return PlaceLookup()

        first = results[0] if isinstance(results[0], dict) else {}
        loc = (first.get("geometry") or {}).get("location") or {}
        out = {
            "name": first.get("name"),
            "place_id": first.get("place_id"),
            "types": first.get("types"),
            "lat": loc.get("lat"),
            "lon": loc.get("lng"),
        }
        if out.get("lat") is None or out.get("lon") is None:
            return PlaceLookup()

        cache_enrichment_data(lat, lon, cache_type, out, timeout=_PLACES_CACHE_TTL)
        return PlaceLookup(place=out)

    def _get_distances(
        self, lat: float, lon: float, destinations: List[Dict[str, Any]], mode: str
    ) -> List[DistanceResult]:
        if not destinations:
            return []

        mode = (mode or "driving").lower()
        estimated = not self.google_maps_key

        dest_sig = "|".join(
            f"{d.get('lat')},{d.get('lon')}:{mode}" for d in destinations
        )
        dest_hash = hashlib.md5(dest_sig.encode()).hexdigest()[:10]
        # Estimates live in their own namespace so that configuring the key
        # later does not keep serving haversine guesses from the cache.
        kind = "estimate" if estimated else "matrix"
        cache_type = f"{_DISTANCE_CACHE_PREFIX}:{kind}:{mode}:{dest_hash}"

        cached = get_cached_enrichment_data(lat, lon, cache_type)
        if isinstance(cached, list) and len(cached) == len(destinations):
            return [self._result_from_cache(entry, estimated) for entry in cached]

        if estimated:
            out = [
                self._estimate_distance(lat, lon, d, mode=mode) for d in destinations
            ]
            self._cache_distances(
                lat, lon, cache_type, out, timeout=_ESTIMATE_CACHE_TTL
            )
            return out

        out: List[DistanceResult] = []
        dest_coords = [f"{d.get('lat')},{d.get('lon')}" for d in destinations]

        for chunk_start in range(0, len(dest_coords), _MAX_DESTINATIONS_PER_REQUEST):
            chunk = dest_coords[
                chunk_start : chunk_start + _MAX_DESTINATIONS_PER_REQUEST
            ]
            out.extend(self._distance_matrix_batch(lat, lon, chunk, mode=mode))

        while len(out) < len(destinations):
            out.append(
                DistanceResult(
                    failure=GoogleApiFailure(
                        reason=REASON_MALFORMED_RESPONSE,
                        message="fewer Distance Matrix results than destinations",
                    )
                )
            )

        # A refused batch must not be cached: it would keep returning "no data"
        # for a week after the API is working again (#98).
        if not any(r.failure is not None for r in out):
            self._cache_distances(
                lat, lon, cache_type, out, timeout=_DISTANCE_CACHE_TTL
            )
        return out

    @staticmethod
    def _result_from_cache(entry: Any, estimated: bool) -> DistanceResult:
        if not isinstance(entry, dict):
            return DistanceResult(estimated=estimated)
        return DistanceResult(
            distance_m=entry.get("distance_m"),
            duration_s=entry.get("duration_s"),
            estimated=estimated,
        )

    @staticmethod
    def _cache_distances(
        lat: float,
        lon: float,
        cache_type: str,
        results: List[DistanceResult],
        timeout: int,
    ) -> None:
        payload = [
            {"distance_m": r.distance_m, "duration_s": r.duration_s}
            if r.resolved
            else None
            for r in results
        ]
        cache_enrichment_data(lat, lon, cache_type, payload, timeout=timeout)

    @staticmethod
    def _estimate_distance(
        lat: float, lon: float, destination: Dict[str, Any], mode: str
    ) -> DistanceResult:
        try:
            dlat = float(destination.get("lat"))
            dlon = float(destination.get("lon"))
        except Exception:
            return DistanceResult(estimated=True)
        distance_m = int(round(_haversine_m(lat, lon, dlat, dlon)))
        return DistanceResult(
            distance_m=distance_m,
            duration_s=_estimate_duration_seconds(distance_m, mode=mode),
            estimated=True,
        )

    def _distance_matrix_batch(
        self, lat: float, lon: float, destinations: List[str], mode: str
    ) -> List[DistanceResult]:
        if not destinations:
            return []

        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": f"{lat},{lon}",
            "destinations": "|".join(destinations),
            "mode": mode,
            "key": self.google_maps_key,
        }
        try:
            response = request_with_retries(
                requests.get, url, params=params, timeout=15, logger=logger
            )
        except Exception as e:
            failure = failure_from_exception(e)
            logger.warning("Distance matrix failed: %s", failure.describe())
            return [DistanceResult(failure=failure) for _ in destinations]

        payload, failure = read_api_payload(response)
        if failure is not None:
            logger.warning("Distance matrix refused: %s", failure.describe())
            return [DistanceResult(failure=failure) for _ in destinations]

        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            failure = GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE,
                message="no rows in Distance Matrix reply",
            )
            logger.warning("Distance matrix malformed: %s", failure.describe())
            return [DistanceResult(failure=failure) for _ in destinations]

        elements = rows[0].get("elements")
        if not isinstance(elements, list) or not elements:
            failure = GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE,
                message="no elements in Distance Matrix reply",
            )
            logger.warning("Distance matrix malformed: %s", failure.describe())
            return [DistanceResult(failure=failure) for _ in destinations]

        results: List[DistanceResult] = []
        for el in elements[: len(destinations)]:
            if not isinstance(el, dict) or el.get("status") != "OK":
                # ZERO_RESULTS / NOT_FOUND / MAX_ROUTE_LENGTH_EXCEEDED are
                # answers about that destination, not transport failures.
                results.append(DistanceResult())
                continue
            results.append(
                DistanceResult(
                    distance_m=(el.get("distance") or {}).get("value"),
                    duration_s=(el.get("duration") or {}).get("value"),
                )
            )
        while len(results) < len(destinations):
            results.append(DistanceResult())
        return results
