"""Read a plot (parcela) from listing text, and say where the figure came from (#634).

A subscription that requires a plot (`min_plot_m2`) judges a row `unknown`
while `properties.plot_area` is empty, and the portals that do not state it as
a field often state it in the description ("asentada sobre una parcela de
745 m²"). This module reads that sentence. It is pure: no database, no
network, nothing that raises on odd input.

What it refuses (a refusal is `None`, which the criteria read as `unknown`,
never as `fail`):

* a figure that belongs to the house ("745 m² construidos", "útiles",
  "de vivienda"): the plot keyword has to introduce the number, and a house
  label after it refuses the figure whatever punctuation stands between
  (review of #661: "Finca: 1.362 m² (superficie construida)"). A qualifier
  (construido, edificado, útil, habitable, construcción) refuses across
  brackets, dashes, colons, slashes and commas; a noun (vivienda, casa)
  across everything but a comma, because after a comma it starts the next
  item ("finca de 2.000 m², casa de 150 m²");
* a text that says there is no plot ("sin terreno", "sin parcela"): whatever
  figure it also gives, the plot is not established, and as a source it
  vetoes the others;
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
EXTRACTOR_VERSION = "plot_text/2"
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
# What follows a figure that is the HOUSE, not the plot. The separator is
# any run of these characters, never one kind (review of #661); a full stop
# or semicolon ends the clause, so it is not one.
_HOUSE_SEPARATOR = r"[\s()\[\]{}\-–—:,/]*"
_HOUSE_NOUN_SEPARATOR = r"[\s()\[\]{}\-–—:/]*"
_HOUSE_QUALIFIER = r"(?:de\s+)?(?:superficie\s+)?(?:constru|edificad|[úu]til|habitable)"
_HOUSE_NOUN = r"(?:de\s+)?(?:superficie\s+)?(?:vivienda|casa)\b"
_HOUSE_AFTER = re.compile(
    rf"{_HOUSE_SEPARATOR}{_HOUSE_QUALIFIER}|{_HOUSE_NOUN_SEPARATOR}{_HOUSE_NOUN}",
    re.IGNORECASE,
)
# A text that says the home has no plot establishes none, whatever it adds.
_NO_PLOT = re.compile(
    r"\bsin\s+(?:ning[úu]n\s+)?(?:terreno|parcela|finca|solar)\b",
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


def denies_plot(text: Optional[str]) -> bool:
    """True when `text` says there is no plot ("sin terreno")."""
    if not text or not isinstance(text, str):
        return False
    try:
        return bool(_NO_PLOT.search(_strip_html(text)))
    except Exception:
        logger.warning("plot denial could not be read", exc_info=True)
        return True  # cannot tell: doubt writes nothing


def _readings(text: str) -> List[Tuple[float, str]]:
    found: List[Tuple[float, str]] = []
    if _NO_PLOT.search(text):
        return found
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


def _all_readings(text: Optional[str]) -> List[Tuple[float, str]]:
    """Every credible plot figure `text` states (total: [] on any failure)."""
    if not text or not isinstance(text, str):
        return []
    try:
        return _readings(_strip_html(text))
    except Exception:  # pure and total: ingestion must never lose an email to it
        logger.warning("plot text reading failed", exc_info=True)
        return []


def mentions_plot(text: Optional[str]) -> bool:
    """True when `text` states at least one credible plot figure."""
    return bool(_all_readings(text))


def extract_plot_m2(text: Optional[str]) -> Optional[PlotReading]:
    """The one plot figure `text` states, or None (absent, cut off, ambiguous)."""
    found = _all_readings(text)
    if len({value for value, _ in found}) != 1:
        return None
    value, snippet = found[0]
    return PlotReading(value=value, snippet=snippet)


def read_sources(
    sources: Sequence[Tuple[str, Optional[str]]],
) -> Optional[Tuple[str, PlotReading]]:
    """The one decision: (field, reading) when every source that names a plot
    names the same single value, else None. A source that contradicts itself,
    or two sources that disagree, leave the row unknown. Total.
    """
    try:
        values = set()
        first: Optional[Tuple[str, PlotReading]] = None
        if any(denies_plot(text) for _, text in sources):
            return None
        for field, text in sources:
            found = _all_readings(text)
            distinct = {value for value, _ in found}
            if len(distinct) > 1:
                return None
            if distinct:
                values |= distinct
                if first is None:
                    first = (field, PlotReading(*found[0]))
        if len(values) != 1:
            return None
        return first
    except Exception:
        logger.warning("plot sources could not be read", exc_info=True)
        return None


def apply_to_property(
    prop: Any,
    sources: Sequence[Tuple[str, Optional[str]]],
    *,
    portal: str,
) -> Optional[Dict[str, Any]]:
    """Set `plot_area` when every source naming a plot names the same one.

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
        decided = read_sources(sources)
        if decided is not None:
            field, reading = decided
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
