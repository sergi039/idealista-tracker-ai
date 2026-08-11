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
class _PlaceRules:
    """What a preset will and will not accept as its nearest place.

    Google's place types are broad: `airport` covers helipads and any business
    that claimed the tag, `hospital` covers dentists and cosmetic clinics, and
    the nearest such hit is routinely not the thing the preset is named after.
    A deny-list alone does not survive contact with the data -- refusing one
    tagged business just promotes the next one -- so a preset may also require
    the place to *say* what it is. Nothing qualifying nearby is reported as
    not found, which the scorer treats as absent rather than as zero.
    """

    require_name_patterns: Tuple[str, ...] = ()
    reject_name_patterns: Tuple[str, ...] = ()
    reject_types: Tuple[str, ...] = ()

    @property
    def signature(self) -> str:
        payload = "#".join(
            (
                "|".join(self.require_name_patterns),
                "|".join(self.reject_name_patterns),
                "|".join(self.reject_types),
            )
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]

    def rejects(self, candidate: Dict[str, Any]) -> bool:
        name = str(candidate.get("name") or "").casefold()
        if self.require_name_patterns and not any(
            pattern in name for pattern in self.require_name_patterns
        ):
            return True
        if any(pattern in name for pattern in self.reject_name_patterns):
            return True
        candidate_types = candidate.get("types")
        if isinstance(candidate_types, list):
            lowered = {str(t).casefold() for t in candidate_types}
            if lowered & set(self.reject_types):
                return True
        return False


def _place_rules(preset_def: Dict[str, Any]) -> Optional[_PlaceRules]:
    if not isinstance(preset_def, dict):
        return None

    def _patterns(key: str) -> Tuple[str, ...]:
        value = preset_def.get(key)
        if not isinstance(value, list):
            return ()
        return tuple(str(item).casefold() for item in value if str(item).strip())

    require = _patterns("require_name_patterns")
    reject_names = _patterns("reject_name_patterns")
    reject_types = _patterns("reject_types")
    if not require and not reject_names and not reject_types:
        return None
    return _PlaceRules(
        require_name_patterns=require,
        reject_name_patterns=reject_names,
        reject_types=reject_types,
    )


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

        reject = _place_rules(preset_def)

        best: Optional[Tuple[float, Dict[str, Any]]] = None
        failure: Optional[GoogleApiFailure] = None
        for place_type in place_types:
            lookup = self._nearest_place(
                lat, lon, place_type=str(place_type), reject=reject
            )
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

        # Nearby Search found nothing this preset accepts. For a preset whose
        # real answer can legitimately sit past its ~50 km reach (only
        # "airport" opts in today via `wide_search_query`, see
        # search_profile_service.py for the measurement), fall back to a
        # Places Text Search -- a second, paid call. It only fires when
        # Nearby Search actually answered and still found nothing usable: a
        # failure stays a failure rather than spending another call chasing
        # the same refusal.
        wide_query = (
            preset_def.get("wide_search_query")
            if isinstance(preset_def, dict)
            else None
        )
        if failure is None and wide_query:
            wide_lookup = self._nearest_place_text_search(
                lat, lon, query=str(wide_query), place_types=place_types, reject=reject
            )
            if wide_lookup.place is not None:
                wide_place = dict(wide_lookup.place)
                wide_place["preset_key"] = preset_key
                return PlaceLookup(place=wide_place)
            failure = wide_lookup.failure

        return PlaceLookup(failure=failure)

    def _nearest_place(
        self,
        lat: float,
        lon: float,
        place_type: str,
        reject: Optional["_PlaceRules"] = None,
    ) -> PlaceLookup:
        place_type = (place_type or "").strip()
        if not place_type:
            return PlaceLookup()

        # The rules are part of the key: entries cached before a preset learned
        # to refuse helipads would otherwise keep serving the helipad.
        cache_type = f"{_PLACES_CACHE_PREFIX}:{place_type}"
        if reject is not None:
            cache_type = f"{cache_type}:{reject.signature}"
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

        # `rankby=distance` orders the list, so the first candidate the preset
        # accepts is the nearest one. Taking `results[0]` unconditionally is
        # what put a contractor tagged `airport` 2.4 km away in front of the
        # real airport 40 km away.
        for candidate in results:
            if not isinstance(candidate, dict):
                continue
            if reject is not None and reject.rejects(candidate):
                logger.debug(
                    "Places lookup skipped %s for %s: fails the preset's rules",
                    candidate.get("name"),
                    place_type,
                )
                continue
            loc = (candidate.get("geometry") or {}).get("location") or {}
            out = {
                "name": candidate.get("name"),
                "place_id": candidate.get("place_id"),
                "types": candidate.get("types"),
                "lat": loc.get("lat"),
                "lon": loc.get("lng"),
            }
            if out.get("lat") is None or out.get("lon") is None:
                continue

            cache_enrichment_data(lat, lon, cache_type, out, timeout=_PLACES_CACHE_TTL)
            return PlaceLookup(place=out)

        # Everything nearby was refused. That is an answer -- "no airport here"
        # -- and not a failure, so it must not read as an API refusal.
        return PlaceLookup()

    def _nearest_place_text_search(
        self,
        lat: float,
        lon: float,
        query: str,
        place_types: List[Any],
        reject: Optional["_PlaceRules"] = None,
    ) -> PlaceLookup:
        """Text Search fallback for a preset whose real answer can be farther
        than Nearby Search's reach.

        Nearby Search is capped at roughly 50 km regardless of `rankby=distance`
        or an explicit `radius=` -- measured 2026-08-11 against property 360
        (La Caridad, El Franco): `radius=75000` and `radius=120000` both came
        back with the identical 7 places, farthest 45.2 km, matching plain
        `rankby=distance`. Text Search has no such cap when called without a
        `radius` (`location` only biases its ranking, it does not bound it);
        the same query found Asturias Airport -- 64.3 km away -- as its
        nearest qualifying result on the first try.

        Only reached from `_nearest_place_for_preset` when the primary Nearby
        Search already answered and found nothing this preset accepts, so
        this second, paid call is the exception rather than the rule.
        """
        # Every preset that defines `place_types` today lists exactly one
        # (see TRAVEL_PRESET_DEFS); a preset that ever needed several and
        # also opted into `wide_search_query` would have to loop here the
        # way the primary Nearby Search loop does, one call per type.
        place_type = str(place_types[0]) if place_types else ""

        # A distinct cache key from `_nearest_place`'s: a different endpoint
        # with a different result shape and ordering, not a substitute lookup
        # for the same query.
        cache_type = f"{_PLACES_CACHE_PREFIX}:text:{query}:{place_type}"
        if reject is not None:
            cache_type = f"{cache_type}:{reject.signature}"
        cached = get_cached_enrichment_data(lat, lon, cache_type)
        if (
            isinstance(cached, dict)
            and cached.get("lat") is not None
            and cached.get("lon") is not None
        ):
            return PlaceLookup(place=cached)

        if not self.google_places_key:
            return PlaceLookup(failure=GoogleApiFailure(reason=REASON_NO_API_KEY))

        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {
            "query": query,
            "location": f"{lat},{lon}",
            "key": self.google_places_key,
        }
        if place_type:
            # Narrows the free-text query back to the type the preset cares
            # about -- without it, "airport" alone also matched car rentals
            # and parking lots that merely mention one.
            params["type"] = place_type
        try:
            response = request_with_retries(
                requests.get, url, params=params, timeout=12, logger=logger
            )
        except Exception as e:
            failure = failure_from_exception(e)
            logger.warning(
                "Places text search failed (%s): %s", query, failure.describe()
            )
            return PlaceLookup(failure=failure)

        payload, failure = read_api_payload(response)
        if failure is not None:
            logger.warning(
                "Places text search refused (%s): %s", query, failure.describe()
            )
            return PlaceLookup(failure=failure)

        results = payload.get("results") or []
        if not isinstance(results, list) or not results:
            return PlaceLookup()

        # Unlike `rankby=distance`, Text Search orders by relevance rather
        # than strict distance -- a globally prominent airport can outrank a
        # closer, less famous one. Every candidate's distance is computed
        # here and the nearest one that survives the preset's rules wins.
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for candidate in results:
            if not isinstance(candidate, dict):
                continue
            if reject is not None and reject.rejects(candidate):
                continue
            loc = (candidate.get("geometry") or {}).get("location") or {}
            out = {
                "name": candidate.get("name"),
                "place_id": candidate.get("place_id"),
                "types": candidate.get("types"),
                "lat": loc.get("lat"),
                "lon": loc.get("lng"),
            }
            if out.get("lat") is None or out.get("lon") is None:
                continue
            distance_m = _haversine_m(lat, lon, float(out["lat"]), float(out["lon"]))
            if best is None or distance_m < best[0]:
                best = (distance_m, out)

        if best is None:
            # Google answered and nothing survived the preset's rules either --
            # still a real "not found", same as Nearby Search's empty case.
            return PlaceLookup()

        logger.info(
            "Places wide search resolved %r for query %r (%.1f km away)",
            best[1].get("name"),
            query,
            best[0] / 1000.0,
        )
        cache_enrichment_data(lat, lon, cache_type, best[1], timeout=_PLACES_CACHE_TTL)
        return PlaceLookup(place=best[1])

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
