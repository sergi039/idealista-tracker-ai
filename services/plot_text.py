"""Read a plot (parcela) from listing text, and say where the figure came from (#634).

A subscription that requires a plot (`min_plot_m2`) judges a row `unknown`
while `properties.plot_area` is empty, and the portals that do not state it as
a field often state it in the description ("asentada sobre una parcela de
745 m²"). This module reads that sentence. It is pure: no database, no
network, nothing that raises on odd input.

What it refuses (a refusal is `None`, which the criteria read as `unknown`,
never as `fail`):

* a figure that belongs to the house ("745 m² construidos", "útiles",
  "de vivienda"): the plot keyword has to introduce the number;
* a figure cut off by an excerpt ("parcela de 6..." -- idealista's alert text
  is truncated): the unit has to follow the number inside the text;
* a text that gives two different plot figures: which one is the plot is not
  established;
* a figure that is not a measurement: `services.subscription_criteria.
  MAX_CREDIBLE_M2` is the one credibility rule (NaN, inf, non-positive and
  absurd values). `PLOT_AREA_STORAGE_MAX` below is NOT a second plausibility
  ceiling but the width of the column (`NUMERIC(10,2)`): a value that cannot
  be stored is refused rather than allowed to fail the ingestion commit.

The number grammar is `utils.idealista_extractors._PRICE_NUMBER` (Spanish
"1.200" and "1,5", English "1,200.5"); there is no second one.

`apply_to_property` writes the figure to `plot_area` -- the column the SQL
twins in `subscription_criteria` read, so the Python and SQL verdicts stay
identical without touching them -- and records the source in
`enrichment["plot_from_text"]` in the same transaction. It never overwrites a
plot that is already stated.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.subscription_criteria import MAX_CREDIBLE_M2, effective_figures
from utils.idealista_extractors import (
    _PRICE_NUMBER,
    _parse_number_groups,
    _strip_html,
)

logger = logging.getLogger(__name__)

# Bump when the reading rules change: a stored provenance names the rules that
# produced it, and the backfill's restore compares it.
EXTRACTOR_VERSION = "plot_text/1"
ENRICHMENT_KEY = "plot_from_text"

# NUMERIC(10, 2) overflows at 1e8; this is the column, not a plausibility rule.
PLOT_AREA_STORAGE_MAX = 1e8
HECTARE_M2 = 10_000
SNIPPET_MAX_CHARS = 120

_KEYWORD = r"(?:superficie\s+de\s+parcela|parcela|terreno|finca)"
# 'solar' is also an adjective ("placas solares", "orientación solar"): as a
# plot it is only ever the noun followed by "de" and the figure.
_SOLAR = r"solar\s+de"
_PLOT_WORD = rf"(?:{_KEYWORD}|solar)"
_FILLER = (
    r"(?:de|del|con|of|total|aproximada|aproximadamente|aprox\.?|rústica|rustica|"
    r"urbana|urbanizable|edificable|cercada|vallada)"
)
_UNIT = (
    r"(?P<unit>m\s?[²2]|mts\s?[²2]|metros\s+cuadrados|m\^2|hect[áa]reas?|ha)"
    r"(?![\w²])"
)
# What directly follows a figure that is the HOUSE, not the plot.
_HOUSE_AFTER = re.compile(
    r"\s*(?:de\s+)?(?:superficie\s+)?"
    r"(?:construid|edificad|[úu]til|habitable|vivienda|de\s+vivienda)",
    re.IGNORECASE,
)

_FORWARD = re.compile(
    rf"\b(?:{_KEYWORD}|{_SOLAR})(?:\s+{_FILLER}){{0,2}}\s*[:\-–=]?\s*"
    rf"{_PRICE_NUMBER}\s*{_UNIT}",
    re.IGNORECASE,
)
# "1,5 ha de terreno", "745 m² de parcela".
_BACKWARD = re.compile(
    rf"{_PRICE_NUMBER}\s*{_UNIT}\s+de\s+{_PLOT_WORD}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlotReading:
    value: float
    snippet: str


def _credible(value: Optional[float]) -> bool:
    return (
        value is not None
        and math.isfinite(value)
        and 0 < value < MAX_CREDIBLE_M2
        and value < PLOT_AREA_STORAGE_MAX
    )


def _square_metres(match: "re.Match[str]") -> Optional[float]:
    number = _parse_number_groups(match.groups()[:4])
    if number is None:
        return None
    unit = match.group("unit").lower()
    factor = HECTARE_M2 if unit.startswith(("h",)) else 1
    return round(number * factor, 2)


def _readings(text: str) -> List[Tuple[float, str]]:
    found: List[Tuple[float, str]] = []
    for pattern in (_FORWARD, _BACKWARD):
        for match in pattern.finditer(text):
            if _HOUSE_AFTER.match(text, match.end()):
                continue
            value = _square_metres(match)
            if not _credible(value):
                continue
            snippet = text[max(0, match.start() - 20) : match.end() + 20].strip()
            found.append((value, snippet[:SNIPPET_MAX_CHARS]))
    return found


def extract_plot_m2(text: Optional[str]) -> Optional[PlotReading]:
    """The one plot figure `text` states, or None (absent, cut off, ambiguous)."""
    if not text or not isinstance(text, str):
        return None
    try:
        plain = _strip_html(text)
        found = _readings(plain)
    except Exception:  # pure and total: ingestion must never lose an email to it
        logger.warning("plot text reading failed", exc_info=True)
        return None
    if not found:
        return None
    if len({value for value, _ in found}) > 1:
        return None
    value, snippet = found[0]
    return PlotReading(value=value, snippet=snippet)


def apply_to_property(
    prop: Any,
    sources: Sequence[Tuple[str, Optional[str]]],
    *,
    portal: str,
) -> Optional[Dict[str, Any]]:
    """Set `plot_area` from the first source that states exactly one plot.

    Returns the provenance block written, or None when nothing was written.
    Never overwrites: a row whose plot is already credible (portal-stated,
    hand-set, research sheet) or whose `area` already IS the parcel (bare
    land) is left alone. The caller owns the transaction; `enrichment` is
    assigned a new dict (the column is not a MutableDict).
    """
    try:
        if effective_figures(prop)["plot_m2"] is not None:
            return None
        if prop.plot_area is not None and not _blank(prop.plot_area):
            return None
        for field, text in sources:
            reading = extract_plot_m2(text)
            if reading is None:
                continue
            block = {
                "value": reading.value,
                "field": field,
                "snippet": reading.snippet,
                "extractor_version": EXTRACTOR_VERSION,
                "portal": portal,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }
            prop.plot_area = reading.value
            enrichment = dict(prop.enrichment or {})
            enrichment[ENRICHMENT_KEY] = block
            prop.enrichment = enrichment
            return block
    except Exception:
        logger.warning("plot text could not be applied", exc_info=True)
    return None


def _blank(value: Any) -> bool:
    """A stored plot that is not a measurement (fotocasa's 0, NaN)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    return not (math.isfinite(number) and number > 0)
