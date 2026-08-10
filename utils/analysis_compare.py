import json
import hashlib
from typing import Any, Dict, List, Optional, Tuple


# `PropertyAIService` asks for a different top-level schema per category, so
# completeness has to be counted against the schema the analysis was actually
# generated from. Counting a house against the land schema deducted two keys
# (`development_ideas`, `construction_value_estimation`) the prompt never asked
# for, capping every housing analysis at 7/9.
LAND_TOP_KEYS = [
    "price_analysis",
    "investment_potential",
    "risks_analysis",
    "development_ideas",
    "comparable_analysis",
    "similar_objects",
    "construction_value_estimation",
    "market_price_dynamics",
    "rental_market_analysis",
]

HOUSING_TOP_KEYS = [
    "price_analysis",
    "investment_potential",
    "risks_analysis",
    "renovation_ideas",
    "comparable_analysis",
    "similar_objects",
    "market_price_dynamics",
    "rental_market_analysis",
]

GENERIC_TOP_KEYS = [
    "price_analysis",
    "investment_potential",
    "risks_analysis",
    "usage_ideas",
    "comparable_analysis",
    "similar_objects",
    "market_price_dynamics",
    "rental_market_analysis",
]

SCHEMA_KEYS_BY_CATEGORY = {
    "land": LAND_TOP_KEYS,
    "housing": HOUSING_TOP_KEYS,
    "new_development": HOUSING_TOP_KEYS,
    "generic": GENERIC_TOP_KEYS,
}

# The ideas section is what tells the three schemas apart, so reading it off the
# stored analysis keeps the count right even when a listing was recategorised
# after its analysis was generated.
_IDEAS_KEY_TO_SCHEMA = {
    "development_ideas": LAND_TOP_KEYS,
    "renovation_ideas": HOUSING_TOP_KEYS,
    "usage_ideas": GENERIC_TOP_KEYS,
}

# Kept for the Land comparison, which only ever sees the land schema.
REQUIRED_TOP_KEYS = LAND_TOP_KEYS

RENTAL_NUMERIC_KEYS = [
    "rental_yield",
    "cap_rate",
    "price_to_rent_ratio",
    "payback_period_years",
]


def _as_dict(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def extract_metrics(analysis: Any) -> Dict[str, Any]:
    a = _as_dict(analysis)
    rental = a.get("rental_market_analysis") if isinstance(a, dict) else None
    rental = rental if isinstance(rental, dict) else {}

    return {
        "investment_rating": rental.get("investment_rating"),
        "rental_yield": rental.get("rental_yield"),
        "cap_rate": rental.get("cap_rate"),
        "price_to_rent_ratio": rental.get("price_to_rent_ratio"),
        "payback_period_years": rental.get("payback_period_years"),
    }


def _pick(a: Dict[str, Any], *path: str) -> Any:
    cur: Any = a
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _truncate(value: Any, max_len: int = 120) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _join_top(value: Any, limit: int = 3, max_len: int = 160) -> Optional[str]:
    """The first few entries of a list field as one line, or `None`.

    A provider that answered with a sentence where the schema asked for a list
    still answered, so it is read as one item: reporting nothing would say the
    provider was silent when it was not. Anything that is neither a list nor a
    string is not an answer.
    """
    if isinstance(value, str):
        items: List[Any] = [value]
    elif isinstance(value, list):
        items = value
    else:
        return None
    text = " • ".join(str(x) for x in items[:limit] if x is not None)
    return _truncate(text, max_len) or None


def _best_use(a: Dict[str, Any]) -> Optional[str]:
    """The ideas section is named per schema, so read whichever one is present."""
    for section in ("development_ideas", "usage_ideas", "renovation_ideas"):
        value = _pick(a, section, "best_use")
        if value:
            return str(value)

    # The housing schema has no `best_use`; its closest field is a list.
    improvements = _pick(a, "renovation_ideas", "best_improvements")
    if isinstance(improvements, list):
        text = " • ".join(str(x) for x in improvements[:3] if x is not None)
        return text or None
    return None


def extract_highlights(analysis: Any) -> Dict[str, Any]:
    """Compact qualitative fields so the UI can show differences quickly."""
    a = _as_dict(analysis)
    if not a:
        return {}

    highlights: Dict[str, Any] = {
        "price_verdict": _pick(a, "price_analysis", "verdict"),
        "price_summary": _truncate(_pick(a, "price_analysis", "summary"), 140),
        "investment_potential_rating": _pick(a, "investment_potential", "rating"),
        "risk_level": _pick(a, "investment_potential", "risk_level"),
        "key_drivers": _join_top(_pick(a, "investment_potential", "key_drivers")),
        # The reasons behind `risk_level`. Without them the comparison states
        # "Medium" against "High" and never says what the two disagree about.
        "key_risks": _join_top(_pick(a, "risks_analysis", "major_risks")),
        "best_use": _truncate(_best_use(a), 140),
        "market_trend": _pick(a, "market_price_dynamics", "price_trend"),
    }

    sig_payload = {
        k: highlights.get(k)
        for k in [
            "price_verdict",
            "price_summary",
            "investment_potential_rating",
            "risk_level",
            "key_drivers",
            "key_risks",
            "best_use",
            "market_trend",
        ]
    }
    sig_bytes = json.dumps(sig_payload, sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    highlights["signature"] = hashlib.sha256(sig_bytes).hexdigest()[:16]

    return highlights


def schema_keys_for(analysis: Any, category: Optional[str] = None) -> List[str]:
    a = _as_dict(analysis)
    for ideas_key, keys in _IDEAS_KEY_TO_SCHEMA.items():
        if a.get(ideas_key) is not None:
            return keys
    return SCHEMA_KEYS_BY_CATEGORY.get((category or "").strip().lower(), LAND_TOP_KEYS)


def schema_completeness(
    analysis: Any, category: Optional[str] = None
) -> Tuple[int, int]:
    keys = schema_keys_for(analysis, category)
    a = _as_dict(analysis)
    if not a:
        return (0, len(keys))
    found = sum(1 for k in keys if k in a and a.get(k) is not None)
    return (found, len(keys))


def numeric_coverage(metrics: Dict[str, Any]) -> Tuple[int, int]:
    """How many rental figures the provider actually put a number on.

    A model that answers `null` for every figure is making a statement, and it
    is the one thing the comparison can measure without a market baseline.
    """
    found = sum(1 for k in RENTAL_NUMERIC_KEYS if _to_float(metrics.get(k)) is not None)
    return (found, len(RENTAL_NUMERIC_KEYS))


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def numeric_fidelity_score(
    metrics: Dict[str, Any], expected: Optional[Dict[str, Any]]
) -> Optional[float]:
    """Score the provider's figures against a baseline, or `None` if unmeasured.

    Nothing to compare is not a score of zero: returning 0.0 made "the baseline
    is missing" read exactly like "every figure was wrong", which is what put
    `0/100` next to both providers on every property page.
    """
    expected = expected or {}
    errors = []
    for k in RENTAL_NUMERIC_KEYS:
        v = _to_float(metrics.get(k))
        e = _to_float(expected.get(k))
        if v is None or e is None or e == 0:
            continue
        errors.append(abs(v - e) / abs(e))
    if not errors:
        return None

    mean_pct = sum(errors) / len(errors)
    # Map 0% -> 100, 50% -> 0 (clamped)
    score = max(0.0, 100.0 * (1.0 - min(mean_pct, 0.5) / 0.5))
    return round(score, 1)


def overall_score(
    completeness: Tuple[int, int],
    fidelity: Optional[float],
    coverage: Optional[Tuple[int, int]] = None,
) -> float:
    """Weight schema completeness against whichever numeric signal exists.

    With a baseline the second term is fidelity to it; without one it is how
    many figures the provider filled in. Falling back to schema completeness
    alone would score every complete answer the same, which is what made both
    providers land on 60/100 regardless of what they actually said.
    """
    found, total = completeness
    completeness_pct = (found / total) * 100.0 if total else 0.0

    numeric_pct: Optional[float] = fidelity
    if numeric_pct is None and coverage is not None:
        cov_found, cov_total = coverage
        numeric_pct = (cov_found / cov_total) * 100.0 if cov_total else None
    if numeric_pct is None:
        return round(completeness_pct, 1)

    return round(0.6 * completeness_pct + 0.4 * numeric_pct, 1)


def expected_rental_metrics(land) -> Dict[str, Any]:
    from services.market_analysis_service import MarketAnalysisService

    service = MarketAnalysisService()
    enriched = service.get_enriched_data(land) or {}
    rental = enriched.get("rental_market_analysis") or {}
    if not isinstance(rental, dict):
        rental = {}

    return {
        "rental_yield": rental.get("rental_yield"),
        "cap_rate": rental.get("cap_rate"),
        "price_to_rent_ratio": rental.get("price_to_rent_ratio"),
        "payback_period_years": rental.get("payback_period_years"),
        "investment_rating": rental.get("investment_rating"),
    }


def build_evaluation(
    analysis: Any,
    expected: Optional[Dict[str, Any]] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """The one rubric both the Land and the Property comparison report."""
    metrics = extract_metrics(analysis)
    completeness = schema_completeness(analysis, category)
    coverage = numeric_coverage(metrics)
    fidelity = numeric_fidelity_score(metrics, expected)
    return {
        "metrics": metrics,
        "highlights": extract_highlights(analysis),
        "schema": {"found": completeness[0], "total": completeness[1]},
        "numeric_coverage": {"found": coverage[0], "total": coverage[1]},
        "expected": expected,
        "fidelity_score": fidelity,
        "overall_score": overall_score(completeness, fidelity, coverage),
        "overall_basis": "schema+baseline"
        if fidelity is not None
        else "schema+coverage",
    }


def evaluate(land, analysis: Any) -> Dict[str, Any]:
    return build_evaluation(
        analysis, expected=expected_rental_metrics(land), category="land"
    )


def build_comparison(
    land, claude_analysis: Any, openai_analysis: Any
) -> Dict[str, Any]:
    claude_eval = evaluate(land, claude_analysis)
    openai_eval = evaluate(land, openai_analysis) if openai_analysis else None
    expected = claude_eval["expected"] or {}
    baseline_available = any(
        _to_float(expected.get(k)) not in (None, 0) for k in RENTAL_NUMERIC_KEYS
    )

    return {
        "claude": claude_eval,
        "chatgpt": openai_eval,
        "expected": claude_eval["expected"],
        "baseline": {
            "available": baseline_available,
            "reason": None
            if baseline_available
            else "The market model returned no rental figures for this land.",
        },
    }
