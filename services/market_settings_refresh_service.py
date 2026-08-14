"""AI-assisted refresh of the market analysis settings.

One button press on /criteria asks Claude (through the owner's subscription
bridge, never an API key) for a current-year set of market parameters and
writes them into the single ``market_settings`` row. The bridge enforces the
JSON shape via ``schema`` (issue #218); this module enforces the *values*:
every figure must land inside the same bounds the manual form imposes, and
min <= avg <= max must hold, or the whole response is refused and the stored
settings stay untouched. A refresh never partially applies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from app import db
from services import subscription_transport

logger = logging.getLogger(__name__)

REFRESH_TIMEOUT_SECONDS = 240

# The same bounds the /criteria form puts on its inputs; a refresh may not
# write a value the owner could not have typed.
_CONSTRUCTION_BOUNDS = {"basic": (500, 3000), "premium": (800, 5000)}
_RENTAL_PRICE_BOUNDS = (1, 50)
_RATIO_BOUNDS = {
    "purchase_costs_ratio": (0.05, 0.20),
    "vacancy_rate": (0.0, 0.50),
    "operating_expenses": (0.0, 0.50),
    "management_fee": (0.0, 0.30),
}

_LOCATION_TYPES = ("urban", "suburban", "rural")
_MIN_AVG_MAX = ("min", "avg", "max")

_TRIPLE_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "number"} for k in _MIN_AVG_MAX},
    "required": list(_MIN_AVG_MAX),
    "additionalProperties": False,
}

_ADJUSTMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "vacancy_rate": {"type": "number"},
        "operating_expenses": {"type": "number"},
        "management_fee": {"type": "number"},
    },
    "required": ["vacancy_rate", "operating_expenses", "management_fee"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "construction_costs": {
            "type": "object",
            "properties": {"basic": _TRIPLE_SCHEMA, "premium": _TRIPLE_SCHEMA},
            "required": ["basic", "premium"],
            "additionalProperties": False,
        },
        "purchase_costs_ratio": {"type": "number"},
        "rental_adjustments": {
            "type": "object",
            "properties": {k: _ADJUSTMENT_SCHEMA for k in _LOCATION_TYPES},
            "required": list(_LOCATION_TYPES),
            "additionalProperties": False,
        },
        "rental_prices": {
            "type": "object",
            "properties": {k: _TRIPLE_SCHEMA for k in _LOCATION_TYPES},
            "required": list(_LOCATION_TYPES),
            "additionalProperties": False,
        },
        "sources_note": {"type": "string"},
    },
    "required": [
        "construction_costs",
        "purchase_costs_ratio",
        "rental_adjustments",
        "rental_prices",
    ],
    "additionalProperties": False,
}


class MarketSettingsRefreshError(RuntimeError):
    """The refresh could not produce a full, in-bounds parameter set."""


def _build_prompt(today: str, market_context: str) -> str:
    return f"""Today is {today}. Produce the current market parameters used by a
property-analysis tool. The buyer's search area is the north coast of Spain
(Asturias and Galicia); "urban" means cities like Gijon and Oviedo, "suburban"
their surrounding municipalities, "rural" coastal villages and countryside.

Owner-configured market context:
{market_context}

Return ONLY JSON matching the schema, with:
- construction_costs (EUR per m2 of built area, current year, Spain):
  "basic" = standard single-family home with basic finishes,
  "premium" = high-end finishes, basement, quality materials.
- purchase_costs_ratio: overall purchase overhead as a ratio of price
  (ITP or IVA plus notary/registry/legal; Asturias general ITP is the
  reference), e.g. 0.10 for 10%.
- rental_adjustments per location type: vacancy_rate, operating_expenses,
  management_fee as ratios (rural = vacation/seasonal rentals).
- rental_prices per location type: long-term rent in EUR per m2 per month.
- sources_note: one sentence naming what the figures are based on.

Base the figures on the most recent data you are confident about and keep
them conservative; do not invent precision you do not have."""


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketSettingsRefreshError(f"{field} is not a number")
    return float(value)


def _check_bounds(value: float, lo: float, hi: float, field: str) -> None:
    if not lo <= value <= hi:
        raise MarketSettingsRefreshError(
            f"{field}={value} is outside the allowed range {lo}..{hi}"
        )


def _validate_triple(data: Any, field: str, lo: float, hi: float) -> Dict[str, int]:
    if not isinstance(data, dict):
        raise MarketSettingsRefreshError(f"{field} is missing")
    out: Dict[str, int] = {}
    for key in _MIN_AVG_MAX:
        value = _require_number(data.get(key), f"{field}.{key}")
        _check_bounds(value, lo, hi, f"{field}.{key}")
        out[key] = int(round(value))
    if not out["min"] <= out["avg"] <= out["max"]:
        raise MarketSettingsRefreshError(f"{field} violates min <= avg <= max")
    return out


def _validate_adjustments(data: Any, field: str) -> Dict[str, float]:
    if not isinstance(data, dict):
        raise MarketSettingsRefreshError(f"{field} is missing")
    out: Dict[str, float] = {}
    for key in ("vacancy_rate", "operating_expenses", "management_fee"):
        value = _require_number(data.get(key), f"{field}.{key}")
        lo, hi = _RATIO_BOUNDS[key]
        _check_bounds(value, lo, hi, f"{field}.{key}")
        out[key] = round(value, 3)
    return out


def _validate_response(payload: Any) -> Dict[str, Any]:
    """Reduce the model's JSON to validated column values, or refuse whole."""
    if not isinstance(payload, dict):
        raise MarketSettingsRefreshError("response is not a JSON object")

    construction = payload.get("construction_costs")
    if not isinstance(construction, dict):
        raise MarketSettingsRefreshError("construction_costs is missing")

    values: Dict[str, Any] = {}
    for tier, (lo, hi) in _CONSTRUCTION_BOUNDS.items():
        triple = _validate_triple(
            construction.get(tier), f"construction_costs.{tier}", lo, hi
        )
        for key in _MIN_AVG_MAX:
            values[f"construction_{tier}_{key}"] = triple[key]

    ratio = _require_number(payload.get("purchase_costs_ratio"), "purchase_costs_ratio")
    lo, hi = _RATIO_BOUNDS["purchase_costs_ratio"]
    _check_bounds(ratio, lo, hi, "purchase_costs_ratio")
    values["purchase_costs_ratio"] = round(ratio, 3)

    adjustments = payload.get("rental_adjustments")
    if not isinstance(adjustments, dict):
        raise MarketSettingsRefreshError("rental_adjustments is missing")
    for location in _LOCATION_TYPES:
        adj = _validate_adjustments(
            adjustments.get(location), f"rental_adjustments.{location}"
        )
        for key, value in adj.items():
            values[f"{location}_{key}"] = value

    prices = payload.get("rental_prices")
    if not isinstance(prices, dict):
        raise MarketSettingsRefreshError("rental_prices is missing")
    lo, hi = _RENTAL_PRICE_BOUNDS
    for location in _LOCATION_TYPES:
        triple = _validate_triple(
            prices.get(location), f"rental_prices.{location}", lo, hi
        )
        for key in _MIN_AVG_MAX:
            values[f"{location}_rental_{key}"] = triple[key]

    return values


def refresh_market_settings() -> Tuple[List[Tuple[str, Any, Any]], str]:
    """Ask the bridge for current parameters and apply them in one commit.

    Returns ``(changes, sources_note)`` where ``changes`` is a list of
    ``(column, old, new)`` for every value that actually moved. Raises
    :class:`MarketSettingsRefreshError` (validation) or
    :class:`subscription_transport.SubscriptionTransportError` (bridge) with
    the stored row untouched.
    """
    from models import MarketSettings
    from services.settings_service import SettingsService

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _build_prompt(today, SettingsService.get_ai_market_context())

    result = subscription_transport.complete(
        prompt,
        provider="claude",
        system="You are a Spanish real-estate market analyst. Answer with JSON only.",
        timeout=REFRESH_TIMEOUT_SECONDS,
        schema=RESPONSE_SCHEMA,
    )

    text = str(result.get("text") or "")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise MarketSettingsRefreshError("the model did not return valid JSON") from exc

    values = _validate_response(payload)
    sources_note = str(payload.get("sources_note") or "").strip()

    settings = MarketSettings.get_settings()
    changes: List[Tuple[str, Any, Any]] = []
    for column, new_value in values.items():
        old_value = getattr(settings, column)
        old_norm = float(old_value) if old_value is not None else None
        if old_norm != float(new_value):
            changes.append((column, old_value, new_value))
        setattr(settings, column, new_value)

    # `onupdate` only fires when a column actually changes; a refresh that
    # confirms every value should still move the "last updated" stamp.
    from models import utcnow

    settings.updated_at = utcnow()
    db.session.commit()

    logger.info(
        "Market settings refreshed via AI: %d value(s) changed. %s",
        len(changes),
        sources_note,
    )
    return changes, sources_note
