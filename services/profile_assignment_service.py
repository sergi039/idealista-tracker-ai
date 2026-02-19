import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app import db
from models import Property, SearchProfile
from services.search_profile_service import SearchProfileService

logger = logging.getLogger(__name__)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    r = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return r * c * 1000.0


class ProfileAssignmentService:
    """Assign properties to the nearest profile center (custom targets)."""

    def _get_profile_centers(self, profile: SearchProfile) -> List[Tuple[float, float, str]]:
        config = SearchProfileService.get_travel_targets_config(profile)
        custom = config.get("custom") if isinstance(config, dict) else []
        centers: List[Tuple[float, float, str]] = []
        for item in custom or []:
            if not isinstance(item, dict):
                continue
            try:
                lat = float(item.get("lat"))
                lon = float(item.get("lon"))
            except Exception:
                continue
            name = str(item.get("name") or "").strip() or "Custom target"
            centers.append((lat, lon, name))
        return centers

    def assign_nearest_profile(
        self,
        prop: Property,
        *,
        commit: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        if not prop:
            return {"assigned": False, "reason": "no_property"}

        if not (prop.location_lat and prop.location_lon):
            return {"assigned": False, "reason": "no_coordinates"}

        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        assignment_meta = enrichment.get("profile_assignment") if isinstance(enrichment.get("profile_assignment"), dict) else {}
        if assignment_meta.get("manual_override") and not force:
            return {"assigned": False, "reason": "manual_override", "profile_id": prop.search_profile_id}

        profiles = SearchProfileService.list_profiles(active_only=True)
        best_profile: Optional[SearchProfile] = None
        best_distance_m: Optional[float] = None
        best_center: Optional[str] = None

        try:
            lat = float(prop.location_lat)
            lon = float(prop.location_lon)
        except Exception:
            return {"assigned": False, "reason": "bad_coordinates"}

        for profile in profiles:
            centers = self._get_profile_centers(profile)
            if not centers:
                continue
            for center_lat, center_lon, center_name in centers:
                distance_m = _haversine_m(lat, lon, center_lat, center_lon)
                if best_distance_m is None or distance_m < best_distance_m:
                    best_distance_m = distance_m
                    best_profile = profile
                    best_center = center_name

        if not best_profile or best_distance_m is None:
            return {"assigned": False, "reason": "no_profile_centers"}

        if prop.search_profile_id == best_profile.id:
            return {
                "assigned": False,
                "reason": "already_assigned",
                "profile_id": prop.search_profile_id,
                "distance_km": round(best_distance_m / 1000.0, 2),
            }

        prop.search_profile_id = best_profile.id

        assignment_meta = {
            "method": "nearest_custom_target",
            "profile_id": best_profile.id,
            "profile_name": best_profile.name,
            "center_name": best_center,
            "distance_km": round(best_distance_m / 1000.0, 2),
            "assigned_at": datetime.now(timezone.utc).isoformat(),
            "manual_override": False,
        }
        enrichment["profile_assignment"] = assignment_meta
        prop.enrichment = enrichment

        if commit:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning("Failed to commit profile assignment for %s: %s", prop.id, e)

        return {
            "assigned": True,
            "reason": "assigned",
            "profile_id": best_profile.id,
            "profile_name": best_profile.name,
            "center_name": best_center,
            "distance_km": round(best_distance_m / 1000.0, 2),
        }
