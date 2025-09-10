import os
import logging
import requests
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

class TravelTimeService:
    def __init__(self):
        # Use existing secret names with fallback to standard names
        self.google_maps_key = os.environ.get("Google_api") or os.environ.get("GOOGLE_MAPS_API") or os.environ.get("GOOGLE_MAPS_API_KEY")
        
        # Key destinations
        self.destinations = {
            'oviedo': 'Oviedo, Asturias, Spain',
            'gijon': 'Gijón, Asturias, Spain'
        }
        
        # Popular beaches in Asturias and Cantabria
        self.beaches = [
            'Playa de San Lorenzo, Gijón, Spain',
            'Playa de Rodiles, Villaviciosa, Spain',
            'Playa de Gulpiyuri, Llanes, Spain',
            'Playa del Sardinero, Santander, Spain',
            'Playa de Comillas, Cantabria, Spain',
            'Playa de Oyambre, Comillas, Spain',
            'Playa de la Concha de Artedo, Cudillero, Spain',
            'Playa de Ribadesella, Asturias, Spain'
        ]
        
        # Key infrastructure locations
        self.airports = [
            'Santander Airport, Santander, Spain',
            'Asturias Airport, Santiago del Monte, Spain',
            'Bilbao Airport, Loiu, Spain'
        ]
        
        self.train_stations = [
            'Santander Railway Station, Santander, Spain',
            'Oviedo Railway Station, Oviedo, Spain',
            'Gijón Railway Station, Gijón, Spain'
        ]
        
        self.hospitals = [
            'Hospital Universitario Marqués de Valdecilla, Santander, Spain',
            'Hospital Universitario Central de Asturias, Oviedo, Spain',
            'Hospital Cabueñes, Gijón, Spain'
        ]
        
        self.police_stations = [
            'Policía Nacional Santander, Spain',
            'Policía Nacional Oviedo, Spain', 
            'Policía Nacional Gijón, Spain'
        ]
    
    def calculate_travel_times(self, land_id: int) -> bool:
        """Calculate travel times for a land property"""
        try:
            from models import Land
            from app import db
            
            land = Land.query.get(land_id)
            if not land or not land.location_lat or not land.location_lon:
                logger.warning(f"Land {land_id} has no coordinates")
                return False
            
            logger.info(f"Calculating travel times for land {land_id}")
            
            origin = f"{land.location_lat},{land.location_lon}"
            
            # Calculate times to Oviedo and Gijón
            oviedo_time = self._get_travel_time(origin, self.destinations['oviedo'])
            gijon_time = self._get_travel_time(origin, self.destinations['gijon'])
            
            # Find nearest beach
            nearest_beach_data = self._find_nearest_beach(origin)
            
            # Calculate times and distances to key infrastructure (priority locations)
            airport_data = self._find_nearest_facility_with_distance(origin, self.airports)
            train_station_data = self._find_nearest_facility_with_distance(origin, self.train_stations)
            hospital_data = self._find_nearest_facility_with_distance(origin, self.hospitals)
            police_data = self._find_nearest_facility_with_distance(origin, self.police_stations)
            
            # Update land record
            if oviedo_time is not None:
                land.travel_time_oviedo = oviedo_time
            if gijon_time is not None:
                land.travel_time_gijon = gijon_time
            if nearest_beach_data:
                land.travel_time_nearest_beach = nearest_beach_data['time']
                land.nearest_beach_name = nearest_beach_data['name']
                
            # Update priority infrastructure travel times and distances
            if airport_data is not None:
                land.travel_time_airport = airport_data['time']
                land.distance_airport = airport_data['distance']
            if train_station_data is not None:
                land.travel_time_train_station = train_station_data['time']
                land.distance_train_station = train_station_data['distance']
            if hospital_data is not None:
                land.travel_time_hospital = hospital_data['time']
                land.distance_hospital = hospital_data['distance']
            if police_data is not None:
                land.travel_time_police = police_data['time']
                land.distance_police = police_data['distance']
            
            db.session.commit()
            
            logger.info(f"Travel times updated for land {land_id}: "
                       f"Oviedo: {oviedo_time}min, Gijón: {gijon_time}min, "
                       f"Beach: {nearest_beach_data['time'] if nearest_beach_data else 'N/A'}min")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to calculate travel times for land {land_id}: {str(e)}")
            return False
    
    def _get_travel_time(self, origin: str, destination: str) -> Optional[int]:
        """Get travel time in minutes between origin and destination"""
        result = self._get_travel_time_and_distance(origin, destination)
        return result['time'] if result else None
    
    def _get_travel_time_and_distance(self, origin: str, destination: str) -> Optional[Dict]:
        """Get travel time in minutes and distance in km between origin and destination"""
        # Try Google API first if available
        if self.google_maps_key:
            result = self._get_google_travel_time(origin, destination)
            if result:
                return result
        
        # Fallback to mathematical estimation
        logger.info("Using fallback travel time calculation")
        return self._calculate_fallback_travel_time(origin, destination)
        
        return self._calculate_fallback_travel_time(origin, destination)
    
    def _get_google_travel_time(self, origin: str, destination: str) -> Optional[Dict]:
        """Get travel time using Google Maps API"""
        try:
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                'origins': origin,
                'destinations': destination,
                'mode': 'driving',
                'units': 'metric',
                'key': self.google_maps_key
            }
            
            response = requests.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                
                if data.get('status') == 'OK' and data.get('rows'):
                    elements = data['rows'][0].get('elements', [])
                    if elements and elements[0].get('status') == 'OK':
                        duration = elements[0]['duration']['value']  # seconds
                        distance = elements[0]['distance']['value']  # meters
                        
                        return {
                            'time': round(duration / 60),  # convert to minutes
                            'distance': round(distance / 1000)  # convert to kilometers
                        }
            
            logger.warning(f"Google API failed for {origin} to {destination}: {data.get('status') if 'data' in locals() else 'No response'}")
            return None
            
        except Exception as e:
            logger.error(f"Google Maps API error: {str(e)}")
            return None
    
    def _calculate_fallback_travel_time(self, origin: str, destination: str) -> Optional[Dict]:
        """Calculate travel time using mathematical distance estimation"""
        try:
            # Parse origin coordinates
            if ',' in origin:
                origin_lat, origin_lon = map(float, origin.split(','))
            else:
                logger.error(f"Invalid origin format: {origin}")
                return None
            
            # Get destination coordinates
            dest_coords = self._get_destination_coordinates(destination)
            if not dest_coords:
                logger.warning(f"Could not get coordinates for destination: {destination}")
                return None
            
            dest_lat, dest_lon = dest_coords
            
            # Calculate straight-line distance using Haversine formula
            distance_km = self._haversine_distance(origin_lat, origin_lon, dest_lat, dest_lon)
            
            # Estimate travel time based on distance and road type
            # Use realistic speed estimates: 
            # - Short distances (<20km): 45 km/h average (local roads, traffic)
            # - Medium distances (20-50km): 55 km/h average (mixed roads)
            # - Long distances (>50km): 65 km/h average (highways)
            
            if distance_km < 20:
                avg_speed = 45
            elif distance_km < 50:
                avg_speed = 55
            else:
                avg_speed = 65
            
            # Add 20% to account for actual road routes vs straight line
            actual_distance = distance_km * 1.2
            travel_time = round((actual_distance / avg_speed) * 60)  # Convert to minutes
            
            return {
                'time': travel_time,
                'distance': round(actual_distance)
            }
            
        except Exception as e:
            logger.error(f"Fallback travel time calculation failed: {str(e)}")
            return None
    
    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate the great circle distance between two points on earth (in kilometers)"""
        import math
        
        # Convert decimal degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        
        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        # Radius of earth in kilometers
        r = 6371
        
        return c * r
    
    def _get_destination_coordinates(self, destination: str) -> Optional[tuple]:
        """Get coordinates for common destinations"""
        # Predefined coordinates for major destinations
        coords_map = {
            'Oviedo, Asturias, Spain': (43.3614, -5.8593),
            'Gijón, Asturias, Spain': (43.5322, -5.6611),
            'Santander, Cantabria, Spain': (43.4623, -3.8099),
            
            # Beaches
            'Playa de San Lorenzo, Gijón, Spain': (43.5390, -5.6531),
            'Playa de Rodiles, Villaviciosa, Spain': (43.4844, -5.3869),
            'Playa de Gulpiyuri, Llanes, Spain': (43.4222, -4.7558),
            'Playa del Sardinero, Santander, Spain': (43.4816, -3.7886),
            'Playa de Comillas, Cantabria, Spain': (43.3878, -4.2894),
            'Playa de Oyambre, Comillas, Spain': (43.3756, -4.2736),
            'Playa de la Concha de Artedo, Cudillero, Spain': (43.5667, -6.1500),
            'Playa de Ribadesella, Asturias, Spain': (43.4628, -5.0589),
            
            # Airports
            'Santander Airport, Santander, Spain': (43.4270, -3.8201),
            'Asturias Airport, Santiago del Monte, Spain': (43.5637, -6.0346),
            'Bilbao Airport, Loiu, Spain': (43.3011, -2.9106),
            
            # Train stations
            'Santander Railway Station, Santander, Spain': (43.4616, -3.8048),
            'Oviedo Railway Station, Oviedo, Spain': (43.3656, -5.8515),
            'Gijón Railway Station, Gijón, Spain': (43.5406, -5.6606),
            
            # Hospitals
            'Hospital Universitario Marqués de Valdecilla, Santander, Spain': (43.4559, -3.8049),
            'Hospital Universitario Central de Asturias, Oviedo, Spain': (43.3378, -5.8515),
            'Hospital Cabueñes, Gijón, Spain': (43.5211, -5.6069),
            
            # Police stations (approximate city center locations)
            'Policía Nacional Santander, Spain': (43.4623, -3.8099),
            'Policía Nacional Oviedo, Spain': (43.3614, -5.8593),
            'Policía Nacional Gijón, Spain': (43.5322, -5.6611)
        }
        
        return coords_map.get(destination)
    
    def _find_nearest_beach(self, origin: str) -> Optional[Dict]:
        """Find nearest beach and travel time"""
        try:
            # Calculate times to all beaches using available method
            beach_times = []
            
            for beach in self.beaches:
                travel_data = self._get_travel_time_and_distance(origin, beach)
                if travel_data:
                    beach_name = beach.split(',')[0].replace('Playa de ', '').replace('Playa del ', '')
                    beach_times.append({
                        'name': beach_name,
                        'time': travel_data['time'],
                        'distance': travel_data['distance'],
                        'full_name': beach
                    })
            
            if beach_times:
                # Return nearest beach by time
                nearest = min(beach_times, key=lambda x: x['time'])
                logger.info(f"Nearest beach: {nearest['name']} ({nearest['time']} minutes, {nearest['distance']} km)")
                return nearest
            
            return None
            
        except Exception as e:
            logger.error(f"Error finding nearest beach: {str(e)}")
            return None
    
    def _find_nearest_facility(self, origin: str, facilities: List[str]) -> Optional[int]:
        """Find travel time to nearest facility from a list (legacy for backward compatibility)"""
        result = self._find_nearest_facility_with_distance(origin, facilities)
        return result['time'] if result else None
    
    def _find_nearest_facility_with_distance(self, origin: str, facilities: List[str]) -> Optional[Dict]:
        """Find travel time and distance to nearest facility from a list"""
        if not facilities:
            return None
        
        try:
            # Calculate times and distances to all facilities using available method
            facility_data = []
            
            for facility in facilities:
                result = self._get_travel_time_and_distance(origin, facility)
                if result is not None:
                    facility_data.append(result)
            
            if facility_data:
                # Return nearest facility data (by time)
                nearest = min(facility_data, key=lambda x: x['time'])
                logger.info(f"Nearest facility: {nearest['time']} min, {nearest['distance']} km")
                return nearest
            
            return None
            
        except Exception as e:
            logger.error(f"Error finding nearest facility: {str(e)}")
            return None
    
    def generate_google_maps_route_url(self, origin_lat: float, origin_lon: float, 
                                      destination: str) -> str:
        """Generate Google Maps URL for route"""
        origin = f"{origin_lat},{origin_lon}"
        
        if destination == 'oviedo':
            dest = self.destinations['oviedo']
        elif destination == 'gijon':
            dest = self.destinations['gijon']  
        else:
            dest = destination
        
        return f"https://www.google.com/maps/dir/{origin}/{dest}"