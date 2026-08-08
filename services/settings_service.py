import logging
from typing import Any, Dict, List, Optional

from app import db
from models import AppSetting

logger = logging.getLogger(__name__)


REFERENCE_CITIES_KEY = "reference_cities"
PROPERTY_CLASSIFICATION_RULES_KEY = "property_classification_rules"
TRAVEL_TARGETS_KEY = "travel_targets"
AI_MARKET_CONTEXT_KEY = "ai_market_context"
SALE_ONLY_KEY = "sale_only"
EXCLUDED_PROPERTY_CATEGORIES_KEY = "excluded_property_categories"


DEFAULT_REFERENCE_CITIES: List[Dict[str, Any]] = [
    {
        "name": "Madrid",
        "lat": 40.4168,
        "lon": -3.7038,
    },
    {
        "name": "Barcelona",
        "lat": 41.3851,
        "lon": 2.1734,
    },
]

DEFAULT_PROPERTY_CLASSIFICATION_RULES: List[Dict[str, Any]] = [
    # Housing
    {
        "category": "housing",
        "subtype": "penthouse",
        "pattern": r"\b(ático|atico|penthouse)\b",
        "priority": 100,
    },
    {
        "category": "housing",
        "subtype": "duplex",
        "pattern": r"\b(dúplex|duplex)\b",
        "priority": 95,
    },
    {
        "category": "housing",
        "subtype": "house",
        "pattern": r"\b(casa|chalet|vivienda|house|villa|adosado|pareado|bungalow)\b",
        "priority": 90,
    },
    {
        "category": "housing",
        "subtype": "apartment",
        "pattern": r"\b(piso|apartamento|apartment|flat|estudio|studio|loft)\b",
        "priority": 80,
    },
    # Land
    {
        "category": "land",
        "subtype": "plot",
        # "suelo" is ambiguous ("floor" vs "land"); keep it contextual to reduce false positives.
        "pattern": r"\b(terreno|parcela|plot|land|suelo\s+(?:en\s+venta|urbanizable|rústico|rustico)|solar\s+(?:urbano|en\s+venta)|finca\s+(?:rústica|rustica|en\s+venta))\b",
        "priority": 70,
    },
    # Garage / storage
    {
        "category": "garage",
        "subtype": "storage",
        "pattern": r"\b(trastero|storage)\b",
        "priority": 60,
    },
    {
        "category": "garage",
        "subtype": "garage",
        "pattern": r"\b(garaje|garage|parking|plaza\s+de\s+garaje)\b",
        "priority": 55,
    },
    # Commercial
    {
        "category": "commercial",
        "subtype": "office",
        "pattern": r"\b(oficina|office)\b",
        "priority": 50,
    },
    {
        "category": "commercial",
        "subtype": "industrial",
        "pattern": r"\b(nave|industrial|warehouse|almac[eé]n|almacen)\b",
        "priority": 45,
    },
    {
        "category": "commercial",
        "subtype": "retail",
        # "local" is ambiguous in Spanish; prefer explicit commercial phrases.
        "pattern": r"\b(local\s+comercial|local\s+en\s+venta|commercial\s+premises|shop|retail)\b",
        "priority": 40,
    },
    # Building
    {
        "category": "building",
        "subtype": "building",
        "pattern": r"\b(edificio|building|bloque)\b",
        "priority": 35,
    },
    # New development (kept for later; can be disabled by users)
    {
        "category": "new_development",
        "subtype": "obra_nueva",
        "pattern": r"\b(obra\s+nueva|promoción|promocion|new\s+development)\b",
        "priority": 10,
    },
]

DEFAULT_TRAVEL_TARGETS: List[Dict[str, Any]] = []

# Generic default. Profiles can override via SearchProfile.ai_config.market_context.
DEFAULT_AI_MARKET_CONTEXT = """
SPAIN REAL ESTATE MARKET CONTEXT (generic, configurable):

- Geography: Spain (sale listings).
- Taxes/fees vary by autonomous community and deal specifics; prefer configured assumptions when available.
- Prefer provided MARKET DATA / CONSTRUCTION ESTIMATES; if missing, state uncertainty instead of inventing numbers.
- Write in English, be concise, and focus on actionable investment and risk insights.
""".strip()


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


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_classification_rule(rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    category = (rule.get("category") or "").strip()
    subtype = (rule.get("subtype") or "").strip()
    pattern = (rule.get("pattern") or "").strip()
    priority = _coerce_int(rule.get("priority"))

    if not category or not subtype or not pattern:
        return None
    if priority is None:
        priority = 0

    return {
        "category": category,
        "subtype": subtype,
        "pattern": pattern,
        "priority": priority,
    }


def _validate_travel_target(target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = (target.get("name") or "").strip()
    lat = _coerce_float(target.get("lat"))
    lon = _coerce_float(target.get("lon"))
    mode = (target.get("mode") or "driving").strip()

    if not name:
        return None
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    allowed_modes = {"driving", "walking", "transit", "bicycling"}
    if mode not in allowed_modes:
        mode = "driving"

    return {"name": name, "lat": lat, "lon": lon, "mode": mode}


def _normalize_excluded_categories(value: Any) -> List[str]:
    if value is None:
        return []

    raw_items: List[str] = []
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw_items = [str(part).strip() for part in value]
    else:
        return []

    cleaned: List[str] = []
    seen = set()
    for item in raw_items:
        if not item:
            continue
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            cleaned.append(key)

    cleaned.sort()
    return cleaned


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
                setting = AppSetting(
                    key=REFERENCE_CITIES_KEY,
                    value=[dict(c) for c in DEFAULT_REFERENCE_CITIES],
                )
                db.session.add(setting)
            else:
                setting.value = [dict(c) for c in DEFAULT_REFERENCE_CITIES]

            db.session.commit()
        except Exception as e:
            logger.warning("Failed to ensure default reference cities: %s", e)

    @staticmethod
    def get_sale_only() -> bool:
        """Sale-only ingestion flag (DB override, fallback to env)."""
        try:
            setting = AppSetting.query.filter_by(key=SALE_ONLY_KEY).first()
            if setting and isinstance(setting.value, bool):
                return bool(setting.value)
        except Exception:
            pass

        try:
            from config import Config

            return bool(getattr(Config, "SALE_ONLY", True))
        except Exception:
            return True

    @staticmethod
    def set_sale_only(value: bool) -> None:
        setting = AppSetting.query.filter_by(key=SALE_ONLY_KEY).first()
        if not setting:
            setting = AppSetting(key=SALE_ONLY_KEY, value=bool(value))
            db.session.add(setting)
        else:
            setting.value = bool(value)
        db.session.commit()

    @staticmethod
    def get_excluded_property_categories() -> List[str]:
        """Excluded categories for ingestion (DB override, fallback to env)."""
        try:
            setting = AppSetting.query.filter_by(
                key=EXCLUDED_PROPERTY_CATEGORIES_KEY
            ).first()
            if setting:
                return _normalize_excluded_categories(setting.value)
        except Exception:
            pass

        try:
            from config import Config

            env_value = list(
                getattr(Config, "EXCLUDED_PROPERTY_CATEGORIES", set()) or []
            )
            return _normalize_excluded_categories(env_value)
        except Exception:
            return []

    @staticmethod
    def set_excluded_property_categories(categories: List[str]) -> None:
        normalized = _normalize_excluded_categories(categories)
        setting = AppSetting.query.filter_by(
            key=EXCLUDED_PROPERTY_CATEGORIES_KEY
        ).first()
        if not setting:
            setting = AppSetting(key=EXCLUDED_PROPERTY_CATEGORIES_KEY, value=normalized)
            db.session.add(setting)
        else:
            setting.value = normalized
        db.session.commit()

    @staticmethod
    def get_property_classification_rules() -> List[Dict[str, Any]]:
        """Return ordered regex-based classification rules (highest priority first)."""
        setting = AppSetting.query.filter_by(
            key=PROPERTY_CLASSIFICATION_RULES_KEY
        ).first()
        if not setting or not isinstance(setting.value, list):
            SettingsService._ensure_default_property_classification_rules()
            rules = [dict(r) for r in DEFAULT_PROPERTY_CLASSIFICATION_RULES]
        else:
            rules = []
            for item in setting.value:
                if isinstance(item, dict):
                    valid = _validate_classification_rule(item)
                    if valid:
                        rules.append(valid)

            if not rules:
                SettingsService._ensure_default_property_classification_rules()
                rules = [dict(r) for r in DEFAULT_PROPERTY_CLASSIFICATION_RULES]

        rules.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
        return rules

    @staticmethod
    def set_property_classification_rules(rules: List[Dict[str, Any]]) -> None:
        validated: List[Dict[str, Any]] = []
        for item in rules:
            if isinstance(item, dict):
                valid = _validate_classification_rule(item)
                if valid:
                    validated.append(valid)

        setting = AppSetting.query.filter_by(
            key=PROPERTY_CLASSIFICATION_RULES_KEY
        ).first()
        if not setting:
            setting = AppSetting(key=PROPERTY_CLASSIFICATION_RULES_KEY, value=validated)
            db.session.add(setting)
        else:
            setting.value = validated

        db.session.commit()

    @staticmethod
    def _ensure_default_property_classification_rules() -> None:
        try:
            setting = AppSetting.query.filter_by(
                key=PROPERTY_CLASSIFICATION_RULES_KEY
            ).first()
            if setting and isinstance(setting.value, list):
                return

            if not setting:
                setting = AppSetting(
                    key=PROPERTY_CLASSIFICATION_RULES_KEY,
                    value=[dict(r) for r in DEFAULT_PROPERTY_CLASSIFICATION_RULES],
                )
                db.session.add(setting)
            else:
                setting.value = [dict(r) for r in DEFAULT_PROPERTY_CLASSIFICATION_RULES]

            db.session.commit()
        except Exception as e:
            logger.warning(
                "Failed to ensure default property classification rules: %s", e
            )

    @staticmethod
    def get_travel_targets() -> List[Dict[str, Any]]:
        """Return user-defined travel targets (can be empty)."""
        setting = AppSetting.query.filter_by(key=TRAVEL_TARGETS_KEY).first()
        if not setting or not isinstance(setting.value, list):
            SettingsService._ensure_default_travel_targets()
            return [dict(t) for t in DEFAULT_TRAVEL_TARGETS]

        targets: List[Dict[str, Any]] = []
        for item in setting.value:
            if isinstance(item, dict):
                valid = _validate_travel_target(item)
                if valid:
                    targets.append(valid)

        return targets

    @staticmethod
    def set_travel_targets(targets: List[Dict[str, Any]]) -> None:
        validated: List[Dict[str, Any]] = []
        for item in targets:
            if isinstance(item, dict):
                valid = _validate_travel_target(item)
                if valid:
                    validated.append(valid)

        setting = AppSetting.query.filter_by(key=TRAVEL_TARGETS_KEY).first()
        if not setting:
            setting = AppSetting(key=TRAVEL_TARGETS_KEY, value=validated)
            db.session.add(setting)
        else:
            setting.value = validated

        db.session.commit()

    @staticmethod
    def _ensure_default_travel_targets() -> None:
        try:
            setting = AppSetting.query.filter_by(key=TRAVEL_TARGETS_KEY).first()
            if setting and isinstance(setting.value, list):
                return

            if not setting:
                setting = AppSetting(
                    key=TRAVEL_TARGETS_KEY,
                    value=[dict(t) for t in DEFAULT_TRAVEL_TARGETS],
                )
                db.session.add(setting)
            else:
                setting.value = [dict(t) for t in DEFAULT_TRAVEL_TARGETS]

            db.session.commit()
        except Exception as e:
            logger.warning("Failed to ensure default travel targets: %s", e)

    @staticmethod
    def get_ai_market_context() -> str:
        """Return the default AI market context text (profiles may override)."""
        setting = AppSetting.query.filter_by(key=AI_MARKET_CONTEXT_KEY).first()
        if (
            not setting
            or not isinstance(setting.value, str)
            or not setting.value.strip()
        ):
            SettingsService._ensure_default_ai_market_context()
            return DEFAULT_AI_MARKET_CONTEXT
        return setting.value.strip()

    @staticmethod
    def set_ai_market_context(text: str) -> None:
        cleaned = str(text or "").strip()
        # Keep the context reasonably bounded to avoid accidental huge blobs.
        cleaned = cleaned[:20000]

        setting = AppSetting.query.filter_by(key=AI_MARKET_CONTEXT_KEY).first()
        if not setting:
            setting = AppSetting(key=AI_MARKET_CONTEXT_KEY, value=cleaned)
            db.session.add(setting)
        else:
            setting.value = cleaned
        db.session.commit()

    @staticmethod
    def _ensure_default_ai_market_context() -> None:
        try:
            setting = AppSetting.query.filter_by(key=AI_MARKET_CONTEXT_KEY).first()
            if setting and isinstance(setting.value, str) and setting.value.strip():
                return

            if not setting:
                setting = AppSetting(
                    key=AI_MARKET_CONTEXT_KEY, value=DEFAULT_AI_MARKET_CONTEXT
                )
                db.session.add(setting)
            else:
                setting.value = DEFAULT_AI_MARKET_CONTEXT

            db.session.commit()
        except Exception as e:
            logger.warning("Failed to ensure default AI market context: %s", e)
