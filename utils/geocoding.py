import logging
import requests
from typing import Dict, List, Optional
from config import Config
from utils.http import request_with_retries

logger = logging.getLogger(__name__)


# --- Nominatim -> Google vocabulary (issue #342, residue item 4) ------------
#
# `services/property_location_service.py` has exactly one rule for refusing a
# too-coarse geocoding result (`_is_too_coarse`, issue #331) and one for a
# result about the wrong place (`_result_province`, issue #348). Both read
# Google's own vocabulary -- `types` and a `postal_code` address component --
# and until now the Nominatim fallback supplied neither, so a Nominatim
# country-level answer was invisible to the rule that exists. The fix is to
# translate the fallback's answer into that vocabulary, not to add a second
# copy of either rule.
#
# Nominatim's `place_rank` is documented at
# https://nominatim.org/release-docs/latest/customize/Ranking/ -- countries
# sit at rank <= 4, states/regions at 5-8, counties/provinces at 9-12.
# `addresstype` (Nominatim >= 4) names the same scale in words and is checked
# first: it is less likely to drift across a Nominatim release than the
# numeric boundaries are.
_NOMINATIM_COUNTRY_TYPES = {"country"}
_NOMINATIM_REGION_TYPES = {"state", "region"}
_NOMINATIM_PROVINCE_TYPES = {"province", "county"}
_NOMINATIM_LOCALITY_TYPES = {
    "city",
    "town",
    "village",
    "municipality",
    "hamlet",
    "suburb",
    "borough",
}
_NOMINATIM_ROUTE_TYPES = {"road", "highway", "residential", "pedestrian"}


def _nominatim_result_types(result: Dict) -> List[str]:
    """Map a Nominatim `addressdetails=1` answer to Google's `types` list.

    Only the coarse levels `_is_too_coarse` refuses need to land precisely;
    the finer mapping is best-effort. An answer carrying neither
    `addresstype` nor a usable `place_rank` maps to `[]` -- unknown scale is
    not a claim of precision.
    """
    addresstype = str(result.get("addresstype") or "").strip().lower()
    try:
        place_rank = (
            int(result["place_rank"]) if result.get("place_rank") is not None else None
        )
    except (TypeError, ValueError):
        place_rank = None

    if addresstype in _NOMINATIM_COUNTRY_TYPES or (
        place_rank is not None and place_rank <= 4
    ):
        return ["country"]
    if addresstype in _NOMINATIM_REGION_TYPES or (
        place_rank is not None and 5 <= place_rank <= 8
    ):
        return ["administrative_area_level_1"]
    if addresstype in _NOMINATIM_PROVINCE_TYPES or (
        place_rank is not None and 9 <= place_rank <= 12
    ):
        return ["administrative_area_level_2"]
    if addresstype in _NOMINATIM_LOCALITY_TYPES or (
        place_rank is not None and 13 <= place_rank <= 21
    ):
        return ["locality"]
    if addresstype in _NOMINATIM_ROUTE_TYPES:
        return ["route"]
    if addresstype:
        # Named but finer than a locality -- a house, building or POI, closer
        # to a single property than to a place.
        return ["premise"]
    return []


def _nominatim_address_components(result: Dict) -> List[Dict]:
    """Map Nominatim's `address` dict into Google's `address_components` shape.

    Two fields, and both exist because
    `services.property_location_service` reads them: `postcode` for the
    province check, and the municipality for the check GEO-001 added.

    The municipality is whichever of `city` / `town` / `village` /
    `municipality` / `hamlet` the answer carries, mapped to `locality` --
    Nominatim names the level it found rather than a fixed one, and the
    reader treats them alike. Without it the municipality check was
    structurally blind on this path: every fallback answer produced
    `result_names_no_municipality`, which is the "nobody could tell" state,
    for answers that named a municipality perfectly clearly. That is #98's
    defect wearing the shape of a missing mapping.
    """
    address = result.get("address")
    if not isinstance(address, dict):
        return []

    components: List[Dict] = []
    postcode = str(address.get("postcode") or "").strip()
    if postcode:
        components.append(
            {"long_name": postcode, "short_name": postcode, "types": ["postal_code"]}
        )
    for key in ("city", "town", "village", "municipality", "hamlet"):
        locality = str(address.get(key) or "").strip()
        if locality:
            components.append(
                {
                    "long_name": locality,
                    "short_name": locality,
                    "types": ["locality", "political"],
                }
            )
            break
    return components


class GeocodingService:
    def __init__(self):
        self.google_maps_key = Config.GOOGLE_MAPS_API_KEY

    def geocode_address(self, address: str) -> Optional[Dict]:
        """Geocode an address using Google Maps Geocoding API"""
        try:
            if not self.google_maps_key:
                logger.warning("Google Maps API key not available for geocoding")
                return self._fallback_geocoding(address)

            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                "address": address,
                "key": self.google_maps_key,
                "region": "es",  # Bias results to Spain
            }

            response = request_with_retries(
                requests.get, url, params=params, timeout=10, logger=logger
            )
            if response.status_code == 200:
                data = response.json()

                if data.get("status") == "OK" and data.get("results"):
                    result = data["results"][0]
                    location = result["geometry"]["location"]
                    location_type = result.get("geometry", {}).get("location_type")

                    accuracy = "unknown"
                    if location_type == "ROOFTOP":
                        accuracy = "precise"
                    elif location_type in {
                        "RANGE_INTERPOLATED",
                        "GEOMETRIC_CENTER",
                        "APPROXIMATE",
                    }:
                        accuracy = "approximate"

                    logger.info(f"Google geocoding successful for '{address}'")
                    return {
                        "lat": location["lat"],
                        "lng": location["lng"],
                        "formatted_address": result["formatted_address"],
                        "address_components": result.get("address_components", []),
                        "location_type": location_type,
                        # What Google says the matched place *is* -- "country",
                        # "locality", "route" and so on. `location_type` only
                        # says how the point was derived, so it cannot tell a
                        # street centroid from an entire country: both are
                        # APPROXIMATE. Callers that care about scale need this
                        # (issue #331).
                        "types": result.get("types", []),
                        "accuracy": accuracy,
                    }
                else:
                    status = data.get("status", "UNKNOWN")
                    error_message = data.get("error_message", "")
                    logger.warning(
                        f"Google geocoding failed for '{address}': {status} - {error_message}"
                    )
                    return self._fallback_geocoding(address)
            else:
                logger.error(
                    f"Google geocoding API request failed with status {response.status_code} for '{address}'"
                )
                return self._fallback_geocoding(address)

        except Exception:
            logger.error("Geocoding error for '%s'", address, exc_info=True)
            return self._fallback_geocoding(address)

    def _fallback_geocoding(self, address: str) -> Optional[Dict]:
        """Fallback geocoding using Nominatim (OpenStreetMap)"""
        try:
            # Use Nominatim as fallback
            url = "https://nominatim.openstreetmap.org/search"
            params = {
                "q": address,
                "format": "json",
                "countrycodes": "es",
                "limit": 1,
                # Documented Nominatim parameter: returns an "address"
                # breakdown (postcode, state, ...) plus "addresstype" and
                # "place_rank" on the result itself, which is what lets this
                # answer be mapped into Google's vocabulary below.
                "addressdetails": 1,
            }

            headers = {"User-Agent": "Idealista-Property-Watch/1.0"}

            response = request_with_retries(
                requests.get,
                url,
                params=params,
                headers=headers,
                timeout=10,
                logger=logger,
            )
            if response.status_code == 200:
                data = response.json()

                if data:
                    result = data[0]
                    return {
                        "lat": float(result["lat"]),
                        "lng": float(result["lon"]),
                        "formatted_address": result["display_name"],
                        "address_components": _nominatim_address_components(result),
                        "location_type": None,
                        # See _nominatim_result_types: maps this answer onto
                        # Google's vocabulary so the one existing coarse-result
                        # rule in property_location_service.py covers both
                        # providers (issue #342, residue item 4).
                        "types": _nominatim_result_types(result),
                        "accuracy": "approximate",
                    }

            logger.warning(f"Fallback geocoding also failed for '{address}'")
            return None

        except Exception:
            logger.error("Fallback geocoding error for '%s'", address, exc_info=True)
            return None

    def reverse_geocode(self, lat: float, lng: float) -> Optional[Dict]:
        """Reverse geocode coordinates to get address"""
        try:
            if not self.google_maps_key:
                return self._fallback_reverse_geocoding(lat, lng)

            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                "latlng": f"{lat},{lng}",
                "key": self.google_maps_key,
                "language": "es",
            }

            response = request_with_retries(
                requests.get, url, params=params, timeout=10, logger=logger
            )
            if response.status_code == 200:
                data = response.json()

                if data.get("status") == "OK" and data.get("results"):
                    result = data["results"][0]
                    return {
                        "formatted_address": result["formatted_address"],
                        "address_components": result.get("address_components", []),
                    }

            return self._fallback_reverse_geocoding(lat, lng)

        except Exception:
            logger.error("Reverse geocoding error for %s,%s", lat, lng, exc_info=True)
            return None

    def _fallback_reverse_geocoding(self, lat: float, lng: float) -> Optional[Dict]:
        """Fallback reverse geocoding using Nominatim"""
        try:
            url = "https://nominatim.openstreetmap.org/reverse"
            params = {"lat": lat, "lon": lng, "format": "json", "accept-language": "es"}

            headers = {"User-Agent": "Idealista-Property-Watch/1.0"}

            response = request_with_retries(
                requests.get,
                url,
                params=params,
                headers=headers,
                timeout=10,
                logger=logger,
            )
            if response.status_code == 200:
                data = response.json()

                return {
                    "formatted_address": data.get("display_name", ""),
                    "address_components": [],
                }

            return None

        except Exception:
            logger.error(
                "Fallback reverse geocoding error for %s,%s", lat, lng, exc_info=True
            )
            return None
