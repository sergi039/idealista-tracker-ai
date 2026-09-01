import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional, Tuple

from app import db
from config import Config
from models import Property, SearchProfile
from services.coordinate_quality import coordinate_slack_m, normalize_accuracy
from services.property_comparables import collect_comparables
from services.sea_distance_service import (
    STATUS_APPROXIMATE_ORIGIN,
    STATUS_NO_COASTLINE,
    STATUS_OK,
    parcel_measurement,
)
from services.search_profile_service import SearchProfileService

logger = logging.getLogger(__name__)

# What counts as a comparable -- the scope ladder, `PEER_AREA_BAND_FACTOR` and
# #377's municipality key -- lives in `services/property_comparables.py`, with
# the measurements behind it. This scorer and the AI prompt are both consumers
# of that one answer (#386); neither owns it.


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _finite_float(value: Any) -> Optional[float]:
    """`_safe_float` that also rejects NaN and the infinities.

    Stored JSON reaches the sea component directly, and `float("nan")` slips
    through every comparison in the decay function to come out as a full score.
    """
    result = _safe_float(value)
    if result is None or not math.isfinite(result):
        return None
    return result


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _weighted_average(
    scores_and_weights: Dict[str, Tuple[Optional[float], float]],
) -> Optional[float]:
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


def _coverage(
    scores_and_weights: Dict[str, Tuple[Optional[float], float]],
) -> Tuple[float, int, int]:
    """(share of enabled weight that answered, measured count, enabled count).

    The same walk `_weighted_average` makes, with one more accumulator: it is
    the one place that knows both the values and the weights and already
    decides what to drop (a `None` score, a weight <= 0), so coverage is read
    off that decision rather than re-derived elsewhere. "Enabled" is weight > 0
    in the profile actually applied -- the pool criterion ships weightless and
    is not a hole on every row, and a subscription's own `scoring_config`
    weights are the ones that count. Weighted, not counted: 2 of 5 criteria can
    be 10% of the weight or 90%.
    """
    enabled_weight = 0.0
    measured_weight = 0.0
    enabled = 0
    measured = 0
    for _, (score, weight) in scores_and_weights.items():
        if weight <= 0:
            continue
        enabled += 1
        enabled_weight += weight
        if score is not None:
            measured += 1
            measured_weight += weight
    if enabled_weight <= 0:
        return 0.0, measured, enabled
    return measured_weight / enabled_weight, measured, enabled


def score_coverage(scoring: Any) -> Optional[Dict[str, Any]]:
    """Coverage for a stored `scoring` payload, derived when it was not recorded.

    #379: `/properties` sorts by `score_total`, and the top of the list was the
    listings the app knew least about -- a criterion nobody could measure scores
    `None`, the branch average renormalises without it, and a row with two
    perfect measured criteria out of five scored 100 while not one row with four
    or five measured criteria reached 90 (measured 2026-08-17). The number stays
    what it is -- the owner's decision is that an unmeasured criterion is shown,
    never invented (no neutral prior, no penalty; #98's rule one level up) --
    and the page says how much of the enabled weight the score rests on and
    which criteria are missing.

    Payloads written before this field are not rescored to gain it: the
    branches already store `weights` and `components` with their `None`s, so
    the same share is derived here. Returns None when there is no scoring at
    all. Shape: {"share", "measured", "enabled", "missing": [criterion, ...],
    "derived": bool}.
    """
    if not isinstance(scoring, dict):
        return None
    recorded = scoring.get("coverage")
    profiles = scoring.get("profiles")
    if not isinstance(profiles, dict):
        return None
    mix = (
        scoring.get("combined_mix")
        if isinstance(scoring.get("combined_mix"), dict)
        else {}
    )
    share_num = 0.0
    share_den = 0.0
    measured = 0
    enabled = 0
    missing: list = []
    for branch in ("investment", "lifestyle"):
        prof = profiles.get(branch)
        if not isinstance(prof, dict):
            continue
        weights = prof.get("weights") if isinstance(prof.get("weights"), dict) else {}
        components = (
            prof.get("components") if isinstance(prof.get("components"), dict) else {}
        )
        inputs = {
            key: (components.get(key), _safe_float(weights.get(key)) or 0.0)
            for key in weights
        }
        b_share, b_measured, b_enabled = _coverage(inputs)
        mix_w = _safe_float(mix.get(branch)) or 0.0
        if mix_w > 0 and b_enabled:
            share_num += b_share * mix_w
            share_den += mix_w
        measured += b_measured
        enabled += b_enabled
        for key, (score, weight) in inputs.items():
            if weight > 0 and score is None and key not in missing:
                missing.append(key)
    if not enabled:
        return None
    share = share_num / share_den if share_den > 0 else 0.0
    if isinstance(recorded, dict) and _safe_float(recorded.get("share")) is not None:
        share = float(recorded["share"])
    return {
        "share": share,
        "measured": measured,
        "enabled": enabled,
        "missing": missing,
        "derived": not isinstance(recorded, dict),
    }


def _percentile_score_lower_is_better(
    value: float, values: list[float]
) -> Optional[float]:
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


def _sea_distance_score(
    distance_m: Optional[float], near_m: float, far_m: float
) -> Optional[float]:
    """Return 0-100 for straight-line distance to the coastline.

    Logarithmic decay, which is what the hedonic literature on coastal premiums
    uses: the effect is steep over the first few hundred metres and flattens out
    as it fades, rather than falling off in a straight line.

    `near_m` is the decay scale, not a plateau: the full 100 belongs to the
    shoreline itself, and `near_m` is the distance at which the curve has
    already given up a fifth of it. `far_m` is where the premium is gone.
    """
    if distance_m is None:
        return None
    if distance_m <= 0:
        return 100.0
    if distance_m >= far_m:
        return 0.0
    ratio = math.log1p(distance_m / near_m) / math.log1p(far_m / near_m)
    return _clamp((1.0 - ratio) * 100.0)


def _resolve_sea_distance_config(
    raw: Any, defaults: Dict[str, float]
) -> Tuple[Dict[str, float], Optional[str]]:
    """Validate a per-profile sea_distance override.

    The override comes from free-form profile JSON, so a bad value must fall back
    to the defaults instead of reaching the logarithm as a zero or a NaN.
    """
    if raw is None:
        return dict(defaults), None
    if not isinstance(raw, dict):
        return dict(defaults), "sea_distance override is not an object"

    resolved = dict(defaults)
    for key in ("near_m", "far_m"):
        if raw.get(key) is None:
            continue
        value = _finite_float(raw.get(key))
        if value is None:
            return dict(defaults), f"{key} is not a finite number"
        resolved[key] = value

    if resolved["near_m"] <= 0:
        return dict(defaults), "near_m must be greater than 0"
    if resolved["far_m"] <= resolved["near_m"]:
        return dict(defaults), "far_m must be greater than near_m"
    return resolved, None


def _resolve_pool_config(
    raw: Any, defaults: Dict[str, float]
) -> Tuple[Dict[str, float], Optional[str]]:
    """Validate a per-profile pool override (best_min/worst_min/require_indoor).

    Same contract as `_resolve_sea_distance_config`: free-form profile JSON,
    so a bad value falls back to the defaults and says so. `require_indoor`
    is numeric on purpose — the scoring form is numeric throughout — and only
    0 (any pool) or 1 (indoor required, the daily-swimmer default) are legal.
    """
    if raw is None:
        return dict(defaults), None
    if not isinstance(raw, dict):
        return dict(defaults), "pool override is not an object"

    resolved = dict(defaults)
    for key in ("best_min", "worst_min", "require_indoor"):
        if raw.get(key) is None:
            continue
        value = _finite_float(raw.get(key))
        if value is None:
            return dict(defaults), f"{key} is not a finite number"
        resolved[key] = value

    if resolved["best_min"] < 0:
        return dict(defaults), "best_min must not be negative"
    if resolved["worst_min"] <= resolved["best_min"]:
        return dict(defaults), "worst_min must be greater than best_min"
    if resolved["require_indoor"] not in (0.0, 1.0):
        return dict(defaults), "require_indoor must be 0 or 1"
    return resolved, None


def _weightless_score_keys() -> Tuple[str, ...]:
    """Criteria that ship at weight 0 in every category, read off the scorers.

    `routes/main_routes.py` reads this to decide which save needs the dry-run
    preview instead of applying straight away: raising one of these re-scores
    every listing in the subscription, which is a silent mass rescore that
    `git log` cannot explain (the pool criterion, D17/#278; the hazard
    criterion, #437).

    **Derived rather than listed**, because the claim this makes is that a
    criterion cannot be added to the scorer and forgotten by the gate — and a
    hand-maintained tuple is exactly a thing that can be forgotten (review,
    #453). It was `("pool_score", "hazard_score")` written out, and the
    sentence above it promised what the tuple could not keep.

    "In every category" is the load-bearing half. `sea_score` is 0.0 in both
    of `GaragePropertyScorer`'s weight sets and non-zero elsewhere: that is a
    statement about garages, not a criterion shipped off, and a rule reading
    one scorer would have dragged it in and demanded a preview for a save
    that changes nothing.
    """
    scorers = [
        value
        for value in globals().values()
        if isinstance(value, type)
        and issubclass(value, BasePropertyScorer)
        and value is not BasePropertyScorer
        and getattr(value, "DEFAULT_INVESTMENT_WEIGHTS", None)
    ]
    if not scorers:
        return ()
    keys = {
        key
        for scorer in scorers
        for key in list(scorer.DEFAULT_INVESTMENT_WEIGHTS)
        + list(scorer.DEFAULT_LIFESTYLE_WEIGHTS)
    }
    return tuple(
        sorted(
            key
            for key in keys
            if all(
                float(scorer.DEFAULT_INVESTMENT_WEIGHTS.get(key, 0.0) or 0.0) == 0.0
                and float(scorer.DEFAULT_LIFESTYLE_WEIGHTS.get(key, 0.0) or 0.0) == 0.0
                for scorer in scorers
            )
        )
    )


def _resolve_hazard_config(
    raw: Any, defaults: Dict[str, float]
) -> Tuple[Dict[str, float], Optional[str]]:
    """Validate a per-profile hazard override (near_m/far_m/moderate_factor).

    Same contract as `_resolve_sea_distance_config` and `_resolve_pool_config`:
    the profile JSON is free-form, so a bad value falls back to the defaults
    and says which one, rather than scoring a listing against a number nobody
    chose.
    """
    if raw is None:
        return dict(defaults), None
    if not isinstance(raw, dict):
        return dict(defaults), "hazard override is not an object"

    resolved = dict(defaults)
    for key in ("near_m", "far_m", "moderate_factor"):
        if raw.get(key) is None:
            continue
        value = _finite_float(raw.get(key))
        if value is None:
            return dict(defaults), f"{key} is not a finite number"
        resolved[key] = value

    if resolved["near_m"] < 0:
        return dict(defaults), "near_m must not be negative"
    if resolved["far_m"] <= resolved["near_m"]:
        return dict(defaults), "far_m must be greater than near_m"
    if not 0.0 <= resolved["moderate_factor"] <= 1.0:
        return dict(defaults), "moderate_factor must be between 0 and 1"
    return resolved, None


def _hazard_proximity_score(
    distance_m: Optional[float], *, near_m: float, far_m: float, factor: float
) -> Optional[float]:
    """100 is far from anything, 0 is next door. Linear between the bounds.

    Linear rather than the logarithmic decay the sea distance uses, and the
    difference is deliberate: the sea's premium collapses over the first few
    hundred metres, while a plume, a noise contour and a lorry route fall off
    over kilometres. `factor` scales the *penalty*, so a `moderate` hazard at
    the doorstep scores 50 rather than 0 and one past `far_m` still scores
    100.
    """
    if distance_m is None:
        return None
    if far_m <= near_m:
        return None
    if distance_m <= near_m:
        raw = 0.0
    elif distance_m >= far_m:
        raw = 100.0
    else:
        raw = (distance_m - near_m) / (far_m - near_m) * 100.0
    return _clamp(100.0 - (100.0 - raw) * factor)


def _linear_minutes_score(
    minutes: Optional[float], best: float, worst: float
) -> Optional[float]:
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


def _minutes_score_bounds(
    minutes: Optional[float],
    *,
    slack_m: float,
    mode: str,
    best: float,
    worst: float,
) -> Tuple[Optional[float], Optional[float]]:
    """What this duration could score, at both ends of the origin's error.

    A duration measured from a locality centroid describes a route that starts
    somewhere the property is merely near, and the difference is worth minutes:
    the slack converted at the mode's assumed speed. The pair is `(nearest,
    furthest)` -- the score if the parcel lies that much closer to the target,
    and the score if it lies that much further. Where they agree, the
    imprecision cannot change the answer, which is the exemption
    `sea_view_service` grants a negative it can prove from a centroid.

    A precise coordinate has no slack, so both are the measurement itself.
    """
    if minutes is None:
        return None, None

    slack_min = _slack_minutes(slack_m, mode)
    nearest = _linear_minutes_score(
        max(0.0, minutes - slack_min), best=best, worst=worst
    )
    furthest = _linear_minutes_score(minutes + slack_min, best=best, worst=worst)
    return nearest, furthest


def _slack_minutes(slack_m: float, mode: str) -> float:
    """The origin's positional error, in minutes of travel by `mode`."""
    if slack_m <= 0:
        return 0.0
    # Imported here, not at module scope: `property_travel_service` reaches for
    # `app.db` when it loads, and this module is on the import path of the app
    # factory itself. The same reason `services/pool_service.py` takes it late.
    from services.property_travel_service import estimate_duration_seconds

    return estimate_duration_seconds(slack_m, mode) / 60.0


@dataclass(frozen=True)
class PropertyScoreResult:
    investment: Optional[float]
    lifestyle: Optional[float]
    combined: Optional[float]
    scoring_payload: Dict[str, Any]


class BasePropertyScorer:
    category: str = "unknown"

    def calculate(
        self, prop: Property, profile: Optional[SearchProfile]
    ) -> PropertyScoreResult:
        raise NotImplementedError


class HousingPropertyScorer(BasePropertyScorer):
    category = "housing"

    DEFAULT_INVESTMENT_WEIGHTS = {
        "value_score": 0.6,
        "travel_score": 0.25,
        "sea_score": 0.15,
        "size_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_LIFESTYLE_WEIGHTS = {
        "travel_score": 0.45,
        "size_score": 0.3,
        "sea_score": 0.25,
        "value_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_TRAVEL_MINUTES = {"best": 10.0, "worst": 60.0}
    # Straight-line metres to the coastline. 300 m is where the coastal premium
    # is still near its peak; past 10 km the literature no longer finds one.
    DEFAULT_SEA_DISTANCE = {"near_m": 300.0, "far_m": 10000.0}
    # Drive minutes to a qualifying swimming pool (proposal D17): 10 min is a
    # daily habit, 40 min is where it stops being one. `require_indoor` = 1
    # counts only pools with indoor evidence — the owner swims year-round.
    # Weightless (0.0) in every category until the owner turns it on.
    DEFAULT_POOL = {"best_min": 10.0, "worst_min": 40.0, "require_indoor": 1.0}
    # Straight-line metres to the nearest hazardous neighbour (#437). At or
    # inside `near_m` the criterion scores 0; at or beyond `far_m` it scores
    # 100.
    #
    # **The distances behind them are measured; the thresholds are chosen**,
    # and the difference is worth keeping (review, #453). What was measured is
    # where the facilities are: at property 793 a cement works at 1.12 km, a
    # coal-fired station at 2.13 km, ArcelorMittal at 5.34 km. That 1 km
    # should score 0 and 5 km should score 100 is a judgement fitted to that
    # one coordinate, and a second industrial estuary may well move it. So is
    # `moderate_factor`, which halves the penalty for the `moderate` band on
    # the view that a quarry and a sewage works are a nuisance rather than an
    # emitter. Three numbers from one place is why the criterion ships
    # weightless: nobody is scored on them until the owner decides they are
    # right.
    # Weightless (0.0) in every category until the owner turns it on.
    DEFAULT_HAZARD = {"near_m": 1000.0, "far_m": 5000.0, "moderate_factor": 0.5}

    def calculate(
        self, prop: Property, profile: Optional[SearchProfile]
    ) -> PropertyScoreResult:
        scoring_config = (
            profile.scoring_config
            if profile and isinstance(profile.scoring_config, dict)
            else {}
        ) or {}
        cat_cfg = (
            (scoring_config.get("categories") or {}).get(self.category)
            if isinstance(scoring_config.get("categories"), dict)
            else None
        ) or {}

        investment_weights = dict(self.DEFAULT_INVESTMENT_WEIGHTS)
        lifestyle_weights = dict(self.DEFAULT_LIFESTYLE_WEIGHTS)
        travel_minutes_cfg = dict(self.DEFAULT_TRAVEL_MINUTES)
        sea_distance_cfg, sea_cfg_error = _resolve_sea_distance_config(
            cat_cfg.get("sea_distance") if isinstance(cat_cfg, dict) else None,
            self.DEFAULT_SEA_DISTANCE,
        )
        if sea_cfg_error:
            logger.warning(
                "Ignoring sea_distance override for category %s: %s",
                self.category,
                sea_cfg_error,
            )
        pool_cfg, pool_cfg_error = _resolve_pool_config(
            cat_cfg.get("pool") if isinstance(cat_cfg, dict) else None,
            self.DEFAULT_POOL,
        )
        if pool_cfg_error:
            logger.warning(
                "Ignoring pool override for category %s: %s",
                self.category,
                pool_cfg_error,
            )
        hazard_cfg, hazard_cfg_error = _resolve_hazard_config(
            cat_cfg.get("hazard") if isinstance(cat_cfg, dict) else None,
            self.DEFAULT_HAZARD,
        )
        if hazard_cfg_error:
            logger.warning(
                "Ignoring hazard override for category %s: %s",
                self.category,
                hazard_cfg_error,
            )

        # One unusable value must not take the whole subscription's scoring with
        # it. `float("high")` used to raise from inside the weight
        # comprehension, and the caller's blanket except turned that into "no
        # score" for every listing under the profile, reported nowhere (#240).
        # A bad override is skipped, named in the log, and the default for that
        # key stands -- the same treatment `_resolve_sea_distance_config` above
        # already gives its own section.
        def _apply_numeric_overrides(target: Dict[str, Any], source: Any, label: str):
            if not isinstance(source, dict):
                return
            for key, value in source.items():
                if value is None:
                    continue
                try:
                    target[key] = float(value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring %s.%s in scoring_config for category %s: "
                        "%r is not a number",
                        label,
                        key,
                        self.category,
                        value,
                    )

        if isinstance(cat_cfg, dict):
            _apply_numeric_overrides(
                investment_weights, cat_cfg.get("investment"), "investment"
            )
            _apply_numeric_overrides(
                lifestyle_weights, cat_cfg.get("lifestyle"), "lifestyle"
            )
            _apply_numeric_overrides(
                travel_minutes_cfg,
                {
                    key: value
                    for key, value in (cat_cfg.get("travel_minutes") or {}).items()
                    if key in ("best", "worst")
                }
                if isinstance(cat_cfg.get("travel_minutes"), dict)
                else None,
                "travel_minutes",
            )

        mix = getattr(Config, "COMBINED_MIX", {"investment": 0.32, "lifestyle": 0.68})
        if isinstance(cat_cfg, dict) and isinstance(cat_cfg.get("combined_mix"), dict):
            mix_override = cat_cfg.get("combined_mix") or {}
            if (
                mix_override.get("investment") is not None
                and mix_override.get("lifestyle") is not None
            ):
                try:
                    mix = {
                        "investment": float(mix_override["investment"]),
                        "lifestyle": float(mix_override["lifestyle"]),
                    }
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring combined_mix in scoring_config for category %s: "
                        "%r is not a pair of numbers",
                        self.category,
                        mix_override,
                    )

        value_score, value_meta = self._value_score(prop)
        size_score, size_meta = self._size_score(prop)
        travel_score, travel_meta = self._travel_score(
            prop,
            profile,
            best=travel_minutes_cfg["best"],
            worst=travel_minutes_cfg["worst"],
        )
        sea_score, sea_meta = self._sea_score(
            prop,
            near_m=sea_distance_cfg["near_m"],
            far_m=sea_distance_cfg["far_m"],
        )
        if sea_cfg_error:
            sea_meta = {**sea_meta, "config_override_ignored": sea_cfg_error}
        pool_score, pool_meta = self._pool_score(
            prop,
            best_min=pool_cfg["best_min"],
            worst_min=pool_cfg["worst_min"],
            require_indoor=bool(pool_cfg["require_indoor"]),
        )
        if pool_cfg_error:
            pool_meta = {**pool_meta, "config_override_ignored": pool_cfg_error}
        hazard_score, hazard_meta = self._hazard_score(
            prop,
            near_m=hazard_cfg["near_m"],
            far_m=hazard_cfg["far_m"],
            moderate_factor=hazard_cfg["moderate_factor"],
        )
        if hazard_cfg_error:
            hazard_meta = {**hazard_meta, "config_override_ignored": hazard_cfg_error}

        investment_inputs = {
            "value_score": (
                value_score,
                investment_weights.get("value_score", 0.0),
            ),
            "travel_score": (
                travel_score,
                investment_weights.get("travel_score", 0.0),
            ),
            "sea_score": (sea_score, investment_weights.get("sea_score", 0.0)),
            "size_score": (size_score, investment_weights.get("size_score", 0.0)),
            "pool_score": (pool_score, investment_weights.get("pool_score", 0.0)),
            "hazard_score": (
                hazard_score,
                investment_weights.get("hazard_score", 0.0),
            ),
        }
        lifestyle_inputs = {
            "travel_score": (
                travel_score,
                lifestyle_weights.get("travel_score", 0.0),
            ),
            "size_score": (size_score, lifestyle_weights.get("size_score", 0.0)),
            "sea_score": (sea_score, lifestyle_weights.get("sea_score", 0.0)),
            "value_score": (value_score, lifestyle_weights.get("value_score", 0.0)),
            "pool_score": (pool_score, lifestyle_weights.get("pool_score", 0.0)),
            "hazard_score": (
                hazard_score,
                lifestyle_weights.get("hazard_score", 0.0),
            ),
        }
        investment = _weighted_average(investment_inputs)
        lifestyle = _weighted_average(lifestyle_inputs)

        # Coverage per branch: how much of the enabled weight actually answered.
        # `_weighted_average` above hides this by renormalising -- a branch with
        # one criterion measured scores exactly that criterion, and nothing in
        # the number says so. Kept beside the branch scores, and folded into
        # the combined estimate below (#379).
        inv_coverage, inv_measured, inv_enabled = _coverage(investment_inputs)
        life_coverage, life_measured, life_enabled = _coverage(lifestyle_inputs)

        combined = None
        coverage = 0.0
        if investment is not None or lifestyle is not None:
            mix_inputs = {
                "investment": (
                    investment,
                    float(mix.get("investment", 0.0) or 0.0),
                ),
                "lifestyle": (lifestyle, float(mix.get("lifestyle", 0.0) or 0.0)),
            }
            combined = _weighted_average(mix_inputs)
            # Coverage composes with the mix's own weights, over both branches:
            # a branch with nothing measured contributes zero coverage, not a
            # renormalised absence. The score itself is untouched by it (owner
            # decision 2026-08-17: show the coverage, do not invent a prior).
            # A branch with no enabled criterion at all (a profile that zeroed
            # one side) has no coverage to contribute and no weight in the
            # denominator -- the same gate `score_coverage()` applies when it
            # derives the share, so the recorded and the derived value agree.
            branch_share = {
                "investment": (inv_coverage, inv_enabled),
                "lifestyle": (life_coverage, life_enabled),
            }
            mix_total = sum(
                w
                for name, (_, w) in mix_inputs.items()
                if w > 0 and branch_share[name][1] > 0
            )
            if mix_total > 0:
                coverage = (
                    sum(
                        branch_share[name][0] * w
                        for name, (_, w) in mix_inputs.items()
                        if w > 0 and branch_share[name][1] > 0
                    )
                    / mix_total
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
                        "sea_score": sea_score,
                        "size_score": size_score,
                        "pool_score": pool_score,
                        "hazard_score": hazard_score,
                    },
                },
                "lifestyle": {
                    "score": lifestyle,
                    "weights": lifestyle_weights,
                    "components": {
                        "travel_score": travel_score,
                        "size_score": size_score,
                        "sea_score": sea_score,
                        "value_score": value_score,
                        "pool_score": pool_score,
                        "hazard_score": hazard_score,
                    },
                },
            },
            "combined_mix": mix,
            "combined_score": combined,
            # #379: how much of the enabled weight the scores rest on. Never
            # part of the number; `score_coverage()` reads it (and derives it
            # for payloads written before this field).
            "coverage": {
                "share": coverage,
                "investment": {
                    "share": inv_coverage,
                    "measured": inv_measured,
                    "enabled": inv_enabled,
                },
                "lifestyle": {
                    "share": life_coverage,
                    "measured": life_measured,
                    "enabled": life_enabled,
                },
            },
            "details": {
                "value": value_meta,
                "size": size_meta,
                "travel": travel_meta,
                "sea": sea_meta,
                "pool": pool_meta,
                "hazard": hazard_meta,
            },
        }

        return PropertyScoreResult(
            investment=investment,
            lifestyle=lifestyle,
            combined=combined,
            scoring_payload=payload,
        )

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

    def _collect_peer_ppm2(
        self, prop: Property, *, min_peers: int, limit: int
    ) -> Tuple[list[float], Dict[str, Any]]:
        """Peer price/m² values, from comparables of a comparable size (#378).

        Price per m² falls as a plot grows, so ranking a listing against every
        size at once makes `value_score` a second reading of `size_score`.
        Measured on production 2026-08-17 as rank correlations, which is what
        these components are rather than an approximation of them:

            land     n=319   corr(area, value%) = +0.702
            housing  n= 87   corr(area, value%) = +0.779

        (Those two are the raw signal, every size in one pool. Through this
        ladder, with #377's key, land measures +0.604 -- the municipality tier
        already narrows it a little.)

        Both weightings look innocent on their own -- land carries value only
        in the investment profile and size only in the lifestyle one -- but the
        combined score is the saved mix (#257), and at the default 0.32/0.68 the
        two channels together drive 46% of the number `/properties` sorts by.

        The band is the fix: a listing is ranked against plots within a factor
        of `PEER_AREA_BAND_FACTOR` of its own area, which is what a human means
        by a comparable. The factor is measured, not chosen -- run through this
        ladder over the 319 production land rows, after #377 gave the
        municipality tier its normalised key:

            factor    corr(area, value%)   rows scored   in band   median peers
            (none)          +0.604            318/319        0          14
            1.15            +0.109            318/319      302          17
            1.25            +0.085            318/319      309          22
            1.35            +0.123            318/319      311          20
            1.50            +0.185            318/319      313          16

        1.25 is the minimum of that curve, not a compromise on it: tighter
        thins the municipality tier until the ladder falls through to a wider
        scope (which is why 1.15 is *worse*), looser lets the confound back in.
        It removes 86% of the double count and still finds a median of 22
        comparables, and every row that scored before still scores.

        Geography relaxes *inside* the banded pass before the band is given up,
        because size is the confound this is about: comparing a 1,200 m² plot
        with 1,200 m² plots in the next municipality is closer to the truth than
        comparing it with 40,000 m² parcels next door. Only when no scope finds
        `min_peers` at a comparable size does the old unbanded ladder run, and
        the scope name records which happened.

        The ladder itself now lives in `services/property_comparables.py`: the
        AI prompt asks the same question of the same table and was still
        answering it unbanded (#386), which is what a rule written down twice
        does. This method is the price/m² reading of that one pool.
        """
        rows, meta = collect_comparables(
            prop,
            category=self.category,
            min_peers=min_peers,
            limit=limit,
        )
        values: list[float] = []
        for p in rows:
            p_price = _safe_float(p.price)
            p_area = _safe_float(p.area)
            if p_price is None or p_area is None or p_area <= 0:
                continue
            values.append(p_price / p_area)

        # `size_comparable` restates the `+area_band` suffix already in
        # `comparable_scope`; the AI prompt needs it as a flag because it has to
        # *say* whether it compared like with like, the scoring payload does not
        # and would only gain a second copy of one fact.
        meta = {k: v for k, v in meta.items() if k != "size_comparable"}
        return values, meta

    def _collect_peer_areas(
        self, prop: Property, *, min_peers: int, limit: int
    ) -> Tuple[list[float], Dict[str, Any]]:
        """Peer areas for the size component — the same ladder, unbanded.

        A band around the listing's own area would be circular here: the
        question is how big this plot is against the others, which a window
        centred on its own size cannot answer.
        """
        rows, meta = collect_comparables(
            prop,
            category=self.category,
            min_peers=min_peers,
            limit=limit,
            require_price=False,
            band=False,
        )
        values: list[float] = []
        for p in rows:
            p_area = _safe_float(p.area)
            if p_area is None or p_area <= 0:
                continue
            values.append(p_area)
        return values, {"comparable_scope": meta.get("comparable_scope")}

    def _sea_score(
        self, prop: Property, near_m: float, far_m: float
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        """Score straight-line proximity to the coastline.

        A refused measurement scores None so `_weighted_average` drops it and
        renormalises; only a measured "no coastline nearby" scores zero. Issue
        #98 is the reason those two are not allowed to look the same.

        A measurement taken from a locality centroid is a third thing, and it
        is scored the way `sea_view_service` decides a verdict on one: not by a
        hard bar, but by asking whether the answer could change anywhere inside
        the error. The centroid of Santa María del Mar is 23.8 m from the
        coastline and four listings sit on it, one of them ten kilometres
        inland -- so a 5 km slack spans the whole decay curve there and there
        is no score to give. Far enough inland the same slack spans nothing:
        both ends of the range are past `far_m`, the answer is zero either way,
        and it is scored.
        """
        bounds = {"near_m": near_m, "far_m": far_m}
        measurement = parcel_measurement(prop)
        status = measurement.get("status")
        accuracy = {"origin_accuracy": measurement.get("origin_accuracy")}

        if status == STATUS_NO_COASTLINE:
            # "No coastline found" only rules out the radius that was actually
            # searched. A profile whose horizon reaches past it is asking about
            # ground the measurement never covered, so there is nothing to score.
            searched = _finite_float(measurement.get("searched_m"))
            if searched is None or far_m > searched:
                return None, {
                    **bounds,
                    **accuracy,
                    "status": "horizon_exceeds_search",
                    "searched_m": searched,
                }
            return 0.0, {
                **bounds,
                **accuracy,
                "status": STATUS_NO_COASTLINE,
                "distance_m": None,
                "searched_m": searched,
            }

        if status not in (STATUS_OK, STATUS_APPROXIMATE_ORIGIN):
            # unavailable / no_coordinates / never measured.
            return None, {**bounds, **accuracy, "status": status or "unknown"}

        lower = _finite_float(measurement.get("min_distance_m"))
        upper = _finite_float(measurement.get("max_distance_m"))
        if lower is None or upper is None:
            return None, {**bounds, **accuracy, "status": "missing_distance"}

        # Identical inputs for a precise row (lower == upper == the measured
        # distance), so this is the whole rule for both kinds of coordinate
        # rather than a special case bolted onto the side of one.
        low_score = _sea_distance_score(lower, near_m=near_m, far_m=far_m)
        high_score = _sea_distance_score(upper, near_m=near_m, far_m=far_m)
        if low_score != high_score:
            return None, {
                **bounds,
                **accuracy,
                "status": STATUS_APPROXIMATE_ORIGIN,
                "origin_distance_m": measurement.get("origin_distance_m"),
                "min_distance_m": lower,
                "max_distance_m": upper,
            }

        return low_score, {
            **bounds,
            **accuracy,
            "status": status,
            "distance_m": measurement.get("distance_m"),
            "min_distance_m": lower,
            "max_distance_m": upper,
        }

    def _pool_score(
        self,
        prop: Property,
        best_min: float,
        worst_min: float,
        require_indoor: bool,
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        """Drive minutes to the nearest qualifying pool (proposal D17).

        The invariants the review pinned: only a *measured* drive time
        scores; `unverified_absence` is None, never 0 — the single Text
        Search cross-check proves nothing about completeness; the only path
        to a true 0 is the owner's own hand-set flag, which outranks
        everything and survives recomputes. With `require_indoor` the
        qualifying set narrows to candidates whose indoor evidence is
        `verified` or `likely` — evidence, not certainty, so the meta says
        which candidate and on what grounds.
        """
        bounds = {
            "best_min": best_min,
            "worst_min": worst_min,
            "require_indoor": require_indoor,
        }
        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        pool = enrichment.get("pool")
        if not isinstance(pool, dict):
            return None, {**bounds, "status": "missing_pool_data"}

        # The owner's verdict outranks every computed state (sea-view rule).
        owner_flag = pool.get("owner_no_pool")
        if isinstance(owner_flag, dict):
            return 0.0, {
                **bounds,
                "status": "owner_verified_absence",
                "set_at": owner_flag.get("set_at"),
            }

        status = pool.get("status")
        if status == "unverified_absence":
            return None, {**bounds, "status": status}
        if status != "ok":
            return None, {**bounds, "status": status or "unknown"}

        candidates = pool.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        best_candidate = None
        best_minutes: Optional[float] = None
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if require_indoor and candidate.get("indoor_status") not in (
                "verified",
                "likely",
            ):
                continue
            minutes = _finite_float(candidate.get("drive_min"))
            if minutes is None:
                continue
            if best_minutes is None or minutes < best_minutes:
                best_minutes = minutes
                best_candidate = candidate

        if best_minutes is None:
            # Measured pools exist but none passes the indoor requirement
            # (or none was measured): not a measured absence of *qualifying*
            # pools, so no score — and the meta says why.
            return None, {
                **bounds,
                "status": "no_qualifying_candidate",
                "candidates_seen": len(candidates),
            }

        score = _linear_minutes_score(best_minutes, best=best_min, worst=worst_min)
        return score, {
            **bounds,
            "status": "ok",
            "minutes": best_minutes,
            "candidate": best_candidate.get("name") if best_candidate else None,
            "indoor_status": best_candidate.get("indoor_status")
            if best_candidate
            else None,
            "indoor_evidence": best_candidate.get("indoor_evidence")
            if best_candidate
            else None,
        }

    def _hazard_score(
        self,
        prop: Property,
        near_m: float,
        far_m: float,
        moderate_factor: float,
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        """Straight-line metres to the nearest hazardous neighbour (#437).

        Three refusals, and each is the #98 rule in a different clothing:

        * no block, a refusal, or no coordinate scores `None`. Nobody looked,
          and a clean 100 for a listing nobody scanned is exactly the false
          all-clear this feature exists to remove;
        * a *measured* absence scores 100 -- but only when the scan reached
          far enough to support it. `guaranteed_m` is the radius the answer
          covers around the **parcel**, so an approximate row's 6 km scan
          guarantees 1 km and cannot say anything about a works at 4 km. That
          is `sea_distance`'s rule that a `far_m` past the searched radius
          scores `None` rather than 0;
        * a measured hazard scores only when the answer is the same at both
          ends of the coordinate's slack (#358). For a precise row the bounds
          are one number twice, so nothing about that path is special-cased;
          for a centroid at 1.1 km the band runs 0 to 6.1 km and the scores
          disagree, so the component abstains rather than asserting either.

        The scan is read through `hazard_service.read_verdict`, never off the
        stored block, because a row re-geocoded since the measurement carries
        a `slack_m` that no longer matches its accuracy.
        """
        from services import hazard_rules, hazard_service

        bounds = {
            "near_m": near_m,
            "far_m": far_m,
            "moderate_factor": moderate_factor,
        }
        verdict = hazard_service.read_verdict(prop)
        status = verdict.get("status")
        if not verdict.get("measured"):
            return None, {**bounds, "status": status or "missing_hazard_data"}

        if verdict.get("truncated"):
            # The scan reached Overpass's element cap, so what came back is a
            # statement about the elements it saw and not about the radius --
            # and the elements it did not see could be anywhere, including
            # nearer than everything it did. That is true with items and
            # without them: the first version of this guard sat inside the
            # empty branch, and a truncated scan carrying one distant facility
            # still scored 100 (codex review, 2026-08-20). The card discloses
            # it; the number must not answer over it.
            return None, {**bounds, "status": "scan_truncated"}

        # The horizon check is asked of every scan, not only an empty one.
        # `far_m` is configurable per subscription, and a scan that covered
        # less than it cannot answer for the ground past its edge: a moderate
        # quarry at 5 km scored 72 while an unseen high-severity facility at
        # 6 km would have scored 55 and decided the component (codex review,
        # 2026-08-20). This is why an approximate row does not score the
        # criterion at all -- 6 km around a centroid guarantees 1 km around
        # the parcel, which is the #358 rule and not a special case.
        guaranteed = verdict.get("guaranteed_m")
        if guaranteed is None or guaranteed < far_m:
            # Two different reasons wear this shape, and the meta names the
            # one the owner can act on: a scan that really was too short, and
            # a scan long enough whose *slack* eats the difference. The second
            # is a re-geocode away; the first is not.
            searched = verdict.get("searched_m")
            shortfall = (
                STATUS_APPROXIMATE_ORIGIN
                if searched is not None and searched >= far_m
                else "searched_radius_too_small"
            )
            return None, {
                **bounds,
                "status": shortfall,
                "guaranteed_m": guaranteed,
                "origin_accuracy": verdict.get("origin_accuracy"),
            }

        items = verdict.get("items") or []
        if not items:
            return 100.0, {
                **bounds,
                "status": status,
                "guaranteed_m": guaranteed,
                "items": 0,
            }

        worst_score: Optional[float] = None
        worst_item: Optional[Dict[str, Any]] = None
        for item in items:
            factor = (
                moderate_factor
                if item.get("severity") != hazard_rules.SEVERITY_HIGH
                else 1.0
            )
            low = _hazard_proximity_score(
                _finite_float(item.get("min_distance_m")),
                near_m=near_m,
                far_m=far_m,
                factor=factor,
            )
            high = _hazard_proximity_score(
                _finite_float(item.get("max_distance_m")),
                near_m=near_m,
                far_m=far_m,
                factor=factor,
            )
            if low is None or high is None:
                # Unreachable through `read_verdict`, which refuses a block
                # holding an item without a finite distance rather than
                # handing one on -- an item that cannot be read is not an item
                # that can be walked past, because the ones beside it would
                # then be reported as the whole picture (codex review,
                # 2026-08-20). Kept because this function takes a dict and
                # somebody will one day hand it one from somewhere else.
                return None, {
                    **bounds,
                    "status": "unreadable_item",
                    "item": item.get("name") or item.get("kind"),
                }
            if low != high:
                # The slack can move this one, and the exemption is asked of
                # the *listing*, not of the items that happen to be safe: a
                # block whose nearest facility is unresolvable cannot report
                # the second-nearest as the answer.
                return None, {
                    **bounds,
                    "status": STATUS_APPROXIMATE_ORIGIN,
                    "origin_accuracy": verdict.get("origin_accuracy"),
                    "slack_m": verdict.get("slack_m"),
                    "item": item.get("name") or item.get("kind"),
                }
            if worst_score is None or low < worst_score:
                worst_score = low
                worst_item = item

        if worst_score is None:
            return None, {**bounds, "status": "no_scorable_item"}

        # The stored list is capped, so a facility may have been left out --
        # but only ones further away than every item here. Their score is
        # therefore at least the score of the farthest kept item taken at full
        # severity, and while the worst kept item is at or below that bound,
        # nothing dropped can beat it. When it is not, the answer is not
        # knowable from what was stored.
        stored_count = verdict.get("item_count") or len(items)
        if stored_count > len(items):
            farthest = max(
                (_finite_float(item.get("max_distance_m")) for item in items),
                default=None,
            )
            bound = _hazard_proximity_score(
                farthest, near_m=near_m, far_m=far_m, factor=1.0
            )
            if bound is None or worst_score > bound:
                return None, {
                    **bounds,
                    "status": "list_truncated",
                    "items": len(items),
                    "item_count": stored_count,
                }

        return worst_score, {
            **bounds,
            "status": status,
            "item": (worst_item or {}).get("name") or (worst_item or {}).get("kind"),
            "kind": (worst_item or {}).get("kind"),
            "severity": (worst_item or {}).get("severity"),
            "distance_m": (worst_item or {}).get("origin_distance_m"),
            "items": len(items),
        }

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

        # The same question the sea score asks, in minutes: the parcel may sit
        # up to the slack away from the coordinate every one of these durations
        # was measured from. `estimate_duration_seconds` converts it at the
        # mode's own assumed speed -- the repository's one answer to "how long
        # does this far take", rather than a second speed constant invented
        # here.
        #
        # Asked of the *average*, not of each target, and that distinction is
        # the whole rule. Per target it would keep only the durations that
        # cannot move -- and measured on the live database those are 690 short
        # ones against 9 long ones, so the surviving subset is systematically
        # the near targets and the component would come out near 100 for rows
        # whose real mix is nothing of the sort. Dropping the ambiguous ones
        # and averaging what is left is not a smaller claim than the original,
        # it is a differently wrong one.
        slack_m = coordinate_slack_m(prop)
        origin_accuracy = normalize_accuracy(getattr(prop, "location_accuracy", None))

        key_scores: Dict[str, Dict[str, Any]] = {}
        near_scores: list[float] = []
        far_scores: list[float] = []
        for key in enabled_keys:
            t = targets.get(key)
            minutes = None
            mode = "driving"
            if isinstance(t, dict):
                minutes = _safe_float(t.get("duration_min"))
                mode = str(t.get("mode") or "driving")
            nearest, furthest = _minutes_score_bounds(
                minutes, slack_m=slack_m, mode=mode, best=best, worst=worst
            )
            key_scores[key] = {
                "minutes": minutes,
                "score": nearest if nearest == furthest else None,
            }
            if nearest is None or furthest is None:
                continue
            near_scores.append(nearest)
            far_scores.append(furthest)

        if not near_scores:
            return None, {
                "status": "missing_travel",
                "origin_accuracy": origin_accuracy,
                "targets": key_scores,
                "best": best,
                "worst": worst,
            }

        # Every target counts in both averages, so this compares the same set
        # to itself at the two ends of the error -- an equality that means the
        # coordinate's imprecision cannot change the component, and for a
        # precise row is one number compared with itself.
        best_case = sum(near_scores) / len(near_scores)
        worst_case = sum(far_scores) / len(far_scores)
        if best_case != worst_case:
            return None, {
                "status": STATUS_APPROXIMATE_ORIGIN,
                "origin_accuracy": origin_accuracy,
                "range": [_clamp(worst_case), _clamp(best_case)],
                "targets": key_scores,
                "best": best,
                "worst": worst,
            }

        return _clamp(best_case), {
            "status": "ok",
            "origin_accuracy": origin_accuracy,
            "targets": key_scores,
            "best": best,
            "worst": worst,
        }


class LandPropertyScorer(HousingPropertyScorer):
    category = "land"

    DEFAULT_INVESTMENT_WEIGHTS = {
        "value_score": 0.7,
        "travel_score": 0.15,
        "sea_score": 0.15,
        "size_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_LIFESTYLE_WEIGHTS = {
        "travel_score": 0.4,
        "size_score": 0.35,
        "sea_score": 0.25,
        "value_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }


class GaragePropertyScorer(HousingPropertyScorer):
    category = "garage"

    # A garage is bought for where you park, not for the view: the sea is
    # deliberately weightless here rather than absent, so the component still
    # shows up in the breakdown.
    DEFAULT_INVESTMENT_WEIGHTS = {
        "value_score": 0.9,
        "travel_score": 0.1,
        "sea_score": 0.0,
        "size_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_LIFESTYLE_WEIGHTS = {
        "travel_score": 0.7,
        "size_score": 0.3,
        "sea_score": 0.0,
        "value_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }


class CommercialPropertyScorer(HousingPropertyScorer):
    category = "commercial"

    DEFAULT_INVESTMENT_WEIGHTS = {
        "value_score": 0.8,
        "travel_score": 0.15,
        "sea_score": 0.05,
        "size_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_LIFESTYLE_WEIGHTS = {
        "travel_score": 0.55,
        "size_score": 0.35,
        "sea_score": 0.1,
        "value_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }


class BuildingPropertyScorer(HousingPropertyScorer):
    category = "building"

    DEFAULT_INVESTMENT_WEIGHTS = {
        "value_score": 0.8,
        "travel_score": 0.15,
        "sea_score": 0.05,
        "size_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_LIFESTYLE_WEIGHTS = {
        "travel_score": 0.55,
        "size_score": 0.35,
        "sea_score": 0.1,
        "value_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }


class NewDevelopmentPropertyScorer(HousingPropertyScorer):
    category = "new_development"

    DEFAULT_INVESTMENT_WEIGHTS = {
        "value_score": 0.6,
        "travel_score": 0.25,
        "sea_score": 0.15,
        "size_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }
    DEFAULT_LIFESTYLE_WEIGHTS = {
        "travel_score": 0.45,
        "size_score": 0.3,
        "sea_score": 0.25,
        "value_score": 0.0,
        "pool_score": 0.0,
        "hazard_score": 0.0,
    }


class DefaultPropertyScorer(BasePropertyScorer):
    category = "default"

    def calculate(
        self, prop: Property, profile: Optional[SearchProfile]
    ) -> PropertyScoreResult:
        payload = {
            "version": 1,
            "category": prop.property_category or "unknown",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "unsupported_category",
        }
        return PropertyScoreResult(
            investment=None, lifestyle=None, combined=None, scoring_payload=payload
        )


WEIGHTLESS_SCORE_KEYS = _weightless_score_keys()


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

    # The editable shape of `SearchProfile.scoring_config`, in one place: the
    # subscription page builds its form from this rather than repeating the
    # scorer's own vocabulary, so a criterion added to a scorer cannot go on
    # being invisible in the UI that is supposed to configure it (#239).
    WEIGHT_KEYS = (
        "value_score",
        "size_score",
        "travel_score",
        "sea_score",
        "pool_score",
        "hazard_score",
    )
    EDITABLE_SECTIONS = {
        "investment": WEIGHT_KEYS,
        "lifestyle": WEIGHT_KEYS,
        "combined_mix": ("investment", "lifestyle"),
        "travel_minutes": ("best", "worst"),
        "sea_distance": ("near_m", "far_m"),
        # require_indoor is numeric like every field here: 1 = only pools
        # with indoor evidence count (the daily-swimmer default), 0 = any.
        "pool": ("best_min", "worst_min", "require_indoor"),
        "hazard": ("near_m", "far_m", "moderate_factor"),
    }

    def known_categories(self) -> list:
        """The categories that have a scorer of their own, in a stable order."""
        return sorted(self._scorers)

    def defaults_for(self, category: str) -> Dict[str, Dict[str, float]]:
        """What a category scores by when its subscription overrides nothing.

        Read off the scorer itself. A page that hard-coded these numbers would
        drift from the scoring the moment either changed.
        """
        scorer = self._scorers.get((category or "").strip().lower()) or self._default
        combined_mix = getattr(
            Config, "COMBINED_MIX", {"investment": 0.32, "lifestyle": 0.68}
        )
        return {
            "investment": {
                key: float(value)
                for key, value in getattr(
                    scorer, "DEFAULT_INVESTMENT_WEIGHTS", {}
                ).items()
            },
            "lifestyle": {
                key: float(value)
                for key, value in getattr(
                    scorer, "DEFAULT_LIFESTYLE_WEIGHTS", {}
                ).items()
            },
            "combined_mix": {
                "investment": float(combined_mix.get("investment", 0.32)),
                "lifestyle": float(combined_mix.get("lifestyle", 0.68)),
            },
            "travel_minutes": {
                key: float(value)
                for key, value in getattr(
                    scorer, "DEFAULT_TRAVEL_MINUTES", {"best": 10.0, "worst": 60.0}
                ).items()
            },
            "sea_distance": {
                key: float(value)
                for key, value in getattr(
                    scorer,
                    "DEFAULT_SEA_DISTANCE",
                    {"near_m": 300.0, "far_m": 10000.0},
                ).items()
            },
            "pool": {
                key: float(value)
                for key, value in getattr(
                    scorer,
                    "DEFAULT_POOL",
                    {"best_min": 10.0, "worst_min": 40.0, "require_indoor": 1.0},
                ).items()
            },
            "hazard": {
                key: float(value)
                for key, value in getattr(
                    scorer,
                    "DEFAULT_HAZARD",
                    {"near_m": 1000.0, "far_m": 5000.0, "moderate_factor": 0.5},
                ).items()
            },
        }

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

        prop.score_investment = (
            Decimal(str(result.investment)) if result.investment is not None else None
        )
        prop.score_lifestyle = (
            Decimal(str(result.lifestyle)) if result.lifestyle is not None else None
        )
        prop.score_total = (
            Decimal(str(result.combined)) if result.combined is not None else None
        )
        prop.scoring = result.scoring_payload

        if commit:
            db.session.commit()
        return True

    @staticmethod
    def stored_score(value: Any) -> Optional[Decimal]:
        """The value as the database will keep it, or None.

        `Property.score_*` are `Numeric(5, 2)`, so a score is stored to the
        cent and a shift of 0.01 is a real difference in the table. Anything
        asking "would this save change the score" has to ask it at that
        precision -- `routes/main_routes.py`'s weight preview asked it with
        `abs(new - old) >= 0.05` and answered *"0 of 4 listings would change"*
        for a save that then wrote 33.32 over 33.33 (review of #453,
        2026-08-20).

        The precision is read off the column rather than written here again,
        so a schema that changes it does not leave this rounding behind. And
        the comparison stays in `Decimal`: both sides already are one -- the
        column gives Decimals and `calculate_for_property` assigns
        `Decimal(str(...))` -- and going through `float` is what defeated the
        old threshold at its own boundary, where `float(50.05) - float(50.0)`
        is 0.049999999999997 and therefore *not* `>= 0.05`.

        `ROUND_HALF_UP` is **measured, not chosen**. This function claims to
        say what the database keeps, so the tie-breaking has to be the
        database's: asked on the deployment's own PostgreSQL, `0.005::numeric
        (5,2)` is `0.01`, `0.025` is `0.03` and `-0.005` is `-0.01` -- half
        away from zero. `Decimal`'s default is `ROUND_HALF_EVEN`, which makes
        the first of those `0.00`; `round(float(...), 2)` gets that one right
        and `0.015` wrong. A first version of this helper used the default and
        was therefore describing a database nobody runs (found by pushing on a
        mutation that escaped, 2026-08-20).
        """
        if value is None:
            return None
        scale = getattr(Property.__table__.c.score_total.type, "scale", 2) or 0
        return Decimal(str(value)).quantize(
            Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP
        )

    def calculate_for_property_id(self, property_id: int, commit: bool = True) -> bool:
        prop = db.session.get(Property, property_id)
        if not prop:
            return False
        return self.calculate_for_property(prop, commit=commit)
