import os
import logging
import requests
import time
from typing import Dict, List, Optional
from utils.geocoding import GeocodingService

logger = logging.getLogger(__name__)

class EnrichmentService:
    def __init__(self):
        # Use existing secret names with fallback to standard names
        self.google_maps_key = os.environ.get("Google_api") or os.environ.get("GOOGLE_MAPS_API") or os.environ.get("GOOGLE_MAPS_API_KEY")
        self.google_places_key = os.environ.get("Google_api") or os.environ.get("GOOGLE_MAPS_API") or os.environ.get("GOOGLE_PLACES_API_KEY")
        self.osm_overpass_url = "https://overpass-api.de/api/interpreter"
        self.geocoding_service = GeocodingService()
        
    def enrich_land(self, land_id: int) -> bool:
        """Main method to enrich a land record with external data"""
        try:
            from models import Land
            from app import db
            
            land = Land.query.get(land_id)
            if not land:
                logger.error(f"Land with ID {land_id} not found")
                return False
            
            logger.info(f"Starting enrichment for land {land_id}: {land.title}")
            
            # Step 1: Geocode the location if coordinates are missing
            if not land.location_lat or not land.location_lon:
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
            from services.scoring_service import ScoringService
            scoring_service = ScoringService()
            scoring_service.calculate_score(land)
            
            db.session.commit()
            logger.info(f"Successfully enriched land {land_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to enrich land {land_id}: {str(e)}")
            return False
    
    def _geocode_with_accuracy(self, land) -> Optional[Dict]:
        """Geocode a land with accuracy determination"""
        if not land.municipality:
            return None
            
        # Try different address formats in order of precision
        address_attempts = []
        
        # Try most specific first if we have detailed municipality info
        if land.municipality and ', ' in land.municipality:
            # For addresses like "Caserio Cuesta Ayones, 22, San Claudio-Trubia-Las Caldas, Oviedo"
            address_attempts.append({
                'address': f"{land.municipality}, Spain",
                'accuracy': 'precise'
            })
        elif land.municipality and any(keyword in land.municipality.lower() for keyword in ['calle', 'carretera', 'lugar', 'avenida', 'plaza']):
            # For addresses with street indicators
            address_attempts.append({
                'address': f"{land.municipality}, Spain", 
                'accuracy': 'precise'
            })
        
        # Always try the municipality as-is
        if land.municipality and land.municipality.lower() not in ['and', 'cantabria']:
            address_attempts.append({
                'address': f"{land.municipality}, Spain",
                'accuracy': 'approximate'
            })
        
        # Final fallback to general region
        address_attempts.append({
            'address': "Cantabria, Spain",
            'accuracy': 'approximate'
        })
        
        for attempt in address_attempts:
            coordinates = self.geocoding_service.geocode_address(attempt['address'])
            if coordinates:
                logger.info(f"Successfully geocoded '{attempt['address']}' with {attempt['accuracy']} accuracy")
                return {
                    'lat': coordinates['lat'],
                    'lng': coordinates['lng'],
                    'accuracy': attempt['accuracy']
                }
        
        return None
    
    def _enrich_with_google_places(self, land):
        """Enrich with Google Places API data"""
        try:
            if not self.google_places_key:
                logger.warning("Google Places API key not available")
                return
            
            lat, lon = float(land.location_lat), float(land.location_lon)
            
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
                    # Note: No longer setting _available = false to avoid duplicates
                
                elif amenity in ['train_station', 'bus_station', 'airport']:
                    # Calculate transport accessibility
                    if nearby_places:
                        nearest = min(nearby_places, key=lambda x: x.get('distance', float('inf')))
                        distance_m = nearest.get('distance')
                        if distance_m and distance_m > 0:
                            transport[f'{amenity}_distance'] = distance_m
                            # Use higher speed for transport hubs (50 km/h average)
                            travel_time_min = max(1, round((distance_m / 1000) * 60 / 50))
                            transport[f'{amenity}_travel_time'] = travel_time_min
                    else:
                        # For airports, check if there's one within 100km radius
                        if amenity == 'airport':
                            wider_places = self._search_nearby_places(lat, lon, place_types, radius=100000)
                            if wider_places:
                                nearest = min(wider_places, key=lambda x: x.get('distance', float('inf')))
                                distance_m = nearest.get('distance')
                                if distance_m and distance_m > 0:
                                    transport[f'{amenity}_distance'] = distance_m
                                    # Highway speed for long distance (80 km/h average)
                                    travel_time_min = max(1, round((distance_m / 1000) * 60 / 80))
                                    transport[f'{amenity}_travel_time'] = travel_time_min
                    # Note: No longer setting _available fields to avoid duplicates
            
            land.infrastructure_extended = infrastructure_extended
            land.transport = transport
            land.services_quality = services_quality
            
        except Exception as e:
            logger.error(f"Failed to enrich with Google Places: {str(e)}")
    
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
                
                response = requests.get(url, params=params)
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
    
    def _enrich_with_google_maps(self, land):
        """Enrich with Google Maps data (distances, travel times)"""
        try:
            if not self.google_maps_key:
                logger.warning("Google Maps API key not available")
                return
            
            lat, lon = float(land.location_lat), float(land.location_lon)
            
            # Get distance matrix to major cities/destinations
            destinations = [
                "Madrid, Spain",
                "Barcelona, Spain",
                "Valencia, Spain",
                f"{land.municipality} city center, Spain"
            ]
            
            transport = land.transport or {}
            
            for destination in destinations:
                distance_data = self._get_distance_matrix(lat, lon, destination)
                if distance_data:
                    dest_key = destination.split(',')[0].lower().replace(' ', '_')
                    transport[f'distance_to_{dest_key}'] = distance_data.get('distance')
                    transport[f'duration_to_{dest_key}'] = distance_data.get('duration')
            
            land.transport = transport
            
        except Exception as e:
            logger.error(f"Failed to enrich with Google Maps: {str(e)}")
    
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
            
            response = requests.get(url, params=params)
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
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
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
            
        except Exception as e:
            logger.error(f"Failed to enrich with OSM data: {str(e)}")
    
    def _analyze_environment(self, land):
        """Analyze environment features like views and orientation"""
        try:
            environment = land.environment or {}
            
            # Analyze description for view keywords
            description = (land.description or "").lower()
            
            # Sea view detection
            sea_keywords = ['mar', 'playa', 'costa', 'litoral', 'vista al mar']
            environment['sea_view'] = any(keyword in description for keyword in sea_keywords)
            
            # Mountain view detection
            mountain_keywords = ['montaña', 'sierra', 'monte', 'vista montaña']
            environment['mountain_view'] = any(keyword in description for keyword in mountain_keywords)
            
            # Forest view detection
            forest_keywords = ['bosque', 'forestal', 'pinar', 'verde']
            environment['forest_view'] = any(keyword in description for keyword in forest_keywords)
            
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
