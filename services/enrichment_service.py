import os
import re
import logging
import hashlib
import requests
import time
from typing import Dict, List, Optional
from utils.geocoding import GeocodingService
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from config import Config
from services.scoring_service import ScoringService

logger = logging.getLogger(__name__)

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
            
            land = Land.query.get(land_id)
            if not land:
                logger.error(f"Land with ID {land_id} not found")
                return False
            
            logger.info(f"Starting enrichment for land {land_id}: {land.title}")
            
            # Step 1: Geocode the location (missing coords, or refresh requested / low-accuracy coords)
            if (not land.location_lat or not land.location_lon) or refresh_coords or self._should_refresh_coordinates(land):
                coordinates_info = self._geocode_with_accuracy(land)
                if coordinates_info:
                    land.location_lat = coordinates_info['lat']
                    land.location_lon = coordinates_info['lng']
                    land.location_accuracy = coordinates_info['accuracy']
                    db.session.commit()
                    logger.info(f"Geocoded land {land_id}: {coordinates_info}")
            
            if not land.location_lat or not land.location_lon:
                logger.warning(f"Could not geocode land {land_id}, skipping enrichment")
                return False
            
            # Step 2: Enrich with Google Places data
            self._enrich_with_google_places(land)
            
            # Step 3: Enrich with Google Maps data (distances, travel times)
            self._enrich_with_google_maps(land)
            
            # Step 4: Enrich with OSM data (fallback and additional POIs)
            self._enrich_with_osm_data(land)
            
            # Step 5: Analyze environment (views, orientation)
            self._analyze_environment(land)
            
            # Step 6: Calculate travel times
            from services.travel_time_service import TravelTimeService
            travel_service = TravelTimeService()
            travel_service.calculate_travel_times(land_id)
            
            
            # Step 7: Calculate final score
            scoring_service = ScoringService()
            scoring_service.calculate_score(land)
            
            db.session.commit()
            logger.info(f"Successfully enriched land {land_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to enrich land {land_id}: {str(e)}")
            return False

    def _should_refresh_coordinates(self, land) -> bool:
        """Heuristic: refresh coords when we likely geocoded from a too-generic / incomplete address."""
        try:
            if not land.location_lat or not land.location_lon:
                return False
            if (land.location_accuracy or "").lower() == "precise":
                return False

            parts = self._extract_location_parts_from_title(getattr(land, "title", None) or "")
            if len(parts) < 2:
                return False

            first = (parts[0] or "").lower()
            streetish = any(k in first for k in ["calle", "avenida", "camino", "lugar", "plaza", "carretera"])
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
        
        # Pattern for "Land in [path], [municipality], [province]" 
        # Examples: "Land in camino Pinzalez, Porceyo - Cenero, Gijón"
        municipality_patterns = [
            # Pattern: "Land in [location], [municipality], [province]"  
            r'Land in\s+[^,]+,\s*([^,]+(?:\s*-\s*[^,]+)*),\s*[^,\d€]+',
            # Pattern: "Land in [municipality], [details]"
            r'Land in\s+([A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]+(?:\s+de\s+[A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]*)*),',
            # Pattern: "Land in [municipality]" (single location)
            r'Land in\s+([A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]+(?:\s+de\s+[A-Za-záéíóúñÁÉÍÓÚÑ][A-Za-záéíóúñ\s]*)*)\s+\d'
        ]
        
        for pattern in municipality_patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                municipality = match.group(1).strip()
                # Clean and validate
                municipality = re.sub(r'\s+', ' ', municipality)
                if self._is_valid_municipality(municipality):
                    logger.debug(f"Extracted municipality from title pattern: '{municipality}'")
                    return municipality.title()
        
        # Fallback: try to extract last meaningful part before number/price
        # "Land in San Martin de Huerces, 49, La Pedrera" -> "San Martin de Huerces"
        simple_match = re.search(r'Land in\s+([A-Za-záéíóúñÁÉÍÓÚÑ][^,\d€]+?)(?:[,\d€]|$)', title, re.IGNORECASE)
        if simple_match:
            municipality = simple_match.group(1).strip()
            municipality = re.sub(r'\s+', ' ', municipality)
            if self._is_valid_municipality(municipality):
                logger.debug(f"Extracted municipality from title fallback: '{municipality}'")
                return municipality.title()
        
        logger.debug("No municipality found in title")
        return None
    
    def _is_valid_municipality(self, municipality: str) -> bool:
        """Validate if a municipality name is legitimate"""
        if not municipality or len(municipality) <= 2:
            return False
        
        # Reject if contains digits
        if re.search(r'\d', municipality):
            return False
        
        # Define stopwords (common Spanish/English words that aren't locations)
        stopwords = {'and', 'en', 'de', 'del', 'la', 'el', 'por', 'con', 'y', 'e', 'with', 'for', 'in', 'of', 'the', 'your', 'search'}
        
        # Check if first word is a stopword
        first_word = municipality.split()[0].lower()
        if first_word in stopwords:
            return False
        
        # Require either:
        # a) Contains a comma (e.g., 'Corias, Pravia')
        # b) Ends with known region
        # c) Contains at least two meaningful tokens
        if (',' in municipality or 
            re.search(r'\b(?:Asturias|Cantabria|Spain)\b', municipality, re.IGNORECASE) or
            len(municipality.split()) >= 2):
            return True
        
        # Single word must be a proper location name (capitalized, reasonable length)
        if (municipality.istitle() and 
            3 <= len(municipality) <= 30 and
            municipality.isalpha()):
            return True
        
        return False
    
    def _geocode_with_accuracy(self, land) -> Optional[Dict]:
        """Geocode a land with accuracy determination"""
        if not land.municipality:
            # Try to re-extract municipality from title if missing
            municipality = self._extract_municipality_from_title(land.title)
            if municipality:
                logger.info(f"Re-extracted municipality from title for land {land.id}: '{municipality}'")
                land.municipality = municipality
                # Commit immediately so we have the municipality for geocoding
                from app import db
                db.session.commit()
            else:
                logger.warning(f"No municipality found in title for land {land.id}: '{land.title}'")
                return None
            
        # Clean and validate municipality data first
        municipality = self._clean_municipality(land.municipality)
        if not municipality:
            logger.warning(f"Invalid municipality data for land {land.id}: '{land.municipality}'")
            return None

        # Normalize address format: replace " - " with ", " for better geocoding
        municipality = municipality.replace(" - ", ", ")

        # Try different address formats in order of precision (title-derived first)
        address_attempts: List[Dict[str, str]] = []

        title_parts = self._extract_location_parts_from_title(land.title or "")
        if title_parts:
            full = ", ".join(title_parts)
            address_attempts.append({"address": f"{full}, Asturias, Spain", "accuracy": "precise" if len(title_parts) >= 2 else "approximate"})
            if len(title_parts) >= 2:
                tail = ", ".join(title_parts[-2:])
                address_attempts.append({"address": f"{tail}, Asturias, Spain", "accuracy": "approximate"})
            if len(title_parts) >= 3:
                tail3 = ", ".join(title_parts[-3:])
                address_attempts.append({"address": f"{tail3}, Asturias, Spain", "accuracy": "precise"})
        
        # Try most specific first if we have detailed municipality info
        if municipality and ', ' in municipality:
            # For addresses like "Caserio Cuesta Ayones, 22, San Claudio-Trubia-Las Caldas, Oviedo"
            address_attempts.append({
                'address': f"{municipality}, Spain",
                'accuracy': 'precise'
            })
        elif municipality and any(keyword in municipality.lower() for keyword in ['calle', 'carretera', 'lugar', 'avenida', 'plaza']):
            # For addresses with street indicators
            address_attempts.append({
                'address': f"{municipality}, Spain", 
                'accuracy': 'precise'
            })
        
        # Always try the municipality as-is (if not too generic)
        if municipality and not self._is_too_generic(municipality):
            address_attempts.append({
                'address': f"{municipality}, Spain",
                'accuracy': 'approximate'
            })
        
        # Try more specific regional fallbacks instead of just "Cantabria, Spain"
        regional_fallbacks = self._get_regional_fallbacks(municipality)
        for fallback in regional_fallbacks:
            address_attempts.append({
                'address': fallback,
                'accuracy': 'regional'
            })
        
        for attempt in address_attempts:
            coordinates = self.geocoding_service.geocode_address(attempt['address'])
            if coordinates:
                # Only check for duplicates on precise geocoding results
                # Allow approximate/regional results even if they're duplicates
                if attempt['accuracy'] == 'precise':
                    if not self._is_duplicate_coordinates(coordinates['lat'], coordinates['lng'], land.id):
                        logger.info(f"Successfully geocoded '{attempt['address']}' with {attempt['accuracy']} accuracy")
                        return {
                            'lat': coordinates['lat'],
                            'lng': coordinates['lng'],
                            'accuracy': attempt['accuracy']
                        }
                    else:
                        logger.warning(f"Skipping duplicate precise coordinates for '{attempt['address']}'")
                        continue
                else:
                    # Accept approximate/regional results even if duplicated
                    logger.info(f"Successfully geocoded '{attempt['address']}' with {attempt['accuracy']} accuracy (allowing duplicates)")
                    return {
                        'lat': coordinates['lat'],
                        'lng': coordinates['lng'],
                        'accuracy': attempt['accuracy']
                    }
        
        return None
    
    def _clean_municipality(self, municipality: str) -> Optional[str]:
        """Clean and validate municipality data"""
        if not municipality or not isinstance(municipality, str):
            return None
            
        # Remove common bad values
        municipality = municipality.strip()
        bad_values = ['and', 'n/a', 'na', 'null', 'none', '']
        if municipality.lower() in bad_values:
            return None
            
        # Clean up common parsing artifacts
        municipality = municipality.replace('"', '').strip()
        
        return municipality if len(municipality) > 2 else None
    
    def _is_too_generic(self, municipality: str) -> bool:
        """Check if municipality is too generic to geocode uniquely"""
        generic_terms = ['cantabria', 'asturias', 'spain', 'españa']
        return municipality.lower().strip() in generic_terms
    
    def _get_regional_fallbacks(self, municipality: str) -> List[str]:
        """Get more specific regional fallbacks instead of just 'Cantabria, Spain'"""
        fallbacks = []
        
        # Try to extract more specific location info
        if municipality:
            # Look for known cities/towns in the municipality string
            known_locations = {
                'oviedo': 'Oviedo, Asturias, Spain',
                'gijon': 'Gijón, Asturias, Spain', 
                'santander': 'Santander, Cantabria, Spain',
                'cudillero': 'Cudillero, Asturias, Spain',
                'ribadedeva': 'Ribadedeva, Asturias, Spain',
                'siero': 'Siero, Asturias, Spain',
                'piloña': 'Piloña, Asturias, Spain',
                'llanes': 'Llanes, Asturias, Spain',
                'comillas': 'Comillas, Cantabria, Spain'
            }
            
            municipality_lower = municipality.lower()
            for location, full_address in known_locations.items():
                if location in municipality_lower:
                    fallbacks.append(full_address)
                    break
        
        # Default regional fallbacks - more specific than just "Cantabria, Spain"
        if not fallbacks:
            fallbacks.extend([
                'Asturias, Spain',  # Try Asturias first as many properties seem to be there
                'Cantabria, Spain'  # Final fallback
            ])
        
        return fallbacks
    
    def _is_duplicate_coordinates(self, lat: float, lng: float, current_land_id: int) -> bool:
        """Check if these coordinates already exist for another property"""
        try:
            from models import Land
            
            # Check if these exact coordinates exist for other properties
            existing = Land.query.filter(
                Land.id != current_land_id,
                Land.location_lat == lat,
                Land.location_lon == lng
            ).first()
            
            return existing is not None
        except Exception as e:
            logger.warning(f"Could not check for duplicate coordinates: {e}")
            return False
    
    def _enrich_with_google_places(self, land):
        """Enrich with Google Places API data"""
        try:
            lat, lon = float(land.location_lat), float(land.location_lon)

            cached = get_cached_enrichment_data(lat, lon, "google_places_v1")
            if isinstance(cached, dict):
                infrastructure_extended = land.infrastructure_extended or {}
                transport = land.transport or {}
                services_quality = land.services_quality or {}

                infrastructure_extended.update(cached.get("infrastructure_extended", {}) or {})
                transport.update(cached.get("transport", {}) or {})
                services_quality.update(cached.get("services_quality", {}) or {})

                land.infrastructure_extended = infrastructure_extended
                land.transport = transport
                land.services_quality = services_quality
                logger.debug("Google Places cache hit for land %s", land.id)
                return

            if not self.google_places_key:
                logger.warning("Google Places API key not available")
                return
            
            # Search for nearby amenities
            amenities = {
                'supermarket': ['supermarket', 'grocery_or_supermarket'],
                'school': ['school', 'primary_school', 'secondary_school'],
                'hospital': ['hospital', 'doctor'],
                'restaurant': ['restaurant'],
                'cafe': ['cafe'],
                'train_station': ['train_station', 'subway_station'],
                'bus_station': ['bus_station'],
                'airport': ['airport']
            }
            
            infrastructure_extended = land.infrastructure_extended or {}
            transport = land.transport or {}
            services_quality = land.services_quality or {}
            
            for amenity, place_types in amenities.items():
                nearby_places = self._search_nearby_places(lat, lon, place_types)
                
                if amenity in ['supermarket', 'school', 'hospital', 'restaurant', 'cafe']:
                    # Calculate distance to nearest and average rating
                    if nearby_places:
                        infrastructure_extended[f'{amenity}_available'] = True
                        nearest = min(nearby_places, key=lambda x: x.get('distance', float('inf')))
                        distance_m = nearest.get('distance')
                        if distance_m and distance_m > 0:
                            infrastructure_extended[f'{amenity}_distance'] = distance_m
                            # Calculate estimated travel time (assuming 40 km/h average speed in city)
                            travel_time_min = max(1, round((distance_m / 1000) * 60 / 40))
                            infrastructure_extended[f'{amenity}_travel_time'] = travel_time_min
                        
                        # Get average rating for services
                        if amenity in ['school', 'restaurant', 'cafe']:
                            ratings = [p.get('rating', 0) for p in nearby_places if p.get('rating')]
                            if ratings:
                                services_quality[f'{amenity}_avg_rating'] = sum(ratings) / len(ratings)
                    else:
                        infrastructure_extended.setdefault(f'{amenity}_available', False)
                
                elif amenity in ['train_station', 'bus_station', 'airport']:
                    # Calculate transport accessibility
                    places_for_transport = nearby_places
                    if not places_for_transport and amenity == 'airport':
                        # For airports, check if there's one within 100km radius
                        places_for_transport = self._search_nearby_places(lat, lon, place_types, radius=100000)

                    if places_for_transport:
                        transport[f'{amenity}_available'] = True
                        nearest = min(places_for_transport, key=lambda x: x.get('distance', float('inf')))
                        distance_m = nearest.get('distance')
                        if distance_m and distance_m > 0:
                            transport[f'{amenity}_distance'] = distance_m
                            # Use higher speed for transport hubs (50 km/h average, 80 for long distance)
                            avg_speed = 80 if amenity == 'airport' and distance_m > 5000 else 50
                            travel_time_min = max(1, round((distance_m / 1000) * 60 / avg_speed))
                            transport[f'{amenity}_travel_time'] = travel_time_min
                    else:
                        transport.setdefault(f'{amenity}_available', False)
            
            land.infrastructure_extended = infrastructure_extended
            land.transport = transport
            land.services_quality = services_quality

            cache_enrichment_data(
                lat,
                lon,
                "google_places_v1",
                {
                    "infrastructure_extended": infrastructure_extended,
                    "transport": transport,
                    "services_quality": services_quality,
                },
                timeout=60 * 60 * 24 * 7,  # 7 days
            )
            
        except Exception as e:
            logger.error(f"Failed to enrich with Google Places: {str(e)}")
            # Create fallback enrichment data when Google APIs fail
            self._create_fallback_amenities_data(land)
    
    def _search_nearby_places(self, lat: float, lon: float, place_types: List[str], radius: int = 5000) -> List[Dict]:
        """Search for nearby places using Google Places API"""
        try:
            places = []
            
            for place_type in place_types:
                url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                params = {
                    'location': f"{lat},{lon}",
                    'radius': radius,
                    'type': place_type,
                    'key': self.google_places_key
                }
                
                response = requests.get(url, params=params, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    for place in data.get('results', []):
                        place_info = {
                            'name': place.get('name'),
                            'rating': place.get('rating'),
                            'place_id': place.get('place_id'),
                            'types': place.get('types', []),
                            'location': place.get('geometry', {}).get('location', {}),
                            'distance': self._calculate_distance(
                                lat, lon,
                                place.get('geometry', {}).get('location', {}).get('lat', 0),
                                place.get('geometry', {}).get('location', {}).get('lng', 0)
                            )
                        }
                        places.append(place_info)
                
                # Rate limiting
                time.sleep(0.1)
            
            return places
            
        except Exception as e:
            logger.error(f"Failed to search nearby places: {str(e)}")
            return []
    
    def _create_fallback_amenities_data(self, land):
        """Create realistic fallback amenity data when Google APIs are not available"""
        try:
            if not land.location_lat or not land.location_lon:
                return
            
            infrastructure_extended = land.infrastructure_extended or {}
            
            # Get municipality info for realistic estimates
            municipality = (land.municipality or '').lower()
            
            # Determine area type (urban/rural) for realistic distances
            is_urban = any(city in municipality for city in ['oviedo', 'gijón', 'gijon', 'santander', 'avilés', 'aviles'])
            is_coastal = 'cudillero' in municipality or any(coastal in municipality for coastal in ['llanes', 'ribadesella', 'comillas', 'castro urdiales'])
            
            # Create realistic fallback data based on location type
            if is_urban:
                # Urban areas - closer amenities
                infrastructure_extended.update({
                    'supermarket_distance': 800,  # 800m
                    'supermarket_travel_time': 3,  # 3 minutes
                    'school_distance': 600,
                    'school_travel_time': 2,
                    'hospital_distance': 2000,
                    'hospital_travel_time': 5,
                    'restaurant_distance': 400,
                    'restaurant_travel_time': 2,
                    'cafe_distance': 300,
                    'cafe_travel_time': 1
                })
            elif is_coastal:
                # Coastal towns - moderate distances
                infrastructure_extended.update({
                    'supermarket_distance': 1500,  # 1.5km
                    'supermarket_travel_time': 5,
                    'school_distance': 1200,
                    'school_travel_time': 4,
                    'hospital_distance': 8000,  # May need to go to larger town
                    'hospital_travel_time': 12,
                    'restaurant_distance': 800,
                    'restaurant_travel_time': 3,
                    'cafe_distance': 600,
                    'cafe_travel_time': 2
                })
            else:
                # Rural areas - longer distances
                infrastructure_extended.update({
                    'supermarket_distance': 5000,  # 5km
                    'supermarket_travel_time': 10,
                    'school_distance': 3000,
                    'school_travel_time': 8,
                    'hospital_distance': 15000,  # 15km to nearest hospital
                    'hospital_travel_time': 20,
                    'restaurant_distance': 2000,
                    'restaurant_travel_time': 6,
                    'cafe_distance': 4000,
                    'cafe_travel_time': 8
                })
            
            land.infrastructure_extended = infrastructure_extended
            logger.info(f"Created fallback amenities data for land {land.id} ({'urban' if is_urban else 'coastal' if is_coastal else 'rural'} area)")
            
        except Exception as e:
            logger.error(f"Failed to create fallback amenities data: {str(e)}")
    
    def _enrich_with_google_maps(self, land):
        """Enrich with Google Maps data (distances, travel times)"""
        try:
            if not self.google_maps_key:
                logger.warning("Google Maps API key not available")
                return
            
            lat, lon = float(land.location_lat), float(land.location_lon)
            
            # Get distance matrix to major cities/destinations (single batch call)
            destinations = [
                "Madrid, Spain",
                "Barcelona, Spain",
                "Valencia, Spain",
                f"{land.municipality} city center, Spain"
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
                return

            distance_results = self._get_distance_matrix_batch(lat, lon, destinations)
            for destination, distance_data in zip(destinations, distance_results):
                if not distance_data:
                    continue
                dest_key = destination.split(',')[0].lower().replace(' ', '_')
                transport[f'distance_to_{dest_key}'] = distance_data.get('distance')
                transport[f'duration_to_{dest_key}'] = distance_data.get('duration')

            land.transport = transport
            cache_enrichment_data(lat, lon, cache_type, transport, timeout=60 * 60 * 24 * 7)
            
        except Exception as e:
            logger.error(f"Failed to enrich with Google Maps: {str(e)}")

    def _get_distance_matrix_batch(self, lat: float, lon: float, destinations: List[str]) -> List[Optional[Dict]]:
        """Batch distance matrix lookup (destinations <= 25)."""
        if not destinations:
            return []

        try:
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                "origins": f"{lat},{lon}",
                "destinations": "|".join(destinations),
                "mode": "driving",
                "key": self.google_maps_key,
            }

            response = requests.get(url, params=params, timeout=15)
            if response.status_code != 200:
                return [None for _ in destinations]

            data = response.json()
            if not data.get("rows") or not data["rows"][0].get("elements"):
                return [None for _ in destinations]

            elements = data["rows"][0]["elements"]
            out: List[Optional[Dict]] = []
            for el in elements[: len(destinations)]:
                if el.get("status") != "OK":
                    out.append(None)
                    continue
                out.append(
                    {
                        "distance": el.get("distance", {}).get("value"),
                        "duration": el.get("duration", {}).get("value"),
                    }
                )

            while len(out) < len(destinations):
                out.append(None)

            return out

        except Exception as e:
            logger.error("Failed to get distance matrix batch: %s", e)
            return [None for _ in destinations]
    
    def _get_distance_matrix(self, lat: float, lon: float, destination: str) -> Optional[Dict]:
        """Get distance and duration to destination using Google Maps Distance Matrix API"""
        try:
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                'origins': f"{lat},{lon}",
                'destinations': destination,
                'mode': 'driving',
                'key': self.google_maps_key
            }
            
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                if data.get('rows') and data['rows'][0].get('elements'):
                    element = data['rows'][0]['elements'][0]
                    if element.get('status') == 'OK':
                        return {
                            'distance': element.get('distance', {}).get('value'),  # in meters
                            'duration': element.get('duration', {}).get('value')   # in seconds
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get distance matrix: {str(e)}")
            return None
    
    def _enrich_with_osm_data(self, land):
        """Enrich with OpenStreetMap data as fallback"""
        try:
            lat, lon = float(land.location_lat), float(land.location_lon)

            cached = get_cached_enrichment_data(lat, lon, "osm_amenities_v1")
            if isinstance(cached, dict):
                infrastructure_extended = land.infrastructure_extended or {}
                infrastructure_extended['osm_amenities'] = cached
                land.infrastructure_extended = infrastructure_extended
                logger.debug("OSM amenities cache hit for land %s", land.id)
                return
            
            # OSM Overpass query for nearby amenities
            overpass_query = f"""
            [out:json][timeout:25];
            (
              node["amenity"~"^(supermarket|school|hospital|restaurant|cafe|fuel)$"](around:2000,{lat},{lon});
              way["amenity"~"^(supermarket|school|hospital|restaurant|cafe|fuel)$"](around:2000,{lat},{lon});
              relation["amenity"~"^(supermarket|school|hospital|restaurant|cafe|fuel)$"](around:2000,{lat},{lon});
            );
            out center;
            """
            
            response = requests.post(
                self.osm_overpass_url,
                data=overpass_query,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30,
            )
            
            if response.status_code == 200:
                osm_data = response.json()
                infrastructure_extended = land.infrastructure_extended or {}
                
                # Process OSM amenities as fallback data
                amenity_counts = {}
                for element in osm_data.get('elements', []):
                    amenity = element.get('tags', {}).get('amenity')
                    if amenity:
                        amenity_counts[amenity] = amenity_counts.get(amenity, 0) + 1
                
                # Store OSM fallback data
                infrastructure_extended['osm_amenities'] = amenity_counts
                land.infrastructure_extended = infrastructure_extended

                cache_enrichment_data(lat, lon, "osm_amenities_v1", amenity_counts, timeout=60 * 60 * 24 * 7)
            
        except Exception as e:
            logger.error(f"Failed to enrich with OSM data: {str(e)}")
    
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
                'vista al mar', 'vistas al mar', 'vista mar', 'vistas mar',
                'sea view', 'sea views', 'ocean view', 'ocean views',
                'frente al mar', 'primera linea', 'primera línea',
                'beach front', 'beachfront', 'waterfront',
                'junto al mar', 'cerca del mar', 'a pie de playa'
            ]
            environment['sea_view'] = any(keyword in text_for_views for keyword in sea_keywords)

            # Mountain view detection - only from explicit mentions
            mountain_keywords = [
                'vista montaña', 'vistas montaña', 'vista a la montaña', 'vistas a la montaña',
                'mountain view', 'mountain views',
                'vista sierra', 'vistas sierra',
                'picos de europa', 'cordillera cantábrica', 'cordillera cantabrica'
            ]
            environment['mountain_view'] = any(keyword in text_for_views for keyword in mountain_keywords)

            # Forest/nature view detection
            forest_keywords = [
                'vista bosque', 'bosque', 'rodeado de bosque', 'rodeada de bosque',
                'rodeado de naturaleza', 'rodeada de naturaleza', 'entorno natural',
                'zona arbolada', 'arbolado', 'mucho verde',
                'forest view', 'woodland', 'surrounded by nature'
            ]
            environment['forest_view'] = any(keyword in text_for_views for keyword in forest_keywords)

            # Orientation detection
            orientation_keywords = {
                'norte': 'north', 'sur': 'south', 'este': 'east', 'oeste': 'west',
                'noreste': 'northeast', 'noroeste': 'northwest',
                'sureste': 'southeast', 'suroeste': 'southwest'
            }

            for spanish_orientation, english_orientation in orientation_keywords.items():
                if spanish_orientation in description:
                    environment['orientation'] = english_orientation
                    break

            land.environment = environment

        except Exception as e:
            logger.error(f"Failed to analyze environment: {str(e)}")
    
    def _is_coastal_location(self, land):
        """Check if location is in a known coastal area based on coordinates"""
        try:
            if not land.location_lat or not land.location_lon:
                return False
            
            lat, lon = float(land.location_lat), float(land.location_lon)
            
            # Define coastal regions of northern Spain (Asturias, Cantabria)
            # Expanded coordinates to cover more coastal areas
            coastal_regions = [
                # Asturias coast (expanded)
                {'min_lat': 43.1, 'max_lat': 43.8, 'min_lon': -7.5, 'max_lon': -4.0},
                # Cantabria coast (expanded)
                {'min_lat': 43.0, 'max_lat': 43.7, 'min_lon': -5.0, 'max_lon': -3.0},
            ]
            
            for region in coastal_regions:
                if (region['min_lat'] <= lat <= region['max_lat'] and 
                    region['min_lon'] <= lon <= region['max_lon']):
                    return True
            
            return False
        except Exception:
            return False
    
    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in meters using Haversine formula"""
        import math
        
        R = 6371000  # Earth's radius in meters
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat/2) * math.sin(delta_lat/2) +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon/2) * math.sin(delta_lon/2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return R * c
    
