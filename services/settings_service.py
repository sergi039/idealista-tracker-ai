import logging
from typing import Any, Dict, List, Optional

from app import db
from models import AppSetting

logger = logging.getLogger(__name__)


REFERENCE_CITIES_KEY = "reference_cities"


DEFAULT_REFERENCE_CITIES: List[Dict[str, Any]] = [
    {
        "name": "Oviedo",
        "lat": 43.3614,
        "lon": -5.8593,
    },
    {
        "name": "Gijón",
        "lat": 43.5322,
        "lon": -5.6611,
    },
]


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_city(city: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = (city.get("name") or "").strip()
    lat = _coerce_float(city.get("lat"))
    lon = _coerce_float(city.get("lon"))

    if not name:
        return None
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    return {"name": name, "lat": lat, "lon": lon}


class SettingsService:
    @staticmethod
    def get_reference_cities() -> List[Dict[str, Any]]:
        """Return the ordered list of reference cities (at least 2)."""
        setting = AppSetting.query.filter_by(key=REFERENCE_CITIES_KEY).first()
        if not setting or not isinstance(setting.value, list):
            SettingsService._ensure_default_reference_cities()
            return [dict(c) for c in DEFAULT_REFERENCE_CITIES]

        # Backward compatible: older versions stored city slots city_a/city_b.
        by_slot: Dict[str, Dict[str, Any]] = {}
        ordered: List[Dict[str, Any]] = []
        for item in setting.value:
            if isinstance(item, dict):
                if "slot" in item:
                    valid = _validate_city(item)
                    slot = item.get("slot")
                    if valid and slot in ("city_a", "city_b"):
                        by_slot[slot] = valid
                else:
                    valid = _validate_city(item)
                    if valid:
                        ordered.append(valid)

        if by_slot:
            if "city_a" in by_slot and "city_b" in by_slot:
                ordered = [by_slot["city_a"], by_slot["city_b"]]
            else:
                ordered = []

        if len(ordered) < 2:
            SettingsService._ensure_default_reference_cities()
            return [dict(c) for c in DEFAULT_REFERENCE_CITIES]

        return ordered

    @staticmethod
    def set_reference_cities(cities: List[Dict[str, Any]]) -> None:
        validated: List[Dict[str, Any]] = []
        for item in cities:
            if isinstance(item, dict):
                valid = _validate_city(item)
                if valid:
                    validated.append(valid)

        if len(validated) < 2:
            raise ValueError("At least 2 reference cities are required.")

        setting = AppSetting.query.filter_by(key=REFERENCE_CITIES_KEY).first()
        if not setting:
            setting = AppSetting(key=REFERENCE_CITIES_KEY, value=validated)
            db.session.add(setting)
        else:
            setting.value = validated

        db.session.commit()

    @staticmethod
    def set_reference_city_names(city_names: List[str]) -> None:
        """Set reference cities by selecting from the city registry by name."""
        from utils.city_registry import resolve_city

        resolved: List[Dict[str, Any]] = []
        for name in city_names:
            city = resolve_city(name)
            if city:
                resolved.append({"name": city.name, "lat": city.lat, "lon": city.lon})

        if len(resolved) < 2:
            raise ValueError("Pick at least 2 cities from the list.")

        SettingsService.set_reference_cities(resolved)

    @staticmethod
    def _ensure_default_reference_cities() -> None:
        try:
            setting = AppSetting.query.filter_by(key=REFERENCE_CITIES_KEY).first()
            if setting and isinstance(setting.value, list):
                return

            if not setting:
                setting = AppSetting(key=REFERENCE_CITIES_KEY, value=[dict(c) for c in DEFAULT_REFERENCE_CITIES])
                db.session.add(setting)
            else:
                setting.value = [dict(c) for c in DEFAULT_REFERENCE_CITIES]

            db.session.commit()
        except Exception as e:
            logger.warning("Failed to ensure default reference cities: %s", e)
