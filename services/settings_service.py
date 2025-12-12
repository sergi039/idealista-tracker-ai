import logging
from typing import Any, Dict, List, Optional

from app import db
from models import AppSetting

logger = logging.getLogger(__name__)


REFERENCE_CITIES_KEY = "reference_cities"


DEFAULT_REFERENCE_CITIES: List[Dict[str, Any]] = [
    {
        "slot": "city_a",
        "name": "Oviedo",
        "lat": 43.3614,
        "lon": -5.8593,
    },
    {
        "slot": "city_b",
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
    slot = city.get("slot")
    name = (city.get("name") or "").strip()
    lat = _coerce_float(city.get("lat"))
    lon = _coerce_float(city.get("lon"))

    if slot not in ("city_a", "city_b"):
        return None
    if not name:
        return None
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    return {"slot": slot, "name": name, "lat": lat, "lon": lon}


class SettingsService:
    @staticmethod
    def get_reference_cities() -> List[Dict[str, Any]]:
        """Return the two city slots used for travel-time scoring and display."""
        setting = AppSetting.query.filter_by(key=REFERENCE_CITIES_KEY).first()
        if not setting or not isinstance(setting.value, list):
            SettingsService._ensure_default_reference_cities()
            return [dict(c) for c in DEFAULT_REFERENCE_CITIES]

        validated: Dict[str, Dict[str, Any]] = {}
        for item in setting.value:
            if isinstance(item, dict):
                valid = _validate_city(item)
                if valid:
                    validated[valid["slot"]] = valid

        if "city_a" not in validated or "city_b" not in validated:
            SettingsService._ensure_default_reference_cities()
            return [dict(c) for c in DEFAULT_REFERENCE_CITIES]

        return [validated["city_a"], validated["city_b"]]

    @staticmethod
    def set_reference_cities(cities: List[Dict[str, Any]]) -> None:
        validated: Dict[str, Dict[str, Any]] = {}
        for item in cities:
            if not isinstance(item, dict):
                continue
            valid = _validate_city(item)
            if valid:
                validated[valid["slot"]] = valid

        if "city_a" not in validated or "city_b" not in validated:
            raise ValueError("Both city slots (city_a, city_b) must be provided with valid name/lat/lon.")

        ordered = [validated["city_a"], validated["city_b"]]

        setting = AppSetting.query.filter_by(key=REFERENCE_CITIES_KEY).first()
        if not setting:
            setting = AppSetting(key=REFERENCE_CITIES_KEY, value=ordered)
            db.session.add(setting)
        else:
            setting.value = ordered

        db.session.commit()

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
