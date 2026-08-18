"""Nearest preset places from OpenStreetMap instead of a billed Places search.

Step 2 of the cost plan agreed after the EUR 190 invoice of 1-18 August 2026.
Five of the seven Places calls a listing costs are the presets below; the
hospital left for the national register in step 1, and the beaches are still
Google's.

Everything here was measured against six real production coordinates before it
was written, and two of the findings decided the design.

**The names Google returns for these types are frequently not the thing.**
Property 101's nearest "police" was *Traffic radar*; property 67's was *Unidad
territorial de seguridad privada*, a private security firm, and its "school"
was *Academia Mar*, a private tutoring academy. Property 123's "supermarket"
was *La luz de mundo*. OSM answers the same coordinates with `amenity=police`
-> Comisaría de la Policía Local de Gijón and Cuartel del Cuerpo Nacional de
Policía, and `shop=supermarket` -> Alimerka 0.9 km. A tag is a claim about
what a thing *is*; a Places type is a claim about what it is *like*.

**The airport rules of #171 work on OSM names verbatim, and reach further.**
`aeroway=aerodrome` includes exactly the aeroclubs and light-aircraft fields
Google's `airport` type does -- at Oviedo the nearest is *Aeródromo de La
Morgal*, 9.2 km -- and the shipped `require_name_patterns` refuse every one of
them while accepting *Aeropuerto de Asturias* and *Aeroporto da Coruña*. On
all six coordinates that picked the same airport Google did. It also removes
the reason `wide_search_query` exists: Overpass has no 50 km cap (#254), so
Cariño resolves A Coruña at 64.3 km in the same query, with no second paid
call.

Two implementation notes that are not free choices.

*One query answers every preset.* The lookup asks for all declared types at
once and caches the candidates, so the five presets of one property cost one
Overpass round trip rather than five. The per-preset call site is unchanged --
the second preset reads the cache the first one filled.

*Candidates, not the nearest.* The cache holds up to `_MAX_CANDIDATES` per
type in distance order, because the rules walk past what they refuse: keeping
only the nearest would cache La Morgal and leave the preset with nothing to
fall back to.
"""

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from services.place_rules import place_rules_from
from utils.cache import cache_enrichment_data, get_cached_enrichment_data

logger = logging.getLogger(__name__)

# A month: a police station does not move, and the read is the expensive part
# of a bulk run at the shared 5 s Overpass gate.
_CACHE_TTL_S = 60 * 60 * 24 * 30
_CACHE_PREFIX = "osm_places_v1"

# Enough for the rules to walk past what they refuse -- eleven aerodromes sit
# within 100 km of Oviedo and the first two are refused.
_MAX_CANDIDATES = 12


@dataclass
class OsmPlaceLookup:
    """A place from OSM, the honest absence of one, or why we do not know."""

    place: Optional[Dict[str, Any]] = None
    failure: Optional[Any] = None
    # True when Overpass answered and nothing of that type qualifies. That is
    # a measurement ("no airport within 100 km"), not a refusal, and the two
    # must not be collapsed (#98).
    answered_empty: bool = False


def osm_spec(preset_def: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
    """`(key, value, radius_m)` this preset is answered by, or None."""
    if not isinstance(preset_def, dict):
        return None
    tag = preset_def.get("osm_tag")
    if not isinstance(tag, str) or "=" not in tag:
        return None
    key, _, value = tag.partition("=")
    radius = preset_def.get("osm_radius_m")
    if not isinstance(radius, int) or radius <= 0:
        return None
    return key.strip(), value.strip(), radius


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _build_query(specs: Dict[str, Tuple[str, str, int]], lat: float, lon: float) -> str:
    """One union over every declared type, largest radius included.

    `out center tags` because a station or an aerodrome is usually a way or a
    relation, and only `center` gives it a point to route from.
    """
    clauses = []
    for key, value, radius in sorted(set(specs.values())):
        for kind in ("node", "way", "relation"):
            clauses.append(f'{kind}["{key}"="{value}"](around:{radius},{lat},{lon});')
    # 90 s: the widest query measured (100 km of aerodromes around Oviedo)
    # took 27 s against the public instance on a good day.
    return "[out:json][timeout:90];(" + "".join(clauses) + ");out center tags;"


def _signature(specs: Dict[str, Tuple[str, str, int]]) -> str:
    raw = "|".join(f"{k}={v}@{r}" for k, v, r in sorted(set(specs.values())))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def _candidates_from(
    elements: List[Dict[str, Any]],
    specs: Dict[str, Tuple[str, str, int]],
    lat: float,
    lon: float,
) -> Dict[str, List[Dict[str, Any]]]:
    found: Dict[str, List[Dict[str, Any]]] = {key: [] for key in specs}
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        centre = (
            element.get("center")
            if isinstance(element.get("center"), dict)
            else element
        )
        try:
            elat = float(centre.get("lat"))
            elon = float(centre.get("lon"))
        except (TypeError, ValueError):
            continue
        distance = _haversine_m(lat, lon, elat, elon)
        for preset_key, (key, value, radius) in specs.items():
            if tags.get(key) != value or distance > radius:
                continue
            found[preset_key].append(
                {
                    # An unnamed school is still a school. The name is what a
                    # human reads, so it is kept as OSM has it and never
                    # invented from the tag.
                    "name": tags.get("name"),
                    "lat": elat,
                    "lon": elon,
                    "distance_m": int(round(distance)),
                    "source": "osm",
                    "osm_type": element.get("type"),
                    "osm_id": element.get("id"),
                }
            )
    for preset_key in found:
        found[preset_key].sort(key=lambda item: item["distance_m"])
        del found[preset_key][_MAX_CANDIDATES:]
    return found


def lookup_candidates(
    service: Any,
    specs: Dict[str, Tuple[str, str, int]],
    lat: float,
    lon: float,
) -> Tuple[Optional[Dict[str, List[Dict[str, Any]]]], Optional[Any]]:
    """Candidates per preset from one Overpass round trip, cached.

    The transport belongs to `EnrichmentService._overpass_elements` and is
    reached through it, the way `services/pool_service.py` already does: the
    gate, the User-Agent and the three refusals Overpass delivers (#144) live
    exactly once and this is not the place to grow a second copy.

    A refusal is never cached -- it would keep answering "nothing nearby" for
    a month, which is the defect the refusal exists to prevent.
    """
    if not specs:
        return {}, None

    cache_key = f"{_CACHE_PREFIX}:{_signature(specs)}"
    cached = get_cached_enrichment_data(lat, lon, cache_key)
    if isinstance(cached, dict):
        return {key: list(cached.get(key) or []) for key in specs}, None

    elements, failure = service._overpass_elements(_build_query(specs, lat, lon))
    if failure is not None:
        return None, failure

    found = _candidates_from(elements, specs, lat, lon)
    try:
        cache_enrichment_data(lat, lon, cache_key, found, timeout=_CACHE_TTL_S)
    except Exception:
        logger.warning("Could not cache OSM places for %s,%s", lat, lon, exc_info=True)
    return found, None


def pick(
    preset_def: Dict[str, Any], candidates: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The nearest candidate this preset's rules accept.

    The same `PlaceRules` the Google path uses, on OSM names: measured on six
    production coordinates, the airport rules refuse every aerodrome and
    aeroclub and accept the two real airports.
    """
    rules = place_rules_from(preset_def)
    for candidate in candidates:
        if rules is not None and rules.rejects(
            {"name": candidate.get("name"), "types": []}
        ):
            continue
        return candidate
    return None
