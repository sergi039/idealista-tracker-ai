import os
import logging
import requests
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class GeocodingService:
    def __init__(self):
        self.google_maps_key = os.environ.get("GOOGLE_MAPS_API", "") or os.environ.get("Google_api", "") or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        
    def geocode_address(self, address: str) -> Optional[Dict]:
        """Geocode an address using Google Maps Geocoding API"""
        try:
            if not self.google_maps_key:
                logger.warning("Google Maps API key not available for geocoding")
                return self._fallback_geocoding(address)
            
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                'address': address,
                'key': self.google_maps_key,
                'region': 'es'  # Bias results to Spain
            }
            
            response = requests.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                
                if data.get('status') == 'OK' and data.get('results'):
                    result = data['results'][0]
                    location = result['geometry']['location']
                    
                    return {
                        'lat': location['lat'],
                        'lng': location['lng'],
                        'formatted_address': result['formatted_address'],
                        'address_components': result.get('address_components', [])
                    }
                else:
                    logger.warning(f"Geocoding failed for '{address}': {data.get('status')}")
                    return self._fallback_geocoding(address)
            else:
                logger.error(f"Geocoding API request failed: {response.status_code}")
                return self._fallback_geocoding(address)
                
        except Exception as e:
            logger.error(f"Geocoding error for '{address}': {str(e)}")
            return self._fallback_geocoding(address)
    
    def _fallback_geocoding(self, address: str) -> Optional[Dict]:
        """Fallback geocoding using Nominatim (OpenStreetMap)"""
        try:
            # Use Nominatim as fallback
            url = "https://nominatim.openstreetmap.org/search"
            params = {
                'q': address,
                'format': 'json',
                'countrycodes': 'es',
                'limit': 1
            }
            
            headers = {
                'User-Agent': 'Idealista-Land-Watch/1.0'
            }
            
            response = requests.get(url, params=params, headers=headers)
            if response.status_code == 200:
                data = response.json()
                
                if data:
                    result = data[0]
                    return {
                        'lat': float(result['lat']),
                        'lng': float(result['lon']),
                        'formatted_address': result['display_name'],
                        'address_components': []
                    }
            
            logger.warning(f"Fallback geocoding also failed for '{address}'")
            return None
            
        except Exception as e:
            logger.error(f"Fallback geocoding error for '{address}': {str(e)}")
            return None
    
    def reverse_geocode(self, lat: float, lng: float) -> Optional[Dict]:
        """Reverse geocode coordinates to get address"""
        try:
            if not self.google_maps_key:
                return self._fallback_reverse_geocoding(lat, lng)
            
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                'latlng': f"{lat},{lng}",
                'key': self.google_maps_key,
                'language': 'es'
            }
            
            response = requests.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                
                if data.get('status') == 'OK' and data.get('results'):
                    result = data['results'][0]
                    return {
                        'formatted_address': result['formatted_address'],
                        'address_components': result.get('address_components', [])
                    }
            
            return self._fallback_reverse_geocoding(lat, lng)
            
        except Exception as e:
            logger.error(f"Reverse geocoding error for {lat},{lng}: {str(e)}")
            return None
    
    def _fallback_reverse_geocoding(self, lat: float, lng: float) -> Optional[Dict]:
        """Fallback reverse geocoding using Nominatim"""
        try:
            url = "https://nominatim.openstreetmap.org/reverse"
            params = {
                'lat': lat,
                'lon': lng,
                'format': 'json',
                'accept-language': 'es'
            }
            
            headers = {
                'User-Agent': 'Idealista-Land-Watch/1.0'
            }
            
            response = requests.get(url, params=params, headers=headers)
            if response.status_code == 200:
                data = response.json()
                
                return {
                    'formatted_address': data.get('display_name', ''),
                    'address_components': []
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Fallback reverse geocoding error for {lat},{lng}: {str(e)}")
            return None
