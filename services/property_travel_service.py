import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app import db
from config import Config
from models import Property, SearchProfile
from services.coordinate_quality import is_precise, normalize_accuracy
from services.place_rules import PlaceRules as _PlaceRules
from services import osm_places
from services.reference_places import nearest_reference_place
from services.place_rules import place_rules_from as _place_rules
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
# The row's coordinate is a locality centroid, so its durations are routes from
# a point the property is merely near. Since 2026-08-17 this is a *reader's*
# state and nothing writes it to a row: `effective_travel_state` derives it from
# the row's accuracy on every read, which is the only way that survives a
# re-geocode. The durations exist -- the run is no longer refused -- and what
# refuses is the scorer, target by target, inside the 5 km slack. Distinct from
# `unavailable`, which means Google was asked and would not answer.
TRAVEL_STATE_APPROXIMATE_ORIGIN = "approximate_origin"
# The row has no coordinate at all, so no request was ever made -- not to
# Places, not to Distance Matrix, and not for the beaches that ride in that
# same batch. Distinct from both of the above: `unavailable` means Google was
# asked and refused, `approximate_origin` means there is a point and it is the
# wrong one, and this means there is no point to route from. It is fixed by a
# geocode, which is what the Enrich button does first.
TRAVEL_STATE_NOT_LOCATED = "not_located"

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

# --- Beaches -----------------------------------------------------------------
# A beach is deliberately *not* a travel preset. A preset resolves exactly one
# place and feeds the scorer; this list is informational, holds as many beaches
# as are within reach, and must never move a score. It rides along in the same
# Distance Matrix batch as the presets, so it costs one extra Places call per
# property and no extra Distance Matrix request -- which is why the beaches
# take only the room the presets leave in that one request (#260), nearest
# first, instead of pushing the group over the per-request limit and being
# billed twice.
BEACHES_STATUS_OK = "ok"
BEACHES_STATUS_NONE_WITHIN_LIMIT = "none_within_limit"
BEACHES_STATUS_NOT_FOUND = "not_found"
BEACHES_STATUS_UNAVAILABLE = "unavailable"

# Beach places are read as `natural_feature` narrowed by a keyword, the same
# pair the legacy `travel_time_service` used. Google's legacy Nearby Search has
# no `beach` type, and `tourist_attraction` alone returns museums and viewpoints.
# The keyword is Spanish because the listings are: another region needs its own.
_BEACH_PLACE_TYPE = "natural_feature"
_BEACH_KEYWORD = "playa"
_BEACH_MODE = "driving"
_BEACH_TARGET_PREFIX = "beach:"
_BEACH_CACHE_PREFIX = "places_beaches_v1"

# How long a drive still counts as "at the beach". The owner asked for 20
# minutes; the environment variable exists so changing it needs no deploy.
BEACH_MAX_DRIVE_MIN_DEFAULT = 20

# One Places page. Paging is a second billable request per property for beaches
# nobody would drive to, and `rankby=distance` already puts the nearest first.
_BEACH_MAX_CANDIDATES = 20

# Only candidates this close in a straight line reach Distance Matrix. A road is
# never shorter than the straight line, so a beach 30 km away would need a 90
# km/h average to come in under 20 minutes -- which no coastal road does. The
# ones beyond it cannot pass the filter, so paying to measure them is waste.
_BEACH_CANDIDATE_RADIUS_M = 30_000

# `natural_feature` plus a keyword still lets businesses named after the beach
# through -- campsites, hotels and beach bars carry "playa" in their name.
_BEACH_REJECT_NAMES = (
    "camping",
    "hotel",
    "hostal",
    "apartament",
    "apartamento",
    "restaurant",
    "chiringuito",
    "parking",
    "aparcamiento",
    "mirador",
    "urbanizaci",
    "residencial",
)
_BEACH_REJECT_TYPES = (
    "lodging",
    "restaurant",
    "cafe",
    "bar",
    "campground",
    "parking",
    "store",
    "real_estate_agency",
)


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


def estimate_duration_seconds(distance_m: float, mode: str) -> int:
    """Rough fallback when Distance Matrix isn't available.

    Public because `services/property_scoring_service.py` converts an
    approximate origin's positional slack into minutes with it: the score has
    to know how much travel time 5 km of coordinate error is worth, and the
    answer already exists here.
    """
    mode = (mode or "driving").lower()
    speed_kmh = {
        "driving": 45.0,
        "walking": 5.0,
        "bicycling": 15.0,
        "transit": 28.0,
    }.get(mode, 45.0)
    hours = (distance_m / 1000.0) / max(speed_kmh, 1.0)
    return max(60, int(round(hours * 3600)))


_BEACH_RULES = _PlaceRules(
    reject_name_patterns=_BEACH_REJECT_NAMES,
    reject_types=_BEACH_REJECT_TYPES,
)


def _beach_limit_min() -> int:
    """The drive-time limit a beach must come in under, in minutes."""
    raw = getattr(Config, "BEACH_MAX_DRIVE_MIN", BEACH_MAX_DRIVE_MIN_DEFAULT)
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return BEACH_MAX_DRIVE_MIN_DEFAULT
    # A non-positive limit would hide the block everywhere while looking
    # configured; a bad value is not a reason to silently disable a feature.
    return limit if limit > 0 else BEACH_MAX_DRIVE_MIN_DEFAULT


def _place_from_result(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The fields kept from one Places result, or None when it has no location."""
    geometry = candidate.get("geometry")
    location = geometry.get("location") if isinstance(geometry, dict) else None
    location = location if isinstance(location, dict) else {}
    place = {
        "name": candidate.get("name"),
        "place_id": candidate.get("place_id"),
        "types": candidate.get("types"),
        "lat": location.get("lat"),
        "lon": location.get("lng"),
    }
    if place["lat"] is None or place["lon"] is None:
        return None
    return place


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


@dataclass
class BeachLookup:
    """Outcome of the beach search around one property.

    `places` are the candidates close enough to be worth measuring, nearest
    first; `total_found` counts every beach Google returned, including the ones
    dropped for being beyond the radius -- that is what tells "no beaches here"
    apart from "beaches, but all of them far away".
    """

    places: List[Dict[str, Any]] = field(default_factory=list)
    total_found: int = 0
    failure: Optional[GoogleApiFailure] = None


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


def effective_travel_state(prop: Property) -> Optional[str]:
    """The verdict a reader should act on, not merely the one on record.

    The row's coordinate quality outranks the stored run state, because 532 of
    the 725 located rows carry `state: ok` from a run that predates this rule
    and measured from a locality centroid. Those durations are not wrong about
    the point they were measured from; they are just not about the property,
    and no amount of re-reading the stored block reveals that. The column does.

    A row with no coordinate is `not_located` whatever the block says, for the
    same reason and one step further along: those durations are not merely
    about the wrong point, they are about a point the row can no longer name.
    Before this the answer was the stored state, which for the rows that have
    never been located is `None` -- indistinguishable, everywhere it is read,
    from a listing whose targets were measured and came back empty.

    `travel_api_state` stays what it always was -- what the last run wrote --
    because "did Google answer" and "is this the parcel" are two questions and
    the ledger of the first is worth keeping intact.
    """
    if prop is None:
        return None
    if prop.location_lat is None or prop.location_lon is None:
        return TRAVEL_STATE_NOT_LOCATED
    if not is_precise(getattr(prop, "location_accuracy", None)):
        return TRAVEL_STATE_APPROXIMATE_ORIGIN
    return travel_api_state(prop)


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

        # #358 refused here, before the Places and Distance Matrix calls, so
        # that no credit was spent on a duration measured from a locality
        # centroid. The owner lifted that on 2026-08-17, and the reason it can
        # be lifted safely is that the refusal was only one of three things
        # #358 built. The other two stay exactly as they are: the scorer still
        # applies the 5 km slack target by target and drops a duration it
        # cannot vouch for, and every surface still reads the row's *current*
        # accuracy through `effective_travel_state` and captions it. So what
        # comes back is the measurement, not the claim that it is the parcel's
        # -- which is the state 115 rows in `Plots 0-50 km` have carried since
        # before #358 landed, and which the pages already draw correctly.
        #
        # What the lift does cost is money: ~$0.36 a listing, on rows that were
        # free to walk past. Nothing unattended reaches this method -- travel
        # is a button press, a profile recalc or `utils/recalc_property_travel`
        # -- so the spender is always someone who asked. Do not add an
        # automatic caller without reading the billing rule in CLAUDE.md first.

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

        # Beaches. They ride in the same Distance Matrix batch as the targets
        # above -- one extra Places call per property, no extra route request --
        # but they are kept out of `targets` and out of the tally on purpose:
        # no score reads them, so a beach Google would not talk about must not
        # turn a good travel run into a degraded one. Their own status carries
        # that fact instead.
        beach_lookup = self._beach_candidates(origin_lat, origin_lon)
        beach_entries: Dict[str, Dict[str, Any]] = {}
        # "No extra Distance Matrix request" is only true if the merged group
        # fits in one. Six presets plus twenty beach candidates is 26 against a
        # 25-destination request, which `_get_distances` splits into two billed
        # calls -- so the beaches take the room that is left, nearest first,
        # rather than the promise quietly becoming false on exactly the coastal
        # listings this list is for (#260).
        room_for_beaches = max(
            0,
            _MAX_DESTINATIONS_PER_REQUEST
            - sum(
                1
                for d in destinations
                if str(d.get("mode") or "driving").lower() == _BEACH_MODE
            ),
        )
        for index, place in enumerate(beach_lookup.places[:room_for_beaches]):
            key = f"{_BEACH_TARGET_PREFIX}{index}"
            destinations.append(
                {
                    "key": key,
                    "mode": _BEACH_MODE,
                    "lat": place["lat"],
                    "lon": place["lon"],
                }
            )
            beach_entries[key] = {"place": place}

        # Compute distances & durations (grouped by mode).
        by_mode: Dict[str, List[Dict[str, Any]]] = {}
        for d in destinations:
            by_mode.setdefault(str(d.get("mode") or "driving").lower(), []).append(d)

        for mode, group in by_mode.items():
            if not group:
                continue
            results = self._get_distances(origin_lat, origin_lon, group, mode=mode)
            for entry, res in zip(group, results):
                key = entry["key"]
                if key in beach_entries:
                    self._apply_distance(beach_entries[key], res, None)
                    continue
                target = targets.setdefault(key, {})
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

        # The same invariant for the beaches, minus the tally: a short reply
        # leaves them unmeasured, which `_beaches_payload` reports as such
        # rather than dropping them as "too far".
        for entry in beach_entries.values():
            if not entry.get("status"):
                entry.update(
                    {
                        "status": TARGET_STATUS_UNAVAILABLE,
                        "error": REASON_MALFORMED_RESPONSE,
                        "stage": STAGE_DISTANCE_MATRIX,
                    }
                )

        travel = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "origin": {"lat": origin_lat, "lon": origin_lon},
            "profile_id": profile.id if profile else None,
            "targets": targets,
            "beaches": self._beaches_payload(beach_lookup, beach_entries),
            # What the run measured *from*, recorded with the run itself. The
            # surfaces do not read this -- they read the row's accuracy now,
            # because a re-geocode changes the answer and no stored value
            # follows it (#358) -- but a ledger that cannot say what its own
            # numbers were taken from is the gap this whole family started in.
            "api_status": {
                **tally.summary(),
                "origin_accuracy": normalize_accuracy(
                    getattr(prop, "location_accuracy", None)
                ),
            },
        }

        if tally.unavailable:
            logger.error(
                # Not "could not reach Google": since 2026-08-18 the presets
                # are resolved from Overpass and only the routing is Google's,
                # so naming one vendor in a line that reports both sends
                # whoever reads it to the wrong status page. The reasons in
                # the tail say which source refused.
                "Travel enrichment for property %s could not reach a lookup "
                "source: %s of %s targets unavailable (%s)",
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
        self,
        target: Dict[str, Any],
        res: DistanceResult,
        tally: Optional[_RunTally],
    ) -> None:
        """Fold one Distance Matrix outcome into its target dict.

        `tally` is None for destinations that must not sway the run's verdict --
        the beaches, which no score reads.
        """
        if res.failure is not None:
            target.update(
                {
                    "status": TARGET_STATUS_UNAVAILABLE,
                    "error": res.failure.reason,
                    "stage": STAGE_DISTANCE_MATRIX,
                }
            )
            if tally is not None:
                tally.record_failure(res.failure)
            return

        if not res.resolved:
            target.update(
                {
                    "status": TARGET_STATUS_NOT_FOUND,
                    "reason": NOT_FOUND_NO_ROUTE,
                }
            )
            if tally is not None:
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
        if tally is not None:
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
        # A preset answered from a local register never reaches Google -- not
        # for the search, and not as a fallback when the register cannot
        # answer. A refusal here is a refusal: falling through would spend
        # exactly where the register is thinnest, which is the opposite of
        # what declaring it was for (services/reference_places.py).
        reference_source = (
            preset_def.get("reference_source") if isinstance(preset_def, dict) else None
        )
        if reference_source:
            reference = nearest_reference_place(str(reference_source), lat, lon)
            if reference is not None:
                if reference.place is not None:
                    place = dict(reference.place)
                    place["preset_key"] = preset_key
                    return PlaceLookup(place=place)
                # Not "nothing nearby": the register was asked and could not
                # answer, so this must not score as a measured absence (#98).
                return PlaceLookup(
                    failure=GoogleApiFailure(reason=str(reference.reason))
                )

        # OpenStreetMap answers the five remaining presets (step 2 of the cost
        # plan). Same rule as the register above: what OSM cannot answer is a
        # refusal, not a reason to buy the answer from Places -- including the
        # `wide_search_query` fallback below, whose whole purpose was Google's
        # 50 km cap, which Overpass does not have.
        spec = osm_places.osm_spec(preset_def)
        if spec is not None:
            osm_lookup = self._osm_place(lat, lon, preset_key, preset_def, spec)
            if osm_lookup is not None:
                return osm_lookup

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

    def _osm_place(
        self,
        lat: float,
        lon: float,
        preset_key: str,
        preset_def: Dict[str, Any],
        spec: Tuple[str, str, int],
    ) -> Optional[PlaceLookup]:
        """This preset's nearest acceptable OSM place, or why there is none.

        Returns `None` only when OSM cannot be consulted at all -- no
        enrichment service to borrow the Overpass transport from -- which is
        the one case where falling through to the paid path is right, because
        nothing was asked of anything.

        Every declared preset rides in one Overpass query: the first one to
        run fetches them all and caches the candidates, so the four after it
        cost no round trip. That is why the specs of *every* preset are
        collected here rather than just this one's.
        """
        from services.search_profile_service import TRAVEL_PRESET_DEFS

        service = getattr(self, "enrichment_service", None)
        if service is None:
            from services.enrichment_service import EnrichmentService

            service = EnrichmentService()
            self.enrichment_service = service

        specs = {}
        for key, definition in TRAVEL_PRESET_DEFS.items():
            declared = osm_places.osm_spec(definition)
            if declared is not None:
                specs[key] = declared
        specs[preset_key] = spec

        candidates, failure = osm_places.lookup_candidates(service, specs, lat, lon)
        if failure is not None:
            # Overpass refused. Not "no airport here", and not a reason to
            # spend: a refusal is retried by the next run for free (#144).
            return PlaceLookup(failure=failure)

        chosen = osm_places.pick(preset_def, (candidates or {}).get(preset_key) or [])
        if chosen is None:
            # Overpass answered and nothing qualifies. That is a measurement
            # -- "no airport within 100 km" is true of an inland valley -- and
            # it scores as absent rather than as a failure (#98).
            return PlaceLookup(reason=NOT_FOUND_NO_NEARBY_PLACE)

        place = dict(chosen)
        place["preset_key"] = preset_key
        return PlaceLookup(place=place)

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

        results, failure = self._places_nearby(lat, lon, place_type=place_type)
        if failure is not None:
            return PlaceLookup(failure=failure)
        if not results:
            # Google answered: nothing of this type nearby.
            return PlaceLookup()

        # `rankby=distance` orders the list, so the first candidate the preset
        # accepts is the nearest one. Taking `results[0]` unconditionally is
        # what put a contractor tagged `airport` 2.4 km away in front of the
        # real airport 40 km away.
        for candidate in results:
            if reject is not None and reject.rejects(candidate):
                logger.debug(
                    "Places lookup skipped %s for %s: fails the preset's rules",
                    candidate.get("name"),
                    place_type,
                )
                continue
            out = _place_from_result(candidate)
            if out is None:
                continue

            cache_enrichment_data(lat, lon, cache_type, out, timeout=_PLACES_CACHE_TTL)
            return PlaceLookup(place=out)

        # Everything nearby was refused. That is an answer -- "no airport here"
        # -- and not a failure, so it must not read as an API refusal.
        return PlaceLookup()

    def _places_nearby(
        self,
        lat: float,
        lon: float,
        place_type: str,
        keyword: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[GoogleApiFailure]]:
        """One page of Places Nearby Search, ranked by distance.

        The single Places transport in this service: `_nearest_place` takes the
        first acceptable hit off it, the beach lookup takes every hit within
        reach, and the request, its refusals and its logging stay in one place.

        A refusal comes back as the failure, never as an empty list -- "Google
        did not answer" and "Google says there is nothing here" are different
        facts (#98), and only the second one may be recorded as a result.
        """
        if not self.google_places_key:
            return [], GoogleApiFailure(reason=REASON_NO_API_KEY)

        url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lon}",
            "rankby": "distance",
            "type": place_type,
            "key": self.google_places_key,
        }
        if keyword:
            params["keyword"] = keyword

        try:
            response = request_with_retries(
                requests.get, url, params=params, timeout=12, logger=logger
            )
        except Exception as e:
            failure = failure_from_exception(e)
            logger.warning(
                "Places lookup failed (%s): %s", place_type, failure.describe()
            )
            return [], failure

        payload, failure = read_api_payload(response)
        if failure is not None:
            logger.warning(
                "Places lookup refused (%s): %s", place_type, failure.describe()
            )
            return [], failure

        results = payload.get("results")
        if not isinstance(results, list):
            return [], None
        return [item for item in results if isinstance(item, dict)], None

    def _beach_candidates(self, lat: float, lon: float) -> "BeachLookup":
        """Beaches near a property, nearest first, within the drive radius.

        Only the candidates worth measuring are returned: the rest could not
        come in under the time limit whatever the roads look like, and each one
        would be a billed Distance Matrix element.
        """
        cache_type = (
            f"{_BEACH_CACHE_PREFIX}:{_BEACH_PLACE_TYPE}:"
            f"{_BEACH_KEYWORD}:{_BEACH_RULES.signature}"
        )
        cached = get_cached_enrichment_data(lat, lon, cache_type)
        if isinstance(cached, dict) and isinstance(cached.get("places"), list):
            return BeachLookup(
                places=[p for p in cached["places"] if isinstance(p, dict)],
                total_found=int(cached.get("total_found") or 0),
            )

        results, failure = self._places_nearby(
            lat, lon, place_type=_BEACH_PLACE_TYPE, keyword=_BEACH_KEYWORD
        )
        if failure is not None:
            return BeachLookup(failure=failure)

        places: List[Dict[str, Any]] = []
        total_found = 0
        for candidate in results:
            if _BEACH_RULES.rejects(candidate):
                continue
            place = _place_from_result(candidate)
            if place is None:
                continue
            total_found += 1
            straight_m = _haversine_m(
                lat, lon, float(place["lat"]), float(place["lon"])
            )
            if straight_m > _BEACH_CANDIDATE_RADIUS_M:
                continue
            place["straight_m"] = int(round(straight_m))
            places.append(place)
            if len(places) >= _BEACH_MAX_CANDIDATES:
                break

        cache_enrichment_data(
            lat,
            lon,
            cache_type,
            {"places": places, "total_found": total_found},
            timeout=_PLACES_CACHE_TTL,
        )
        return BeachLookup(places=places, total_found=total_found)

    @staticmethod
    def _beaches_payload(
        lookup: "BeachLookup", measured: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Fold the beach lookup and its distances into one stored record.

        The four statuses keep apart what the page must not confuse: beaches
        within the limit (`ok`), beaches that exist but are all too far
        (`none_within_limit`), Google answering that there are none at all
        (`not_found`), and no answer at all (`unavailable`). Only the first two
        are measured facts; a refusal must never render as "no beach nearby".
        """
        limit_min = _beach_limit_min()
        base: Dict[str, Any] = {
            "max_drive_min": limit_min,
            "mode": _BEACH_MODE,
            "search_radius_m": _BEACH_CANDIDATE_RADIUS_M,
            "candidates": len(lookup.places),
            "found": lookup.total_found,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if lookup.failure is not None:
            return {
                **base,
                "status": BEACHES_STATUS_UNAVAILABLE,
                "stage": STAGE_PLACES,
                "error": lookup.failure.reason,
                "items": [],
            }

        items: List[Dict[str, Any]] = []
        unmeasured = 0
        for entry in measured.values():
            place = entry.get("place") or {}
            status = entry.get("status")
            if status == TARGET_STATUS_UNAVAILABLE:
                unmeasured += 1
                continue
            duration_min = entry.get("duration_min")
            if duration_min is None or duration_min > limit_min:
                continue
            items.append(
                {
                    "name": place.get("name"),
                    "place_id": place.get("place_id"),
                    "lat": place.get("lat"),
                    "lon": place.get("lon"),
                    "duration_min": duration_min,
                    "distance_m": entry.get("distance_m"),
                    "distance_km": entry.get("distance_km"),
                    "estimated": bool(entry.get("estimated")),
                }
            )

        items.sort(key=lambda item: (item["duration_min"], item.get("distance_m") or 0))

        # Google holds the same beach under several place ids -- a live lookup
        # off La Caridad returned "Playa de Torbas" twice, 5 minutes apart --
        # and a name repeated down the block reads as two places to go to. The
        # nearest of each name survives, which is the one the sort put first.
        deduped: List[Dict[str, Any]] = []
        seen_names = set()
        for item in items:
            name_key = str(item.get("name") or "").strip().casefold()
            if name_key and name_key in seen_names:
                continue
            if name_key:
                seen_names.add(name_key)
            deduped.append(item)
        items = deduped

        if items:
            status = BEACHES_STATUS_OK
        elif unmeasured:
            # Every candidate that could have qualified went unmeasured, so
            # "none within the limit" is not something this run may claim.
            status = BEACHES_STATUS_UNAVAILABLE
        elif lookup.total_found:
            status = BEACHES_STATUS_NONE_WITHIN_LIMIT
        else:
            status = BEACHES_STATUS_NOT_FOUND

        payload = {**base, "status": status, "items": items}
        if unmeasured:
            payload["unmeasured"] = unmeasured
            payload["stage"] = STAGE_DISTANCE_MATRIX
        return payload

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
            duration_s=estimate_duration_seconds(distance_m, mode=mode),
            estimated=True,
        )

    def measure_drive_minutes(
        self, lat: float, lon: float, points: List[tuple]
    ) -> List[Dict[str, Any]]:
        """Drive minutes to a few coordinate pairs (proposal D17's pool path).

        Returns one `{"minutes": int|None, "refused": bool}` per point, and
        that shape is the #98 rule in miniature: Google answering
        ZERO_RESULTS (no road route) is a *measurement* with no minutes,
        while a refused request is `refused=True` — collapsing both to None
        would make an unreachable pool look like an unanswered call and
        re-bill it on every rerun. Answers cache 7 days; a batch containing
        any refusal is never cached.
        """
        if not points:
            return []
        destinations = [f"{float(p[0])},{float(p[1])}" for p in points]
        cache_key = (
            "drive_minutes_v2:"
            + hashlib.md5("|".join(destinations).encode()).hexdigest()[:10]
        )
        cached = get_cached_enrichment_data(lat, lon, cache_key)
        if isinstance(cached, list) and len(cached) == len(destinations):
            return cached

        results = self._distance_matrix_batch(lat, lon, destinations, mode="driving")
        readings: List[Dict[str, Any]] = []
        for result in results:
            if result.failure is not None:
                readings.append({"minutes": None, "refused": True})
            elif result.duration_s is None:
                readings.append({"minutes": None, "refused": False})
            else:
                readings.append(
                    {"minutes": int(round(result.duration_s / 60.0)), "refused": False}
                )
        if not any(reading["refused"] for reading in readings):
            try:
                cache_enrichment_data(
                    lat, lon, cache_key, readings, timeout=60 * 60 * 24 * 7
                )
            except Exception:
                logger.warning("Could not cache drive minutes", exc_info=True)
        return readings

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
