import logging
import requests
from typing import Dict, Optional
from config import Config
from utils.http import request_with_retries

logger = logging.getLogger(__name__)


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
            params = {"q": address, "format": "json", "countrycodes": "es", "limit": 1}

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
                        "address_components": [],
                        "location_type": None,
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
