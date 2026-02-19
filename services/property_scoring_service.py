import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from app import db
from config import Config
from models import Property, SearchProfile
from services.search_profile_service import SearchProfileService

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _weighted_average(scores_and_weights: Dict[str, Tuple[Optional[float], float]]) -> Optional[float]:
    total_weight = 0.0
    weighted_sum = 0.0
    for _, (score, weight) in scores_and_weights.items():
        if score is None:
            continue
        if weight <= 0:
            continue
        total_weight += weight
        weighted_sum += score * weight
    if total_weight <= 0:
        return None
    return weighted_sum / total_weight


def _percentile_score_lower_is_better(value: float, values: list[float]) -> Optional[float]:
    """Return 0-100 where lower `value` is better compared to `values`."""
    if value is None:
        return None
    cleaned = [v for v in values if v is not None]
    if len(cleaned) < 3:
        return None
    cleaned.sort()
    # rank = fraction of values <= value; lower better => invert.
    count_le = 0
    for v in cleaned:
        if v <= value:
            count_le += 1
        else:
            break
    pct = count_le / len(cleaned)
    score = (1.0 - pct) * 100.0
    return _clamp(score)


def _linear_minutes_score(minutes: Optional[float], best: float, worst: float) -> Optional[float]:
    if minutes is None:
        return None
    if worst <= best:
        return None
    if minutes <= best:
        return 100.0
    if minutes >= worst:
        return 0.0
    ratio = (worst - minutes) / (worst - best)
    return _clamp(ratio * 100.0)


@dataclass(frozen=True)
class PropertyScoreResult:
    investment: Optional[float]
    lifestyle: Optional[float]
    combined: Optional[float]
    scoring_payload: Dict[str, Any]


class BasePropertyScorer:
    category: str = "unknown"

    def calculate(self, prop: Property, profile: Optional[SearchProfile]) -> PropertyScoreResult:
        raise NotImplementedError


class HousingPropertyScorer(BasePropertyScorer):
    category = "housing"

    DEFAULT_INVESTMENT_WEIGHTS = {"value_score": 0.7, "travel_score": 0.3, "size_score": 0.0}
    DEFAULT_LIFESTYLE_WEIGHTS = {"travel_score": 0.6, "size_score": 0.4, "value_score": 0.0}
    DEFAULT_TRAVEL_MINUTES = {"best": 10.0, "worst": 60.0}

    def calculate(self, prop: Property, profile: Optional[SearchProfile]) -> PropertyScoreResult:
        scoring_config = (profile.scoring_config if profile and isinstance(profile.scoring_config, dict) else {}) or {}
        cat_cfg = (
            ((scoring_config.get("categories") or {}).get(self.category) if isinstance(scoring_config.get("categories"), dict) else None)
            or {}
        )

        investment_weights = dict(self.DEFAULT_INVESTMENT_WEIGHTS)
        lifestyle_weights = dict(self.DEFAULT_LIFESTYLE_WEIGHTS)
        travel_minutes_cfg = dict(self.DEFAULT_TRAVEL_MINUTES)

        if isinstance(cat_cfg, dict):
            inv = cat_cfg.get("investment")
            life = cat_cfg.get("lifestyle")
            travel_minutes = cat_cfg.get("travel_minutes")
            if isinstance(inv, dict):
                investment_weights.update({k: float(v) for k, v in inv.items() if v is not None})
            if isinstance(life, dict):
                lifestyle_weights.update({k: float(v) for k, v in life.items() if v is not None})
            if isinstance(travel_minutes, dict):
                if travel_minutes.get("best") is not None:
                    travel_minutes_cfg["best"] = float(travel_minutes.get("best"))
                if travel_minutes.get("worst") is not None:
                    travel_minutes_cfg["worst"] = float(travel_minutes.get("worst"))

        mix = getattr(Config, "COMBINED_MIX", {"investment": 0.32, "lifestyle": 0.68})
        if isinstance(cat_cfg, dict) and isinstance(cat_cfg.get("combined_mix"), dict):
            mix_override = cat_cfg.get("combined_mix") or {}
            if mix_override.get("investment") is not None and mix_override.get("lifestyle") is not None:
                mix = {"investment": float(mix_override["investment"]), "lifestyle": float(mix_override["lifestyle"])}

        value_score, value_meta = self._value_score(prop)
        size_score, size_meta = self._size_score(prop)
        travel_score, travel_meta = self._travel_score(prop, profile, best=travel_minutes_cfg["best"], worst=travel_minutes_cfg["worst"])

        investment = _weighted_average(
            {
                "value_score": (value_score, investment_weights.get("value_score", 0.0)),
                "travel_score": (travel_score, investment_weights.get("travel_score", 0.0)),
                "size_score": (size_score, investment_weights.get("size_score", 0.0)),
            }
        )
        lifestyle = _weighted_average(
            {
                "travel_score": (travel_score, lifestyle_weights.get("travel_score", 0.0)),
                "size_score": (size_score, lifestyle_weights.get("size_score", 0.0)),
                "value_score": (value_score, lifestyle_weights.get("value_score", 0.0)),
            }
        )

        combined = None
        if investment is not None or lifestyle is not None:
            combined = _weighted_average(
                {
                    "investment": (investment, float(mix.get("investment", 0.0) or 0.0)),
                    "lifestyle": (lifestyle, float(mix.get("lifestyle", 0.0) or 0.0)),
                }
            )

        payload: Dict[str, Any] = {
            "version": 1,
            "category": self.category,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "profiles": {
                "investment": {
                    "score": investment,
                    "weights": investment_weights,
                    "components": {
                        "value_score": value_score,
                        "travel_score": travel_score,
                        "size_score": size_score,
                    },
                },
                "lifestyle": {
                    "score": lifestyle,
                    "weights": lifestyle_weights,
                    "components": {
                        "travel_score": travel_score,
                        "size_score": size_score,
                        "value_score": value_score,
                    },
                },
            },
            "combined_mix": mix,
            "combined_score": combined,
            "details": {
                "value": value_meta,
                "size": size_meta,
                "travel": travel_meta,
            },
        }

        return PropertyScoreResult(investment=investment, lifestyle=lifestyle, combined=combined, scoring_payload=payload)

    def _value_score(self, prop: Property) -> Tuple[Optional[float], Dict[str, Any]]:
        price = _safe_float(prop.price)
        area = _safe_float(prop.area)
        meta: Dict[str, Any] = {"basis": "price_per_m2"}
        if price is None or area is None or area <= 0:
            return None, {**meta, "status": "missing_price_or_area"}

        ppm2 = price / area
        meta["price_per_m2"] = ppm2

        peer_ppm2, peer_meta = self._collect_peer_ppm2(prop, min_peers=3, limit=600)
        meta.update(peer_meta)
        score = _percentile_score_lower_is_better(ppm2, peer_ppm2)
        meta["peer_count"] = len(peer_ppm2)
        if score is None:
            return None, {**meta, "status": "insufficient_peers"}
        return score, {**meta, "status": "ok"}

    def _size_score(self, prop: Property) -> Tuple[Optional[float], Dict[str, Any]]:
        area = _safe_float(prop.area)
        if area is None or area <= 0:
            return None, {"status": "missing_area"}

        peer_areas, meta = self._collect_peer_areas(prop, min_peers=3, limit=600)

        # For size, larger is (usually) better; compute percentile and use directly.
        if len(peer_areas) < 3:
            meta.update({"status": "insufficient_peers", "peer_count": len(peer_areas)})
            return None, meta
        peer_areas.sort()
        count_le = 0
        for v in peer_areas:
            if v <= area:
                count_le += 1
            else:
                break
        pct = count_le / len(peer_areas)
        score = _clamp(pct * 100.0)
        meta.update({"status": "ok", "peer_count": len(peer_areas), "area": area})
        return score, meta

    def _collect_peer_ppm2(self, prop: Property, *, min_peers: int, limit: int) -> Tuple[list[float], Dict[str, Any]]:
        """Collect peer price/m² values with progressive scope relaxation to avoid 'no peers' dead-ends."""
        scopes: list[tuple[str, Dict[str, bool]]] = []
        # Strict -> relaxed: municipality+subtype -> subtype -> category-only.
        if prop.municipality and prop.property_subtype:
            scopes.append(("municipality+subtype", {"municipality": True, "subtype": True}))
        if prop.property_subtype:
            scopes.append(("subtype", {"municipality": False, "subtype": True}))
        scopes.append(("category", {"municipality": False, "subtype": False}))

        best_values: list[float] = []
        best_meta: Dict[str, Any] = {"comparable_scope": None}

        for scope_name, cfg in scopes:
            q = Property.query
            if prop.search_profile_id is not None:
                q = q.filter(Property.search_profile_id == prop.search_profile_id)
            q = q.filter(Property.id != prop.id)
            q = q.filter(Property.property_category == self.category)
            if cfg.get("subtype") and prop.property_subtype:
                q = q.filter(Property.property_subtype == prop.property_subtype)
            if cfg.get("municipality") and prop.municipality:
                q = q.filter(Property.municipality == prop.municipality)
            q = q.filter(Property.price.isnot(None), Property.area.isnot(None), Property.area > 0)

            peers = q.limit(limit).all()
            values: list[float] = []
            for p in peers:
                p_price = _safe_float(p.price)
                p_area = _safe_float(p.area)
                if p_price is None or p_area is None or p_area <= 0:
                    continue
                values.append(p_price / p_area)

            if len(values) > len(best_values):
                best_values = values
                best_meta = {"comparable_scope": scope_name}

            if len(values) >= min_peers:
                return values, {"comparable_scope": scope_name}

        return best_values, best_meta

    def _collect_peer_areas(self, prop: Property, *, min_peers: int, limit: int) -> Tuple[list[float], Dict[str, Any]]:
        scopes: list[tuple[str, Dict[str, bool]]] = []
        if prop.municipality and prop.property_subtype:
            scopes.append(("municipality+subtype", {"municipality": True, "subtype": True}))
        if prop.property_subtype:
            scopes.append(("subtype", {"municipality": False, "subtype": True}))
        scopes.append(("category", {"municipality": False, "subtype": False}))

        best_values: list[float] = []
        best_meta: Dict[str, Any] = {"comparable_scope": None}

        for scope_name, cfg in scopes:
            q = Property.query
            if prop.search_profile_id is not None:
                q = q.filter(Property.search_profile_id == prop.search_profile_id)
            q = q.filter(Property.id != prop.id)
            q = q.filter(Property.property_category == self.category)
            if cfg.get("subtype") and prop.property_subtype:
                q = q.filter(Property.property_subtype == prop.property_subtype)
            if cfg.get("municipality") and prop.municipality:
                q = q.filter(Property.municipality == prop.municipality)
            q = q.filter(Property.area.isnot(None), Property.area > 0)

            peers = q.limit(limit).all()
            values: list[float] = []
            for p in peers:
                p_area = _safe_float(p.area)
                if p_area is None or p_area <= 0:
                    continue
                values.append(p_area)

            if len(values) > len(best_values):
                best_values = values
                best_meta = {"comparable_scope": scope_name}

            if len(values) >= min_peers:
                return values, {"comparable_scope": scope_name}

        return best_values, best_meta

    def _travel_score(
        self,
        prop: Property,
        profile: Optional[SearchProfile],
        best: float,
        worst: float,
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        config = SearchProfileService.get_travel_targets_config(profile)
        travel = prop.travel if isinstance(prop.travel, dict) else {}
        targets = (travel.get("targets") if isinstance(travel, dict) else None) or {}

        enabled_keys: list[str] = []
        presets_cfg = config.get("presets") if isinstance(config, dict) else None
        if isinstance(presets_cfg, dict):
            for preset_key, preset_cfg in presets_cfg.items():
                if not isinstance(preset_cfg, dict):
                    continue
                if bool(preset_cfg.get("enabled", True)):
                    enabled_keys.append(preset_key)

        custom_cfg = config.get("custom") if isinstance(config, dict) else None
        if isinstance(custom_cfg, list):
            for item in custom_cfg:
                if not isinstance(item, dict):
                    continue
                raw_id = str(item.get("id") or "").strip()
                if raw_id:
                    enabled_keys.append(f"custom:{raw_id}")

        key_scores: Dict[str, Dict[str, Any]] = {}
        scores: list[float] = []
        for key in enabled_keys:
            t = targets.get(key)
            minutes = None
            if isinstance(t, dict):
                minutes = _safe_float(t.get("duration_min"))
            s = _linear_minutes_score(minutes, best=best, worst=worst)
            key_scores[key] = {"minutes": minutes, "score": s}
            if s is not None:
                scores.append(s)

        if not scores:
            return None, {"status": "missing_travel", "targets": key_scores, "best": best, "worst": worst}

        avg = sum(scores) / len(scores)
        return _clamp(avg), {"status": "ok", "targets": key_scores, "best": best, "worst": worst}


class LandPropertyScorer(HousingPropertyScorer):
    category = "land"

    DEFAULT_INVESTMENT_WEIGHTS = {"value_score": 0.8, "travel_score": 0.2, "size_score": 0.0}
    DEFAULT_LIFESTYLE_WEIGHTS = {"travel_score": 0.5, "size_score": 0.5, "value_score": 0.0}

class GaragePropertyScorer(HousingPropertyScorer):
    category = "garage"

    DEFAULT_INVESTMENT_WEIGHTS = {"value_score": 0.9, "travel_score": 0.1, "size_score": 0.0}
    DEFAULT_LIFESTYLE_WEIGHTS = {"travel_score": 0.7, "size_score": 0.3, "value_score": 0.0}


class CommercialPropertyScorer(HousingPropertyScorer):
    category = "commercial"

    DEFAULT_INVESTMENT_WEIGHTS = {"value_score": 0.85, "travel_score": 0.15, "size_score": 0.0}
    DEFAULT_LIFESTYLE_WEIGHTS = {"travel_score": 0.6, "size_score": 0.4, "value_score": 0.0}


class BuildingPropertyScorer(HousingPropertyScorer):
    category = "building"

    DEFAULT_INVESTMENT_WEIGHTS = {"value_score": 0.85, "travel_score": 0.15, "size_score": 0.0}
    DEFAULT_LIFESTYLE_WEIGHTS = {"travel_score": 0.6, "size_score": 0.4, "value_score": 0.0}


class NewDevelopmentPropertyScorer(HousingPropertyScorer):
    category = "new_development"

    DEFAULT_INVESTMENT_WEIGHTS = {"value_score": 0.7, "travel_score": 0.3, "size_score": 0.0}
    DEFAULT_LIFESTYLE_WEIGHTS = {"travel_score": 0.6, "size_score": 0.4, "value_score": 0.0}


class DefaultPropertyScorer(BasePropertyScorer):
    category = "default"

    def calculate(self, prop: Property, profile: Optional[SearchProfile]) -> PropertyScoreResult:
        payload = {
            "version": 1,
            "category": prop.property_category or "unknown",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "unsupported_category",
        }
        return PropertyScoreResult(investment=None, lifestyle=None, combined=None, scoring_payload=payload)


class PropertyScoringService:
    """Category-aware scoring for the universal Property model."""

    def __init__(self):
        self._scorers: Dict[str, BasePropertyScorer] = {
            "housing": HousingPropertyScorer(),
            "land": LandPropertyScorer(),
            "garage": GaragePropertyScorer(),
            "commercial": CommercialPropertyScorer(),
            "building": BuildingPropertyScorer(),
            "new_development": NewDevelopmentPropertyScorer(),
        }
        self._default = DefaultPropertyScorer()

    def scorer_for(self, prop: Property) -> BasePropertyScorer:
        category = (prop.property_category or "").strip().lower()
        return self._scorers.get(category) or self._default

    def calculate_for_property(self, prop: Property, commit: bool = False) -> bool:
        if not prop:
            return False

        profile: Optional[SearchProfile] = None
        try:
            profile = prop.search_profile if prop.search_profile else None
            if not profile and prop.search_profile_id:
                profile = db.session.get(SearchProfile, prop.search_profile_id)
        except Exception:
            profile = None

        scorer = self.scorer_for(prop)
        result = scorer.calculate(prop, profile)

        prop.score_investment = Decimal(str(result.investment)) if result.investment is not None else None
        prop.score_lifestyle = Decimal(str(result.lifestyle)) if result.lifestyle is not None else None
        prop.score_total = Decimal(str(result.combined)) if result.combined is not None else None
        prop.scoring = result.scoring_payload

        if commit:
            db.session.commit()
        return True

    def calculate_for_property_id(self, property_id: int, commit: bool = True) -> bool:
        prop = db.session.get(Property, property_id)
        if not prop:
            return False
        return self.calculate_for_property(prop, commit=commit)
