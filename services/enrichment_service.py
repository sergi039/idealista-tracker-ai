import re
import logging
import hashlib
import math
import requests
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

from utils.geocoding import GeocodingService
from utils.google_api import (
    REASON_HTTP_ERROR,
    REASON_MALFORMED_RESPONSE,
    REASON_NO_API_KEY,
    GoogleApiFailure,
    failure_from_exception,
    read_api_payload,
)
from utils.http import HTTP_USER_AGENT, OVERPASS_GATE, request_with_retries
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from config import Config
from services.sea_view_service import haversine_m
from services.place_rules import place_rules_from
from services.scoring_service import ScoringService
from services.search_profile_service import TRAVEL_PRESET_DEFS

logger = logging.getLogger(__name__)

# What may be written down as an airport, read from the preset that defines it
# (issue #171) instead of copied. `/properties` has refused helipads and
# aeroclubs since #171; this legacy path went on accepting them, which is the
# drift a second copy of the patterns would have made permanent.
_AIRPORT_RULES = place_rules_from(TRAVEL_PRESET_DEFS.get("airport") or {})

# Bumped from v1 with those rules: a v1 entry holds the helipad the old,
# unfiltered airport search accepted, and it is cached for a week. Reusing the
# key would have served that answer -- and the "no airport" the rules would now
# refuse to draw from it -- for seven days after the fix shipped.
_PLACES_CACHE_TYPE = "google_places_v2"

# Outcome of the last Overpass call, persisted in
# `infrastructure_extended["osm_amenities_status"]["state"]`. Without it an
# absent `osm_amenities` is ambiguous: it could mean Overpass answered and
# there is nothing within 2km, or that Overpass refused and we never looked.
OSM_STATUS_KEY = "osm_amenities_status"
OSM_STATE_OK = "ok"
OSM_STATE_UNAVAILABLE = "unavailable"

# Overpass answers a server-side failure with HTTP 200 and a `remark` in the
# body rather than an error status, so this reason exists to keep that case
# distinguishable from the transport failures in utils.google_api. Persisted in
# a JSON column and matched by tests: keep it stable.
OSM_REASON_QUERY_ERROR = "overpass_query_error"

# Bumped from v1: the entry now carries the time its counts were measured, so a
# cache hit can report their real age instead of the age of the read. A v1
# entry is a bare counts dict, which this key simply never looks at.
OSM_CACHE_KEY = "osm_amenities_v2"

# A property with no usable coordinates was never asked about, which is not the
# same as "nothing nearby". Geocoding it would be a paid Google call, so the
# amenity lookup records the gap instead of reaching for one. Same wording as
# `services/sea_distance_service.STATUS_NO_COORDINATES`, for the same reason.
OSM_REASON_NO_COORDINATES = "no_coordinates"

# The section of `Property.enrichment` the amenity counts live in. It is the
# same name the legacy `Land` column has, so `Property.infrastructure_extended`
# and the property page read both without a second code path.
PROPERTY_INFRASTRUCTURE_KEY = "infrastructure_extended"

# Amenity kinds counted within this radius. Frozen into the cache key by
# OSM_CACHE_KEY: widen either and bump that, or a cached count answers a
# question it was never asked.
OSM_AMENITY_KINDS = ("supermarket", "school", "hospital", "restaurant", "cafe", "fuel")
OSM_AMENITY_RADIUS_M = 2000


def _osm_coordinate(value: Any, *, limit: float) -> Optional[float]:
    """A usable coordinate, or None when it is missing or off the globe.

    Same rule as `_coordinate` in services/sea_distance_service.py, and for the
    same reason: an out-of-range latitude would still come back with nothing
    nearby, and that absence would then be filed as a measurement.
    """
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or abs(result) > limit:
        return None
    return result


@dataclass(frozen=True)
class OsmAmenityReading:
    """The outcome of one Overpass amenity lookup, with no model attached.

    Exactly one of `counts` and `failure` is set. Empty counts with no failure
    is a real answer -- Overpass looked and there is nothing within the radius
    -- and that distinction is the whole point of #98, #144 and this class.
    """

    counts: Optional[Dict[str, int]] = None
    measured_at: Optional[str] = None
    failure: Optional[GoogleApiFailure] = None


@dataclass(frozen=True)
class OsmSupermarketReading:
    """One supermarket-reach lookup (QoL slice, issue #271 follow-up).

    Same contract as OsmAmenityReading: exactly one of `items`/`failure` set,
    an empty list is a measured answer, a refusal never reads as one.
    """

    items: Optional[list] = None
    measured_at: Optional[str] = None
    failure: Optional[GoogleApiFailure] = None


# The QoL card names the nearest supermarkets rather than counting them: the
# 2 km amenity count above answers "is there one at the door", this answers
# "where do you actually shop" — rural Asturias routinely means a 10 km drive.
OSM_SUPERMARKET_CACHE_KEY = "osm_supermarket_reach_v1"
OSM_SUPERMARKET_RADIUS_M = 12000
OSM_SUPERMARKET_TOP_N = 5


# Verdict for one `enrich_land` run, persisted in
# `infrastructure_extended["enrichment_status"]["state"]`. It mirrors
# `Property.travel["api_status"]["state"]` in services/property_travel_service.py
# so both enrichment paths report completeness in the same shape (#153).
#
# `unavailable` means a source the score reads refused, so the run did not
# produce what it was asked for. `degraded` means only an advisory source
# refused: the score is unaffected and the record is usable, but a source was
# still missed, and calling that "ok" is the #98 mistake in miniature. The
# per-source `osm_amenities_status` above is unaffected - it says what the last
# Overpass call did, this says what the run as a whole produced.
# Persisted in a JSON column and matched by tests: keep these stable.
ENRICHMENT_STATUS_KEY = "enrichment_status"
ENRICHMENT_STATE_OK = "ok"
ENRICHMENT_STATE_DEGRADED = "degraded"
ENRICHMENT_STATE_UNAVAILABLE = "unavailable"


def enrichment_run_state(
    refusals: List[Tuple[str, GoogleApiFailure, bool]],
) -> str:
    """Reduce the sources that refused during one run to a single verdict.

    Each entry is `(source, failure, decisive)`. Which refusal is decisive is
    the #153 decision, and it travels with the source rather than living in a
    comment on an `if`, so a reader sees the asymmetry in the data.
    """
    if not refusals:
        return ENRICHMENT_STATE_OK
    if any(decisive for _source, _failure, decisive in refusals):
        return ENRICHMENT_STATE_UNAVAILABLE
    return ENRICHMENT_STATE_DEGRADED


class EnrichmentService:
    def __init__(self):
        self.google_maps_key = Config.GOOGLE_MAPS_API_KEY
        self.google_places_key = Config.GOOGLE_PLACES_API_KEY
        self.osm_overpass_url = Config.OSM_OVERPASS_URL
        self.geocoding_service = GeocodingService()

    def enrich_land(self, land_id: int, refresh_coords: bool = False) -> bool:
        """Main method to enrich a land record with external data"""
        try:
            from models import Land
            from app import db

            land = db.session.get(Land, land_id)
            if not land:
                logger.error("Land with ID %s not found", land_id)
                return False

            logger.info(f"Starting enrichment for land {land_id}: {land.title}")

            # Step 1: Geocode the location (missing coords, or refresh requested / low-accuracy coords)
            if (
                (not land.location_lat or not land.location_lon)
                or refresh_coords
                or self._should_refresh_coordinates(land)
            ):
                coordinates_info = self._geocode_with_accuracy(land)
                if coordinates_info:
                    land.location_lat = coordinates_info["lat"]
                    land.location_lon = coordinates_info["lng"]
                    land.location_accuracy = coordinates_info["accuracy"]
                    db.session.commit()
                    logger.info(f"Geocoded land {land_id}: {coordinates_info}")

            if not land.location_lat or not land.location_lon:
                logger.warning(f"Could not geocode land {land_id}, skipping enrichment")
                return False

            # Step 2: Enrich with Google Places data
            places_failure = self._enrich_with_google_places(land)

            # Step 3: Enrich with Google Maps data (distances, travel times)
            maps_failure = self._enrich_with_google_maps(land)

            # Step 4: Enrich with OSM data (fallback and additional POIs)
            osm_failure = self._enrich_with_osm_data(land)

            # Step 5: Analyze environment (views, orientation)
            self._analyze_environment(land)

            # Step 6: Calculate travel times
            from services.travel_time_service import TravelTimeService

            travel_service = TravelTimeService()
            travel_service.calculate_travel_times(land_id)

            # Step 7: Calculate final score
            scoring_service = ScoringService()
            scoring_service.calculate_score(land)

            # A refused source is never a silently successful enrichment, no
            # matter how much local work ran afterwards (#98). Whatever was
            # computed stays committed; the verdict says how complete the run
            # was and the log below names what refused.
            #
            # Google is decisive because it is the source the score reads:
            # `_score_infrastructure_extended` reads only the
            # `<amenity>_available` keys Places writes, so an Overpass refusal
            # cannot move a score. Overpass is a supplementary POI feed that
            # answers 504 whenever both of its two per-IP slots are busy, which
            # is routine rather than exceptional - failing the whole run on
            # that would report failure for lands whose Google data arrived
            # intact, trading a silent false negative for a noisy false one
            # (#153, owner decision 2026-08-09).
            refusals = [
                (source, failure, decisive)
                for source, failure, decisive in (
                    ("Google Places", places_failure, True),
                    ("Google Maps", maps_failure, True),
                    ("OSM Overpass", osm_failure, False),
                )
                if failure is not None
            ]
            state = enrichment_run_state(refusals)
            self._record_enrichment_status(land, state, refusals)

            db.session.commit()

            if refusals:
                logger.error(
                    "Enrichment for land %s is %s: %s",
                    land_id,
                    state,
                    "; ".join(
                        f"{source} unavailable ({failure.describe()})"
                        for source, failure, _decisive in refusals
                    ),
                )

            if state == ENRICHMENT_STATE_UNAVAILABLE:
                return False

            # Not "successfully enriched": a degraded run reaches here too, and
            # the state is what says which of the two this was.
            logger.info("Enrichment for land %s finished: %s", land_id, state)
            return True

        except Exception:
            logger.error("Failed to enrich land %s", land_id, exc_info=True)
            return False

    def _should_refresh_coordinates(self, land) -> bool:
        """Heuristic: refresh coords when we likely geocoded from a too-generic / incomplete address."""
        try:
            if not land.location_lat or not land.location_lon:
                return False
            if (land.location_accuracy or "").lower() == "precise":
                return False

            parts = self._extract_location_parts_from_title(
                getattr(land, "title", None) or ""
            )
            if len(parts) < 2:
                return False

            first = (parts[0] or "").lower()
            streetish = any(
                k in first
                for k in ["calle", "avenida", "camino", "lugar", "plaza", "carretera"]
            )
            return streetish
        except Exception:
            return False

    def _extract_location_parts_from_title(self, title: str) -> List[str]:
        """Parse 'Land in ...' titles into usable address components (drops n/a and price)."""
        if not title:
            return []
        t = title.strip()
        if t.lower().startswith("land in "):
            t = t[8:].strip()

        # Remove trailing price once before splitting by commas (price often contains commas).
        t = re.sub(r"\s*\d[\d\.,]*\s*€.*$", "", t).strip()

        # Split by commas, strip, drop empty/n/a, and strip trailing price markers.
        raw_parts = [p.strip() for p in t.split(",")]
        parts: List[str] = []
        for p in raw_parts:
            if not p:
                continue
            low = p.lower().strip()
            if low in {"n/a", "na", "null", "none"}:
                continue
            # Remove trailing " 65,000 €" / " 65000€" etc.
            p = re.sub(r"\s*\d[\d\.,]*\s*€.*$", "", p).strip()
            if not p:
                continue
            parts.append(p)
        return parts

    def _extract_municipality_from_title(self, title: str) -> Optional[str]:
        """Extract municipality specifically from title like 'Land in camino Pinzalez, Porceyo - Cenero, Gijón'"""

        if not title:
            return None

        logger.debug(f"Extracting municipality from title: '{title}'")

        # Pattern: "Land in [location], [municipality], [province]"
        match = re.search(
            r"Land in\s+[^,]+,\s*([^,]+(?:\s*-\s*[^,]+)*),\s*[^,\d€]+",
            title,
            re.IGNORECASE,
        )
        if match:
            municipality = re.sub(r"\s+", " ", match.group(1).strip())
            if self._is_valid_municipality(municipality):
                logger.debug(
                    f"Extracted municipality from title pattern: '{municipality}'"
                )
                return municipality.title()

        # Prefer the last meaningful comma-separated part (street/hamlet first, municipality last).
        # Example: "Land in La Faza, 280, Caldones, Gijón 85,000 €" -> "Gijón"
        parts = self._extract_location_parts_from_title(title)
        if len(parts) >= 2:
            generic = {"cantabria", "asturias", "spain", "españa"}
            for part in reversed(parts):
                candidate = (part or "").strip()
                if not candidate:
                    continue
                if re.fullmatch(r"\d+", candidate):
                    continue
                if self._normalize_search_text(candidate) in generic:
                    continue
                if self._is_valid_municipality(candidate):
                    logger.debug(
                        f"Extracted municipality from title parts: '{candidate}'"
                    )
                    return candidate.title()

        # Pattern: "Land in [municipality], [details]" (only if the title isn't already split into many parts)
        match = re.search(
            r"Land in\s+([A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]+(?:\s+de\s+[A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]*)*),",
            title,
            re.IGNORECASE,
        )
        if match:
            municipality = re.sub(r"\s+", " ", match.group(1).strip())
            if self._is_valid_municipality(municipality):
                logger.debug(
                    f"Extracted municipality from title pattern: '{municipality}'"
                )
                return municipality.title()

        # Pattern: "Land in [municipality]" (single location)
        match = re.search(
            r"Land in\s+([A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]+(?:\s+de\s+[A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]*)*)\s+\d",
            title,
            re.IGNORECASE,
        )
        if match:
            municipality = re.sub(r"\s+", " ", match.group(1).strip())
            if self._is_valid_municipality(municipality):
                logger.debug(
                    f"Extracted municipality from title pattern: '{municipality}'"
                )
                return municipality.title()

        # Fallback: try to extract last meaningful part before number/price
        # "Land in San Martin de Huerces, 49, La Pedrera" -> "San Martin de Huerces"
        simple_match = re.search(
            r"Land in\s+([A-Za-záéíóúñÁÉÍÓÚÑ][^,\d€]+?)(?:[,\d€]|$)",
            title,
            re.IGNORECASE,
        )
        if simple_match:
            municipality = simple_match.group(1).strip()
            municipality = re.sub(r"\s+", " ", municipality)
            if self._is_valid_municipality(municipality):
                logger.debug(
                    f"Extracted municipality from title fallback: '{municipality}'"
                )
                return municipality.title()

        logger.debug("No municipality found in title")
        return None

    def _is_valid_municipality(self, municipality: str) -> bool:
        """Validate if a municipality name is legitimate"""
        if not municipality or len(municipality) <= 2:
            return False

        # Reject if contains digits
        if re.search(r"\d", municipality):
            return False

        # Define stopwords (common Spanish/English words that aren't locations)
        stopwords = {
            "and",
            "en",
            "de",
            "del",
            "la",
            "el",
            "por",
            "con",
            "y",
            "e",
            "with",
            "for",
            "in",
            "of",
            "the",
            "your",
            "search",
        }

        # Require at least one meaningful token (many Spanish locations start with articles like "La"/"El")
        tokens = [t.strip(".,;:()[]{}\"'").lower() for t in municipality.split()]
        tokens = [t for t in tokens if t]
        meaningful = [
            t for t in tokens if t not in stopwords and t.isalpha() and len(t) > 1
        ]
        if not meaningful:
            return False

        # Require either:
        # a) Contains a comma (e.g., 'Corias, Pravia')
        # b) Mentions country
        # c) Contains at least two meaningful tokens
        if (
            "," in municipality
            or re.search(r"\b(?:Spain|España)\b", municipality, re.IGNORECASE)
            or len(municipality.split()) >= 2
        ):
            return True

        # Single word must be a proper location name (capitalized, reasonable length)
        if (
            municipality.istitle()
            and 3 <= len(municipality) <= 30
            and municipality.isalpha()
        ):
            return True

        return False

    @staticmethod
    def _normalize_search_text(text: str) -> str:
        raw = str(text or "")
        normalized = unicodedata.normalize("NFKD", raw)
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        return normalized.lower()

    def _geocode_with_accuracy(self, land) -> Optional[Dict]:
        """Geocode a land with accuracy determination"""
        if not getattr(land, "municipality", None):
            # Try to re-extract municipality from title if missing (used for filters and better geocoding),
            # but do not bail out if we can't - titles often contain enough address info already.
            extracted = self._extract_municipality_from_title(
                getattr(land, "title", None) or ""
            )
            if extracted:
                logger.info(
                    f"Re-extracted municipality from title for land {land.id}: '{extracted}'"
                )
                land.municipality = extracted
                from app import db

                db.session.commit()
            else:
                logger.warning(
                    f"No municipality found in title for land {land.id}: '{getattr(land, 'title', '')}'"
                )

        municipality = (
            self._clean_municipality(getattr(land, "municipality", None))
            if getattr(land, "municipality", None)
            else None
        )
        if municipality:
            municipality = municipality.replace(" - ", ", ")

        # Try different address formats in order of precision (title-derived
        # first). The label on each attempt is how *specific the query string*
        # looks, guessed before anything is called -- it is not, and must never
        # be recorded as, the accuracy of the result. It decides the order to
        # try and the duplicate policy below; Google decides the accuracy
        # (issue #321).
        address_attempts: List[Dict[str, str]] = []
        seen_addresses: set[str] = set()

        def add_attempt(address: str, specificity: str) -> None:
            candidate = (address or "").strip()
            if not candidate or candidate in seen_addresses:
                return
            seen_addresses.add(candidate)
            address_attempts.append({"address": candidate, "specificity": specificity})

        title_parts = self._extract_location_parts_from_title(
            getattr(land, "title", None) or ""
        )
        if title_parts:
            full = ", ".join(title_parts)
            base_specificity = "precise" if len(title_parts) >= 2 else "approximate"
            add_attempt(f"{full}, Spain", base_specificity)
            if len(title_parts) >= 2:
                tail = ", ".join(title_parts[-2:])
                add_attempt(f"{tail}, Spain", "approximate")
            if len(title_parts) >= 3:
                tail3 = ", ".join(title_parts[-3:])
                add_attempt(f"{tail3}, Spain", "precise")

        if municipality:
            # Try most specific first if we have detailed municipality info.
            if ", " in municipality:
                add_attempt(f"{municipality}, Spain", "precise")
            elif any(
                keyword in municipality.lower()
                for keyword in ["calle", "carretera", "lugar", "avenida", "plaza"]
            ):
                add_attempt(f"{municipality}, Spain", "precise")

            # Always try the municipality as-is (if not too generic).
            if not self._is_too_generic(municipality):
                add_attempt(f"{municipality}, Spain", "approximate")

        # Regional fallbacks: derive from any available hints (title/description/municipality).
        hint_text = " ".join(
            part
            for part in [
                getattr(land, "title", None) or "",
                getattr(land, "municipality", None) or "",
                getattr(land, "description", None) or "",
                getattr(land, "email_subject", None) or "",
            ]
            if part
        )
        regional_fallbacks = self._get_regional_fallbacks(
            hint_text or (municipality or "")
        )
        for fallback in regional_fallbacks:
            add_attempt(fallback, "regional")

        if not address_attempts:
            return None

        for attempt in address_attempts:
            coordinates = self.geocoding_service.geocode_address(attempt["address"])
            if not coordinates:
                continue

            # What Google said about the point it returned. `utils/geocoding.py`
            # derives it from `location_type` -- ROOFTOP and nothing else means
            # "precise" -- and that is the only thing entitled to be stored as
            # the accuracy of this coordinate (issue #321). The attempt's own
            # `specificity` describes the query, was decided before the call,
            # and knows nothing about what came back.
            measured = (coordinates.get("accuracy") or "unknown").strip().lower()
            if measured not in {"precise", "approximate", "unknown"}:
                measured = "unknown"
            result = {
                "lat": coordinates["lat"],
                "lng": coordinates["lng"],
                "accuracy": measured,
            }

            # The duplicate check still keys on the *query*: a specific-looking
            # address that lands on a point another land already owns is the
            # signal that the specific part was ignored, whatever Google then
            # called the result.
            if attempt["specificity"] != "precise":
                logger.info(
                    "Geocoded '%s' (query looked %s), Google says %s (allowing duplicates)",
                    attempt["address"],
                    attempt["specificity"],
                    measured,
                )
                return result

            if self._is_duplicate_coordinates(
                coordinates["lat"], coordinates["lng"], land.id
            ):
                logger.warning(
                    "Skipping duplicate coordinates for specific query '%s'",
                    attempt["address"],
                )
                continue

            logger.info(
                "Geocoded '%s' (query looked %s), Google says %s",
                attempt["address"],
                attempt["specificity"],
                measured,
            )
            return result

        return None

    def _clean_municipality(self, municipality: str) -> Optional[str]:
        """Clean and validate municipality data"""
        if not municipality or not isinstance(municipality, str):
            return None

        # Remove common bad values
        municipality = municipality.strip()
        bad_values = ["and", "n/a", "na", "null", "none", ""]
        if municipality.lower() in bad_values:
            return None

        # Clean up common parsing artifacts
        municipality = municipality.replace('"', "").strip()

        return municipality if len(municipality) > 2 else None

    def _is_too_generic(self, municipality: str) -> bool:
        """Check if municipality is too generic to geocode uniquely"""
        generic_terms = {"spain", "españa", "espana"}
        return self._normalize_search_text(municipality).strip() in generic_terms

    def _get_regional_fallbacks(self, hint_text: str) -> List[str]:
        """Return fallback location contexts to help geocoding when the address is incomplete."""
        fallbacks: List[str] = []
        norm = self._normalize_search_text(hint_text or "")

        # If reference cities are configured and mentioned, use them as a soft hint.
        try:
            from services.settings_service import SettingsService

            for city in SettingsService.get_reference_cities():
                name = str((city or {}).get("name") or "").strip()
                if not name:
                    continue
                if self._normalize_search_text(name) in norm:
                    fallbacks.append(f"{name}, Spain")
                    break
        except Exception:
            pass

        # Last resort: country only.
        if not fallbacks:
            fallbacks.append("Spain")

        return fallbacks

    def _is_duplicate_coordinates(
        self, lat: float, lng: float, current_land_id: int
    ) -> bool:
        """Check if these coordinates already exist for another property"""
        try:
            from models import Land

            # Check if these exact coordinates exist for other properties
            existing = Land.query.filter(
                Land.id != current_land_id,
                Land.location_lat == lat,
                Land.location_lon == lng,
            ).first()

            return existing is not None
        except Exception as e:
            logger.warning(f"Could not check for duplicate coordinates: {e}")
            return False

    def _enrich_with_google_places(self, land) -> Optional[GoogleApiFailure]:
        """Enrich with Google Places API data.

        Returns the failure that stopped it, or None when Google answered.
        A refused request never becomes `<amenity>_available: False` - that is
        a claim about the world, and we did not get to look (#98).
        """
        try:
            lat, lon = float(land.location_lat), float(land.location_lon)

            cached = get_cached_enrichment_data(lat, lon, _PLACES_CACHE_TYPE)
            if isinstance(cached, dict):
                infrastructure_extended = land.infrastructure_extended or {}
                transport = land.transport or {}
                services_quality = land.services_quality or {}

                infrastructure_extended.update(
                    cached.get("infrastructure_extended", {}) or {}
                )
                transport.update(cached.get("transport", {}) or {})
                services_quality.update(cached.get("services_quality", {}) or {})

                land.infrastructure_extended = infrastructure_extended
                land.transport = transport
                land.services_quality = services_quality
                logger.debug("Google Places cache hit for land %s", land.id)
                return None

            if not self.google_places_key:
                logger.warning("Google Places API key not available")
                return GoogleApiFailure(reason=REASON_NO_API_KEY)

            # Search for nearby amenities
            amenities = {
                "supermarket": ["supermarket", "grocery_or_supermarket"],
                "school": ["school", "primary_school", "secondary_school"],
                "hospital": ["hospital", "doctor"],
                "restaurant": ["restaurant"],
                "cafe": ["cafe"],
                "train_station": ["train_station", "subway_station"],
                "bus_station": ["bus_station"],
                "airport": ["airport"],
            }

            infrastructure_extended = land.infrastructure_extended or {}
            transport = land.transport or {}
            services_quality = land.services_quality or {}

            first_failure: Optional[GoogleApiFailure] = None
            failed_amenities: List[str] = []

            for amenity, place_types in amenities.items():
                nearby_places, failure = self._search_nearby_places(
                    lat, lon, place_types
                )
                if failure is not None:
                    first_failure = first_failure or failure
                    failed_amenities.append(amenity)
                    if not nearby_places:
                        # No answer for this amenity: leave its keys untouched
                        # rather than recording "not available".
                        continue

                if amenity in [
                    "supermarket",
                    "school",
                    "hospital",
                    "restaurant",
                    "cafe",
                ]:
                    # Calculate distance to nearest and average rating
                    if nearby_places:
                        infrastructure_extended[f"{amenity}_available"] = True
                        nearest = min(
                            nearby_places, key=lambda x: x.get("distance", float("inf"))
                        )
                        distance_m = nearest.get("distance")
                        if distance_m and distance_m > 0:
                            infrastructure_extended[f"{amenity}_distance"] = distance_m
                            # Calculate estimated travel time (assuming 40 km/h average speed in city)
                            travel_time_min = max(
                                1, round((distance_m / 1000) * 60 / 40)
                            )
                            infrastructure_extended[f"{amenity}_travel_time"] = (
                                travel_time_min
                            )

                        # Get average rating for services
                        if amenity in ["school", "restaurant", "cafe"]:
                            ratings = [
                                p.get("rating", 0)
                                for p in nearby_places
                                if p.get("rating")
                            ]
                            if ratings:
                                services_quality[f"{amenity}_avg_rating"] = sum(
                                    ratings
                                ) / len(ratings)
                    else:
                        infrastructure_extended.setdefault(
                            f"{amenity}_available", False
                        )

                elif amenity in ["train_station", "bus_station", "airport"]:
                    # Calculate transport accessibility
                    places_for_transport = nearby_places
                    if amenity == "airport":
                        places_for_transport, wide_failure = self._airport_candidates(
                            lat, lon, nearby_places
                        )
                        if wide_failure is not None:
                            first_failure = first_failure or wide_failure
                            if amenity not in failed_amenities:
                                # The near search may already have logged it:
                                # name each amenity once, not once per call.
                                failed_amenities.append(amenity)
                            if not places_for_transport:
                                # Nobody answered about airports: leave the
                                # keys untouched rather than recording "no
                                # airport here" for a search that never ran.
                                continue

                    if places_for_transport:
                        transport[f"{amenity}_available"] = True
                        nearest = min(
                            places_for_transport,
                            key=lambda x: x.get("distance", float("inf")),
                        )
                        distance_m = nearest.get("distance")
                        if distance_m and distance_m > 0:
                            transport[f"{amenity}_distance"] = distance_m
                            # Use higher speed for transport hubs (50 km/h average, 80 for long distance)
                            avg_speed = (
                                80 if amenity == "airport" and distance_m > 5000 else 50
                            )
                            travel_time_min = max(
                                1, round((distance_m / 1000) * 60 / avg_speed)
                            )
                            transport[f"{amenity}_travel_time"] = travel_time_min
                    else:
                        transport.setdefault(f"{amenity}_available", False)

            land.infrastructure_extended = infrastructure_extended
            land.transport = transport
            land.services_quality = services_quality

            if first_failure is not None:
                logger.error(
                    "Google Places enrichment for land %s is incomplete: %s "
                    "(no answer for %s)",
                    land.id,
                    first_failure.describe(),
                    ", ".join(failed_amenities),
                )
                # A partial answer must not be cached as the full picture.
                return first_failure

            cache_enrichment_data(
                lat,
                lon,
                _PLACES_CACHE_TYPE,
                {
                    "infrastructure_extended": infrastructure_extended,
                    "transport": transport,
                    "services_quality": services_quality,
                },
                timeout=60 * 60 * 24 * 7,  # 7 days
            )
            return None

        except Exception as e:
            logger.error("Failed to enrich with Google Places", exc_info=True)
            return failure_from_exception(e)

    def _airport_candidates(
        self, lat: float, lon: float, near_places: List[Dict]
    ) -> Tuple[List[Dict], Optional[GoogleApiFailure]]:
        """The places near `lat, lon` that may be recorded as an airport.

        Two corrections over the plain `type=airport` search this replaces,
        both measured on 2026-08-11 against the owner's own data:

        * **Google's `airport` type is not a claim about airports.** At
          43.551663,-6.831426 it returns seven places and every one is a
          helipad, a light-aircraft aerodrome or an aeroclub -- the nearest
          being "Helipuerto Hospital de Jarrio", 6.75 km away. Taking the
          nearest of those is how 145 of the owner's 168 lands came to store
          an "airport" sitting at a median 0.27x the distance of the real
          one, contradicting the road distance to the actual airport shown
          from `Land.distance_airport` on the same page. It also moved a
          score: `ScoringService._score_transport` reads `airport_available`
          and `airport_distance`, and dropping the helipad shifts 158 of the
          168 by a median +1.26 points of `score_total` (it normalises over
          the options it found, so a mediocre one drags the rest down). So
          issue #171's rules apply here, read from the preset that defines
          them rather than copied.

        * **An explicit `radius=` buys nothing past 50 km.** The
          `radius=100000` this replaces was silently clamped: at that same
          coordinate `radius=50000`, `radius=100000` and `radius=200000` all
          returned the *identical* seven places, farthest 45.21 km. Asturias
          Airport is 64.3 km away and could never appear in any of them, so
          the wide attempt was dead code that still cost a call. Text Search
          takes no `radius` -- `location` only biases its ranking -- and has
          no such cap (PR #254 measured it finding that airport first try).

        The second call only happens when the first one answered and nothing
        in it qualifies, so a listing with a real airport nearby still costs
        exactly one request.
        """
        accepted = self._accepted_airports(near_places)
        if accepted:
            return accepted, None

        wide_places, failure = self._search_places_text(
            lat, lon, query="airport", place_type="airport"
        )
        return self._accepted_airports(wide_places), failure

    @staticmethod
    def _accepted_airports(places: List[Dict]) -> List[Dict]:
        """Drop everything #171's rules refuse to call an airport."""
        if _AIRPORT_RULES is None:
            return list(places or [])
        return [
            place
            for place in places or []
            if isinstance(place, dict) and not _AIRPORT_RULES.rejects(place)
        ]

    def _search_places_text(
        self, lat: float, lon: float, query: str, place_type: str
    ) -> Tuple[List[Dict], Optional[GoogleApiFailure]]:
        """Places Text Search, for an answer that legitimately sits far away.

        Same contract as `_search_nearby_places`: an empty list with no
        failure means Google answered and there is nothing; an empty list
        with a failure means we never got to look, and the caller must not
        write that down as an absence (#98).

        Ranked by relevance rather than distance, so a famous airport can
        outrank a closer one. Every result therefore carries its own
        straight-line `distance` and the caller picks the nearest, the same
        way it does for a nearby search.
        """
        if not self.google_places_key:
            return [], GoogleApiFailure(reason=REASON_NO_API_KEY)

        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {
            "query": query,
            "location": f"{lat},{lon}",
            # Narrows the free-text query back to the type being looked for --
            # without it "airport" also matches car rentals and parking lots
            # that merely mention one.
            "type": place_type,
            "key": self.google_places_key,
        }

        try:
            response = request_with_retries(
                requests.get, url, params=params, timeout=15, logger=logger
            )
            payload, failure = read_api_payload(response)
        except Exception as e:
            payload, failure = None, failure_from_exception(e)

        if failure is not None:
            logger.warning(
                "Places text search refused (%s): %s", query, failure.describe()
            )
            return [], failure

        places: List[Dict] = []
        for place in payload.get("results") or []:
            if not isinstance(place, dict):
                continue
            location = (place.get("geometry") or {}).get("location") or {}
            places.append(
                {
                    "name": place.get("name"),
                    "rating": place.get("rating"),
                    "place_id": place.get("place_id"),
                    "types": place.get("types", []),
                    "location": location,
                    "distance": self._calculate_distance(
                        lat,
                        lon,
                        location.get("lat", 0),
                        location.get("lng", 0),
                    ),
                }
            )
        return places, None

    def _search_nearby_places(
        self, lat: float, lon: float, place_types: List[str], radius: int = 5000
    ) -> Tuple[List[Dict], Optional[GoogleApiFailure]]:
        """Search for nearby places using Google Places API.

        Returns the places found and, separately, the first failure. An empty
        list with no failure means Google answered and there is nothing there;
        an empty list with a failure means we never got to look.
        """
        places: List[Dict] = []
        first_failure: Optional[GoogleApiFailure] = None

        for place_type in place_types:
            url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
            params = {
                "location": f"{lat},{lon}",
                "radius": radius,
                "type": place_type,
                "key": self.google_places_key,
            }

            try:
                response = request_with_retries(
                    requests.get, url, params=params, timeout=15, logger=logger
                )
                payload, failure = read_api_payload(response)
            except Exception as e:
                payload, failure = None, failure_from_exception(e)

            if failure is not None:
                logger.warning(
                    "Places search refused (%s): %s", place_type, failure.describe()
                )
                first_failure = first_failure or failure
                continue

            for place in payload.get("results") or []:
                if not isinstance(place, dict):
                    continue
                location = (place.get("geometry") or {}).get("location") or {}
                places.append(
                    {
                        "name": place.get("name"),
                        "rating": place.get("rating"),
                        "place_id": place.get("place_id"),
                        "types": place.get("types", []),
                        "location": location,
                        "distance": self._calculate_distance(
                            lat,
                            lon,
                            location.get("lat", 0),
                            location.get("lng", 0),
                        ),
                    }
                )

            # Rate limiting
            time.sleep(0.1)

        return places, first_failure

    def _enrich_with_google_maps(self, land) -> Optional[GoogleApiFailure]:
        """Enrich with Google Maps data (distances, travel times).

        Returns the failure that stopped it, or None when Google answered.
        """
        try:
            if not self.google_maps_key:
                logger.warning("Google Maps API key not available")
                return GoogleApiFailure(reason=REASON_NO_API_KEY)

            lat, lon = float(land.location_lat), float(land.location_lon)

            # Get distance matrix to major cities/destinations (single batch call)
            destinations = [
                "Madrid, Spain",
                "Barcelona, Spain",
                "Valencia, Spain",
                f"{land.municipality} city center, Spain",
            ]

            transport = land.transport or {}

            dest_sig = "|".join(destinations)
            dest_hash = hashlib.md5(dest_sig.encode()).hexdigest()[:8]
            cache_type = f"distance_matrix_v1:{dest_hash}"

            cached = get_cached_enrichment_data(lat, lon, cache_type)
            if isinstance(cached, dict):
                transport.update(cached)
                land.transport = transport
                logger.debug("Distance matrix cache hit for land %s", land.id)
                return None

            distance_results, failure = self._get_distance_matrix_batch(
                lat, lon, destinations
            )
            for destination, distance_data in zip(destinations, distance_results):
                if not distance_data:
                    continue
                dest_key = destination.split(",")[0].lower().replace(" ", "_")
                transport[f"distance_to_{dest_key}"] = distance_data.get("distance")
                transport[f"duration_to_{dest_key}"] = distance_data.get("duration")

            land.transport = transport

            if failure is not None:
                logger.error(
                    "Distance matrix enrichment for land %s is incomplete: %s",
                    land.id,
                    failure.describe(),
                )
                # Refused answers must not be cached as the computed result.
                return failure

            cache_enrichment_data(
                lat, lon, cache_type, transport, timeout=60 * 60 * 24 * 7
            )
            return None

        except Exception as e:
            logger.error("Failed to enrich with Google Maps", exc_info=True)
            return failure_from_exception(e)

    def _get_distance_matrix_batch(
        self, lat: float, lon: float, destinations: List[str]
    ) -> Tuple[List[Optional[Dict]], Optional[GoogleApiFailure]]:
        """Batch distance matrix lookup (destinations <= 25).

        Returns per-destination results plus the failure, if any. A `None`
        entry alongside no failure means Google answered "no route"; a failure
        means the request never produced an answer at all.
        """
        if not destinations:
            return [], None

        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": f"{lat},{lon}",
            "destinations": "|".join(destinations),
            "mode": "driving",
            "key": self.google_maps_key,
        }

        try:
            response = request_with_retries(
                requests.get, url, params=params, timeout=15, logger=logger
            )
            payload, failure = read_api_payload(response)
        except Exception as e:
            payload, failure = None, failure_from_exception(e)

        if failure is not None:
            logger.warning("Distance matrix refused: %s", failure.describe())
            return [None for _ in destinations], failure

        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows or not rows[0].get("elements"):
            failure = GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE,
                message="no elements in Distance Matrix reply",
            )
            logger.warning("Distance matrix malformed: %s", failure.describe())
            return [None for _ in destinations], failure

        elements = rows[0]["elements"]
        out: List[Optional[Dict]] = []
        for el in elements[: len(destinations)]:
            if not isinstance(el, dict) or el.get("status") != "OK":
                out.append(None)
                continue
            out.append(
                {
                    "distance": (el.get("distance") or {}).get("value"),
                    "duration": (el.get("duration") or {}).get("value"),
                }
            )

        while len(out) < len(destinations):
            out.append(None)

        return out, None

    def _get_distance_matrix(
        self, lat: float, lon: float, destination: str
    ) -> Optional[Dict]:
        """Distance and duration to a single destination.

        Thin wrapper over the batch call so both share one response reader.
        """
        results, _failure = self._get_distance_matrix_batch(lat, lon, [destination])
        return results[0] if results else None

    @staticmethod
    def _write_infrastructure_extended(land, **entries) -> None:
        """Merge `entries` into the land's infrastructure_extended and persist.

        `Land.infrastructure_extended` is a plain `db.Column(JSON)` with no
        `MutableDict` wrapper, so SQLAlchemy detects a change by comparing the
        old value with the new one. Mutating the loaded dict in place and
        assigning it straight back hands it the *same object* twice, the
        attribute never goes dirty, and the flush emits no UPDATE: the write
        survives in memory and is lost on commit. Copy first, always.
        """
        merged = dict(land.infrastructure_extended or {})
        merged.update(entries)
        land.infrastructure_extended = merged

    @staticmethod
    def _write_property_infrastructure_extended(prop, **entries) -> None:
        """Merge `entries` into the property's infrastructure_extended section.

        `Property.enrichment` is a plain `db.Column(JSON)` too, so the same
        copy-before-assign rule as above applies, one level deeper: both the
        blob and the section have to be new objects. `flag_modified` is what
        services/sea_distance_service.py already uses for this column.

        The base is `prop.infrastructure_extended`, the model property, and not
        `enrichment["infrastructure_extended"]` directly. On the 139 rows
        mirrored from `lands` that section only exists under `legacy_land`, and
        the model falls back to it *only while there is no top-level one*. So
        the first write here would otherwise hide every Google-derived key the
        page already showed. Seeding from the model property means the write
        adds the amenity counts and takes nothing away.
        """
        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        section = dict(prop.infrastructure_extended or {})
        section.update(entries)
        enrichment[PROPERTY_INFRASTRUCTURE_KEY] = section
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

    @staticmethod
    def _osm_status_entry(
        current: Dict[str, Any],
        state: str,
        failure: Optional[GoogleApiFailure] = None,
        measured_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the status entry for the last Overpass call.

        Written on every run so an absent `osm_amenities` can be read back as
        either "Overpass answered, nothing nearby" or "we never got to look".
        `measured_at` says when the counts now on the record were actually
        measured, which is not the same as when they were last fetched.

        `current` is the infrastructure section as it stands, so a refusal can
        carry the age of the counts it leaves behind. `Land` and `Property`
        keep that section in different columns and share this shape.
        """
        now = datetime.now(timezone.utc).isoformat()
        status: Dict[str, Any] = {"state": state, "checked_at": now}

        if failure is None:
            # Counts read back from the cache were measured when the cache
            # entry was written, up to a week ago - stamping "now" on them
            # would refresh an age the page then reports as fact.
            status["measured_at"] = measured_at or now
            return status

        status["reason"] = failure.reason
        if failure.http_status is not None:
            status["http_status"] = failure.http_status
        # Counts from an earlier, successful run stay on the record - they
        # were true once and deleting them loses real data. Carry their
        # age forward so the page can label them as stale instead of
        # rendering them as though this run had just measured them.
        previous = current.get(OSM_STATUS_KEY)
        if isinstance(previous, dict) and previous.get("measured_at"):
            status["measured_at"] = previous["measured_at"]
        return status

    def _record_osm_status(
        self,
        land,
        state: str,
        failure: Optional[GoogleApiFailure] = None,
        measured_at: Optional[str] = None,
    ) -> None:
        """Stamp the outcome of the last Overpass call onto the land."""
        status = self._osm_status_entry(
            land.infrastructure_extended or {}, state, failure, measured_at
        )
        self._write_infrastructure_extended(land, **{OSM_STATUS_KEY: status})

    def _record_property_osm_status(
        self,
        prop,
        state: str,
        failure: Optional[GoogleApiFailure] = None,
        measured_at: Optional[str] = None,
    ) -> None:
        """Stamp the outcome of the last Overpass call onto the property."""
        status = self._osm_status_entry(
            prop.infrastructure_extended or {}, state, failure, measured_at
        )
        self._write_property_infrastructure_extended(prop, **{OSM_STATUS_KEY: status})

    def _record_enrichment_status(
        self,
        land,
        state: str,
        refusals: List[Tuple[str, GoogleApiFailure, bool]],
    ) -> None:
        """Stamp the verdict of this enrichment run onto the land.

        Written on every run that got as far as calling the sources, so a
        reader can tell a complete run from one that only lost an advisory
        source without re-deriving it from the log. `decisive` is carried per
        refusal because it is what turned this run's state into `unavailable`
        rather than `degraded`, and an operator reading the JSON should not have
        to know the rule to see which refusal mattered.
        """
        status: Dict = {
            "state": state,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        refused = []
        for source, failure, decisive in refusals:
            entry: Dict = {
                "source": source,
                "reason": failure.reason,
                "decisive": decisive,
            }
            if failure.http_status is not None:
                entry["http_status"] = failure.http_status
            refused.append(entry)
        if refused:
            status["refused"] = refused

        self._write_infrastructure_extended(land, **{ENRICHMENT_STATUS_KEY: status})

    def _osm_refusal(self, land, failure: GoogleApiFailure) -> GoogleApiFailure:
        """Log a refused Overpass call, stamp it on the land, hand it back."""
        logger.error(
            "OSM amenities unavailable for land %s: %s", land.id, failure.describe()
        )
        self._record_osm_status(land, OSM_STATE_UNAVAILABLE, failure)
        return failure

    def _fetch_osm_amenities(self, lat: float, lon: float) -> OsmAmenityReading:
        """Count amenities near a point, or say why Overpass refused.

        The one Overpass amenity client in the app: `Land` and `Property` both
        write what it returns, so the three refusals overpass-api.de delivers
        are read the same way for both, and a second client cannot drift away
        from them (#152). Those three, all measured against the live instance:

        * `406 Not Acceptable` for a User-Agent it dislikes -- the default
          `python-requests/x.y.z`, and any token carrying a parenthetical
          comment (#144);
        * `504` while both of its two per-IP slots are busy, which is a queue
          rather than a broken request;
        * a server-side failure delivered *inside a 200*, as a `remark` in the
          body with an empty or absent `elements`.

        A refusal never comes back as empty counts. That is the #98 mistake,
        and it would then be cached for a week.
        """
        try:
            cached = get_cached_enrichment_data(lat, lon, OSM_CACHE_KEY)
            if isinstance(cached, dict) and isinstance(cached.get("counts"), dict):
                logger.debug("OSM amenities cache hit for %s,%s", lat, lon)
                return OsmAmenityReading(
                    counts=cached["counts"], measured_at=cached.get("measured_at")
                )

            amenities = "|".join(OSM_AMENITY_KINDS)
            around = f"around:{OSM_AMENITY_RADIUS_M},{lat},{lon}"
            overpass_query = f"""
            [out:json][timeout:25];
            (
              node["amenity"~"^({amenities})$"]({around});
              way["amenity"~"^({amenities})$"]({around});
              relation["amenity"~"^({amenities})$"]({around});
            );
            out center;
            """

            elements, transport_failure = self._overpass_elements(overpass_query)
            if transport_failure is not None:
                return OsmAmenityReading(failure=transport_failure)

            amenity_counts: Dict[str, int] = {}
            for element in elements:
                if not isinstance(element, dict):
                    continue
                amenity = (element.get("tags") or {}).get("amenity")
                if amenity:
                    amenity_counts[amenity] = amenity_counts.get(amenity, 0) + 1

            # An empty dict here is a real answer: Overpass looked and there is
            # nothing within the radius.
            measured_at = datetime.now(timezone.utc).isoformat()

            # Best effort, and deliberately last. Overpass answered and the
            # counts are the caller's to store; a cache that refuses the write
            # is a slower next run, not a failed lookup, so it must not fall
            # through to the handler below and relabel this call "unavailable".
            try:
                cache_enrichment_data(
                    lat,
                    lon,
                    OSM_CACHE_KEY,
                    {"counts": amenity_counts, "measured_at": measured_at},
                    timeout=60 * 60 * 24 * 7,
                )
            except Exception:
                logger.warning(
                    "Could not cache OSM amenities for %s,%s", lat, lon, exc_info=True
                )
            return OsmAmenityReading(counts=amenity_counts, measured_at=measured_at)

        except Exception as exc:
            logger.error("Failed to fetch OSM amenities", exc_info=True)
            return OsmAmenityReading(failure=failure_from_exception(exc))

    def _overpass_elements(self, overpass_query: str):
        """One Overpass round-trip: elements list, or why it refused.

        Extracted from `_fetch_osm_amenities` so the supermarket-reach lookup
        below cannot grow its own transport — the gate, the User-Agent and the
        three measured refusals (#144: 406 for a bad UA, 504 while both per-IP
        slots are busy, and a `remark` inside a 200) live exactly once.
        Returns `(elements, None)` or `(None, failure)`.
        """
        try:
            response = request_with_retries(
                requests.post,
                self.osm_overpass_url,
                data=overpass_query,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    # Without this the default `python-requests/x.y.z` is
                    # sent and every call comes back 406. See HTTP_USER_AGENT.
                    "User-Agent": HTTP_USER_AGENT,
                },
                # A 504 here means "both slots are busy", and a slot frees
                # up in roughly a minute. The default half-second backoff
                # gives up long before then, so widen it: 8+16+32 out-waits
                # a typical turnover without stalling a bulk run for
                # minutes on a *fallback* source.
                max_attempts=4,
                backoff_base=8.0,
                backoff_max=90.0,
                timeout=60,
                logger=logger,
                # Every Overpass caller in this process shares one gate, and
                # it covers every attempt: the retries are what a bulk run
                # spends most of its requests on.
                gate=OVERPASS_GATE,
            )
        except requests.RequestException as exc:
            return None, failure_from_exception(exc)

        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            return None, GoogleApiFailure(
                reason=REASON_HTTP_ERROR,
                http_status=status_code if isinstance(status_code, int) else None,
            )

        try:
            osm_data = response.json()
        except ValueError as exc:
            return None, GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE, message=str(exc)
            )

        if not isinstance(osm_data, dict):
            return None, GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE,
                message=f"expected an object, got {type(osm_data).__name__}",
            )

        # Overpass reports a server-side failure *inside a 200*: the body
        # carries a `remark` ("runtime error: Query timed out in ...",
        # "Query run out of memory") and, usually, an empty `elements`.
        # Reading that as "nothing nearby" is the very defect this method
        # exists to remove, and it would then be cached for a week.
        remark = osm_data.get("remark")
        if remark:
            return None, GoogleApiFailure(
                reason=OSM_REASON_QUERY_ERROR, message=str(remark)
            )

        # No `elements` at all is not an empty answer either - a well-formed
        # Overpass result always carries the key, even when it is empty.
        elements = osm_data.get("elements")
        if not isinstance(elements, list):
            return None, GoogleApiFailure(
                reason=REASON_MALFORMED_RESPONSE,
                message="response carries no elements list",
            )

        return elements, None

    def fetch_osm_supermarket_reach(self, lat: float, lon: float):
        """The named supermarkets within driving reach, nearest first.

        The QoL card's data (agreed proposal, D16 as amended in review round
        2): factual rows — name, brand, shop type, straight-line km — sorted
        by distance, no brand ranking. `shop=supermarket|convenience` within
        12 km; an empty list is a measured answer (`osm_empty` upstream), a
        refusal comes back as a failure and must never render as "none".
        """
        try:
            cached = get_cached_enrichment_data(lat, lon, OSM_SUPERMARKET_CACHE_KEY)
            if isinstance(cached, dict) and isinstance(cached.get("items"), list):
                logger.debug("OSM supermarket cache hit for %s,%s", lat, lon)
                return OsmSupermarketReading(
                    items=cached["items"], measured_at=cached.get("measured_at")
                )

            around = f"around:{OSM_SUPERMARKET_RADIUS_M},{lat},{lon}"
            overpass_query = f"""
            [out:json][timeout:25];
            (
              node["shop"~"^(supermarket|convenience)$"]({around});
              way["shop"~"^(supermarket|convenience)$"]({around});
              relation["shop"~"^(supermarket|convenience)$"]({around});
            );
            out center tags;
            """

            elements, transport_failure = self._overpass_elements(overpass_query)
            if transport_failure is not None:
                return OsmSupermarketReading(failure=transport_failure)

            items = []
            for element in elements:
                if not isinstance(element, dict):
                    continue
                tags = element.get("tags") or {}
                el_lat = element.get("lat")
                el_lon = element.get("lon")
                if el_lat is None or el_lon is None:
                    center = element.get("center") or {}
                    el_lat = center.get("lat")
                    el_lon = center.get("lon")
                if el_lat is None or el_lon is None:
                    continue
                items.append(
                    {
                        # Unnamed shops stay explicit rather than dropped: a
                        # village shop with no name tag is still a shop.
                        "name": tags.get("name"),
                        "brand": tags.get("brand"),
                        "shop": tags.get("shop"),
                        "lat": el_lat,
                        "lon": el_lon,
                        "distance_km": round(
                            haversine_m(lat, lon, float(el_lat), float(el_lon))
                            / 1000.0,
                            1,
                        ),
                    }
                )
            items.sort(key=lambda item: item["distance_km"])
            items = items[:OSM_SUPERMARKET_TOP_N]

            measured_at = datetime.now(timezone.utc).isoformat()
            try:
                cache_enrichment_data(
                    lat,
                    lon,
                    OSM_SUPERMARKET_CACHE_KEY,
                    {"items": items, "measured_at": measured_at},
                    timeout=60 * 60 * 24 * 7,
                )
            except Exception:
                logger.warning(
                    "Could not cache supermarket reach for %s,%s",
                    lat,
                    lon,
                    exc_info=True,
                )
            return OsmSupermarketReading(items=items, measured_at=measured_at)

        except Exception as exc:
            logger.error("Failed to fetch OSM supermarket reach", exc_info=True)
            return OsmSupermarketReading(failure=failure_from_exception(exc))

    def _enrich_with_osm_data(self, land) -> Optional[GoogleApiFailure]:
        """Write nearby-amenity counts onto a legacy `Land`.

        Returns the failure that stopped it, or None when Overpass answered.
        """
        try:
            lat, lon = float(land.location_lat), float(land.location_lon)
        except (TypeError, ValueError) as exc:
            return self._osm_refusal(land, failure_from_exception(exc))

        reading = self._fetch_osm_amenities(lat, lon)
        if reading.failure is not None:
            return self._osm_refusal(land, reading.failure)

        try:
            self._write_infrastructure_extended(land, osm_amenities=reading.counts)
            self._record_osm_status(land, OSM_STATE_OK, measured_at=reading.measured_at)
        except Exception as exc:
            failure = failure_from_exception(exc)
            logger.error("Failed to store OSM amenities on land", exc_info=True)
            return failure
        return None

    def enrich_osm_amenities(
        self, prop, *, commit: bool = True
    ) -> Optional[GoogleApiFailure]:
        """Measure nearby amenities for a universal `Property`.

        Free and keyless: OpenStreetMap, no Google billing (#152). Until this
        existed the amenity lookup was reachable only from the legacy `Land`
        endpoints, so 213 of 352 listings had an Extended Infrastructure card
        that was simply absent -- which reads as "nothing nearby" rather than
        "never asked".

        Lives here rather than in `PropertyEnrichmentService` because the
        Overpass client and its refusal handling do, and the issue asks for one
        client, not two. Returns the failure, or None when Overpass answered.
        """
        from app import db

        lat = _osm_coordinate(prop.location_lat, limit=90.0)
        lon = _osm_coordinate(prop.location_lon, limit=180.0)

        if lat is None or lon is None:
            # Geocoding is a paid Google call; do not reach for one to count
            # cafés. Recorded so the page can say "not asked" rather than
            # showing an absence that reads like a measurement.
            failure = GoogleApiFailure(reason=OSM_REASON_NO_COORDINATES)
            logger.info(
                "OSM amenities skipped for property %s: no usable coordinates",
                prop.id,
            )
            self._record_property_osm_status(prop, OSM_STATE_UNAVAILABLE, failure)
            if commit:
                db.session.commit()
            return failure

        reading = self._fetch_osm_amenities(lat, lon)
        if reading.failure is not None:
            logger.error(
                "OSM amenities unavailable for property %s: %s",
                prop.id,
                reading.failure.describe(),
            )
            self._record_property_osm_status(
                prop, OSM_STATE_UNAVAILABLE, reading.failure
            )
        else:
            self._write_property_infrastructure_extended(
                prop, osm_amenities=reading.counts
            )
            self._record_property_osm_status(
                prop, OSM_STATE_OK, measured_at=reading.measured_at
            )

        if commit:
            db.session.commit()
        return reading.failure

    def _analyze_environment(self, land):
        """Analyze environment features like views and orientation"""
        try:
            environment = land.environment or {}

            # Analyze description and location for view keywords
            description = (land.description or "").lower()
            title = (land.title or "").lower()

            # Only use description and title for sea view detection, NOT municipality
            # Municipality like "Gijón" doesn't mean the property has sea view
            text_for_views = f"{description} {title}"

            # Sea view detection - only from explicit mentions in description/title
            # NOT from city names or general coastal region
            sea_keywords = [
                "vista al mar",
                "vistas al mar",
                "vista mar",
                "vistas mar",
                "sea view",
                "sea views",
                "ocean view",
                "ocean views",
                "frente al mar",
                "primera linea",
                "primera línea",
                "beach front",
                "beachfront",
                "waterfront",
                "junto al mar",
                "cerca del mar",
                "a pie de playa",
            ]
            environment["sea_view"] = any(
                keyword in text_for_views for keyword in sea_keywords
            )

            # Mountain view detection - only from explicit mentions
            mountain_keywords = [
                "vista montaña",
                "vistas montaña",
                "vista a la montaña",
                "vistas a la montaña",
                "mountain view",
                "mountain views",
                "vista sierra",
                "vistas sierra",
                "picos de europa",
                "cordillera cantábrica",
                "cordillera cantabrica",
            ]
            environment["mountain_view"] = any(
                keyword in text_for_views for keyword in mountain_keywords
            )

            # Forest/nature view detection
            forest_keywords = [
                "vista bosque",
                "bosque",
                "rodeado de bosque",
                "rodeada de bosque",
                "rodeado de naturaleza",
                "rodeada de naturaleza",
                "entorno natural",
                "zona arbolada",
                "arbolado",
                "mucho verde",
                "forest view",
                "woodland",
                "surrounded by nature",
            ]
            environment["forest_view"] = any(
                keyword in text_for_views for keyword in forest_keywords
            )

            # Orientation detection
            orientation_keywords = {
                "norte": "north",
                "sur": "south",
                "este": "east",
                "oeste": "west",
                "noreste": "northeast",
                "noroeste": "northwest",
                "sureste": "southeast",
                "suroeste": "southwest",
            }

            for (
                spanish_orientation,
                english_orientation,
            ) in orientation_keywords.items():
                if spanish_orientation in description:
                    environment["orientation"] = english_orientation
                    break

            land.environment = environment

        except Exception:
            logger.error("Failed to analyze environment", exc_info=True)

    def _calculate_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Calculate distance between two points in meters using Haversine formula"""
        import math

        R = 6371000  # Earth's radius in meters

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = math.sin(delta_lat / 2) * math.sin(delta_lat / 2) + math.cos(
            lat1_rad
        ) * math.cos(lat2_rad) * math.sin(delta_lon / 2) * math.sin(delta_lon / 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c
