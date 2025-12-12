import json
import hashlib
from typing import Any, Dict, Optional, Tuple


REQUIRED_TOP_KEYS = [
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


def extract_highlights(analysis: Any) -> Dict[str, Any]:
    """Compact qualitative fields so the UI can show differences quickly."""
    a = _as_dict(analysis)
    if not a:
        return {}

    key_drivers = _pick(a, "investment_potential", "key_drivers")
    if isinstance(key_drivers, list):
        key_drivers_text = " • ".join(str(x) for x in key_drivers[:3] if x is not None)
    else:
        key_drivers_text = None

    highlights: Dict[str, Any] = {
        "price_verdict": _pick(a, "price_analysis", "verdict"),
        "price_summary": _truncate(_pick(a, "price_analysis", "summary"), 140),
        "investment_potential_rating": _pick(a, "investment_potential", "rating"),
        "risk_level": _pick(a, "investment_potential", "risk_level"),
        "key_drivers": _truncate(key_drivers_text, 160),
        "best_use": _truncate(_pick(a, "development_ideas", "best_use"), 140),
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
            "best_use",
            "market_trend",
        ]
    }
    sig_bytes = json.dumps(sig_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    highlights["signature"] = hashlib.sha256(sig_bytes).hexdigest()[:16]

    return highlights


def schema_completeness(analysis: Any) -> Tuple[int, int]:
    a = _as_dict(analysis)
    if not a:
        return (0, len(REQUIRED_TOP_KEYS))
    found = sum(1 for k in REQUIRED_TOP_KEYS if k in a and a.get(k) is not None)
    return (found, len(REQUIRED_TOP_KEYS))


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def numeric_fidelity_score(metrics: Dict[str, Any], expected: Dict[str, Any]) -> float:
    keys = ["rental_yield", "cap_rate", "price_to_rent_ratio", "payback_period_years"]
    errors = []
    for k in keys:
        v = _to_float(metrics.get(k))
        e = _to_float(expected.get(k))
        if v is None or e is None or e == 0:
            continue
        errors.append(abs(v - e) / abs(e))
    if not errors:
        return 0.0

    mean_pct = sum(errors) / len(errors)
    # Map 0% -> 100, 50% -> 0 (clamped)
    score = max(0.0, 100.0 * (1.0 - min(mean_pct, 0.5) / 0.5))
    return round(score, 1)


def overall_score(completeness: Tuple[int, int], fidelity: float) -> float:
    found, total = completeness
    completeness_pct = (found / total) * 100.0 if total else 0.0
    return round(0.6 * completeness_pct + 0.4 * fidelity, 1)


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


def evaluate(land, analysis: Any) -> Dict[str, Any]:
    metrics = extract_metrics(analysis)
    completeness = schema_completeness(analysis)
    expected = expected_rental_metrics(land)
    fidelity = numeric_fidelity_score(metrics, expected)
    return {
        "metrics": metrics,
        "highlights": extract_highlights(analysis),
        "schema": {"found": completeness[0], "total": completeness[1]},
        "expected": expected,
        "fidelity_score": fidelity,
        "overall_score": overall_score(completeness, fidelity),
    }


def build_comparison(land, claude_analysis: Any, openai_analysis: Any) -> Dict[str, Any]:
    claude_eval = evaluate(land, claude_analysis)
    openai_eval = evaluate(land, openai_analysis) if openai_analysis else None

    return {
        "claude": claude_eval,
        "chatgpt": openai_eval,
        "expected": claude_eval["expected"],
    }
