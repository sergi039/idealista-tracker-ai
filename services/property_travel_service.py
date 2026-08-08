import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app import db
from config import Config
from models import Property, SearchProfile
from services.property_location_service import PropertyLocationService
from services.search_profile_service import SearchProfileService
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from utils.http import request_with_retries

logger = logging.getLogger(__name__)


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
                if not enabled:
                    targets[preset_key] = {
                        "kind": "preset",
                        "enabled": False,
                        "mode": mode,
                        "label": preset_defs[preset_key].get("label"),
                    }
                    continue

                place = self._nearest_place_for_preset(
                    origin_lat, origin_lon, preset_key, preset_defs[preset_key]
                )
                if not place:
                    targets[preset_key] = {
                        "kind": "preset",
                        "enabled": True,
                        "mode": mode,
                        "label": preset_defs[preset_key].get("label"),
                        "status": "not_found"
                        if self.google_places_key
                        else "no_places_key",
                    }
                    continue

                dest_key = preset_key
                destinations.append(
                    {
                        "key": dest_key,
                        "mode": mode,
                        "lat": place["lat"],
                        "lon": place["lon"],
                    }
                )
                targets[preset_key] = {
                    "kind": "preset",
                    "enabled": True,
                    "mode": mode,
                    "label": preset_defs[preset_key].get("label"),
                    "place": place,
                }

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
                target_key = entry["key"]
                if not res:
                    targets.setdefault(target_key, {})
                    targets[target_key]["status"] = "distance_unavailable"
                    continue
                targets.setdefault(target_key, {})
                targets[target_key].update(
                    {
                        "distance_m": res.get("distance_m"),
                        "distance_km": (res.get("distance_m") or 0) / 1000.0
                        if res.get("distance_m")
                        else None,
                        "duration_s": res.get("duration_s"),
                        "duration_min": int(round((res.get("duration_s") or 0) / 60.0))
                        if res.get("duration_s")
                        else None,
                    }
                )

        travel = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "origin": {"lat": origin_lat, "lon": origin_lon},
            "profile_id": profile.id if profile else None,
            "targets": targets,
        }

        prop.travel = travel

        if commit:
            db.session.commit()

        return True

    def calculate_for_property_id(self, property_id: int, commit: bool = True) -> bool:
        prop = db.session.get(Property, property_id)
        if not prop:
            return False
        return self.calculate_for_property(prop, commit=commit)

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
    ) -> Optional[Dict[str, Any]]:
        place_types = (
            preset_def.get("place_types") if isinstance(preset_def, dict) else None
        )
        if not isinstance(place_types, list) or not place_types:
            return None

        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for place_type in place_types:
            place = self._nearest_place(lat, lon, place_type=str(place_type))
            if not place:
                continue
            distance_m = _haversine_m(
                lat, lon, float(place["lat"]), float(place["lon"])
            )
            if best is None or distance_m < best[0]:
                best = (distance_m, place)

        if not best:
            return None
        best_place = dict(best[1])
        best_place["preset_key"] = preset_key
        return best_place

    def _nearest_place(
        self, lat: float, lon: float, place_type: str
    ) -> Optional[Dict[str, Any]]:
        place_type = (place_type or "").strip()
        if not place_type:
            return None

        cache_type = f"places_nearest_v1:{place_type}"
        cached = get_cached_enrichment_data(lat, lon, cache_type)
        if (
            isinstance(cached, dict)
            and cached.get("lat") is not None
            and cached.get("lon") is not None
        ):
            return cached

        if not self.google_places_key:
            return None

        try:
            url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
            params = {
                "location": f"{lat},{lon}",
                "rankby": "distance",
                "type": place_type,
                "key": self.google_places_key,
            }
            response = request_with_retries(
                requests.get, url, params=params, timeout=12, logger=logger
            )
            if response.status_code != 200:
                return None
            data = response.json()
            if data.get("status") != "OK":
                return None
            results = data.get("results") or []
            if not results:
                return None

            first = results[0]
            loc = (first.get("geometry") or {}).get("location") or {}
            out = {
                "name": first.get("name"),
                "place_id": first.get("place_id"),
                "types": first.get("types"),
                "lat": loc.get("lat"),
                "lon": loc.get("lng"),
            }
            if out.get("lat") is None or out.get("lon") is None:
                return None

            cache_enrichment_data(lat, lon, cache_type, out, timeout=60 * 60 * 24 * 7)
            return out
        except Exception as e:
            logger.warning("Places lookup failed (%s): %s", place_type, e)
            return None

    def _get_distances(
        self, lat: float, lon: float, destinations: List[Dict[str, Any]], mode: str
    ) -> List[Optional[Dict[str, Any]]]:
        if not destinations:
            return []

        mode = (mode or "driving").lower()

        dest_sig = "|".join(
            f"{d.get('lat')},{d.get('lon')}:{mode}" for d in destinations
        )
        dest_hash = hashlib.md5(dest_sig.encode()).hexdigest()[:10]
        cache_type = f"property_travel_v1:{mode}:{dest_hash}"

        cached = get_cached_enrichment_data(lat, lon, cache_type)
        if isinstance(cached, list) and len(cached) == len(destinations):
            return cached

        if not self.google_maps_key:
            out: List[Optional[Dict[str, Any]]] = []
            for d in destinations:
                try:
                    dlat = float(d.get("lat"))
                    dlon = float(d.get("lon"))
                except Exception:
                    out.append(None)
                    continue
                distance_m = int(round(_haversine_m(lat, lon, dlat, dlon)))
                out.append(
                    {
                        "distance_m": distance_m,
                        "duration_s": _estimate_duration_seconds(distance_m, mode=mode),
                    }
                )
            cache_enrichment_data(lat, lon, cache_type, out, timeout=60 * 60 * 24 * 3)
            return out

        # Distance Matrix allows <=25 destinations per request.
        out: List[Optional[Dict[str, Any]]] = []
        dest_coords = [f"{d.get('lat')},{d.get('lon')}" for d in destinations]

        for chunk_start in range(0, len(dest_coords), 25):
            chunk = dest_coords[chunk_start : chunk_start + 25]
            chunk_results = self._distance_matrix_batch(lat, lon, chunk, mode=mode)
            out.extend(chunk_results)

        while len(out) < len(destinations):
            out.append(None)

        cache_enrichment_data(lat, lon, cache_type, out, timeout=60 * 60 * 24 * 7)
        return out

    def _distance_matrix_batch(
        self, lat: float, lon: float, destinations: List[str], mode: str
    ) -> List[Optional[Dict[str, Any]]]:
        if not destinations:
            return []
        try:
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                "origins": f"{lat},{lon}",
                "destinations": "|".join(destinations),
                "mode": mode,
                "key": self.google_maps_key,
            }
            response = request_with_retries(
                requests.get, url, params=params, timeout=15, logger=logger
            )
            if response.status_code != 200:
                return [None for _ in destinations]
            data = response.json()
            if not data.get("rows") or not data["rows"][0].get("elements"):
                return [None for _ in destinations]
            elements = data["rows"][0]["elements"]
            results: List[Optional[Dict[str, Any]]] = []
            for el in elements[: len(destinations)]:
                if el.get("status") != "OK":
                    results.append(None)
                    continue
                results.append(
                    {
                        "distance_m": el.get("distance", {}).get("value"),
                        "duration_s": el.get("duration", {}).get("value"),
                    }
                )
            while len(results) < len(destinations):
                results.append(None)
            return results
        except Exception as e:
            logger.warning("Distance matrix failed: %s", e)
            return [None for _ in destinations]
