import os
import logging
import requests
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

class TravelTimeService:
    def __init__(self):
        self.google_maps_key = os.environ.get("GOOGLE_MAPS_API", "") or os.environ.get("Google_api", "") or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        
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
            
            # Update land record
            if oviedo_time is not None:
                land.travel_time_oviedo = oviedo_time
            if gijon_time is not None:
                land.travel_time_gijon = gijon_time
            if nearest_beach_data:
                land.travel_time_nearest_beach = nearest_beach_data['time']
                land.nearest_beach_name = nearest_beach_data['name']
            
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
        if not self.google_maps_key:
            logger.warning("Google Maps API key not available for travel times")
            return None
        
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
                        return round(duration / 60)  # convert to minutes
            
            logger.warning(f"Failed to get travel time from {origin} to {destination}")
            return None
            
        except Exception as e:
            logger.error(f"Error getting travel time: {str(e)}")
            return None
    
    def _find_nearest_beach(self, origin: str) -> Optional[Dict]:
        """Find nearest beach and travel time"""
        if not self.google_maps_key:
            return None
        
        try:
            # Calculate times to all beaches
            beach_times = []
            
            for beach in self.beaches:
                travel_time = self._get_travel_time(origin, beach)
                if travel_time is not None:
                    beach_name = beach.split(',')[0].replace('Playa de ', '').replace('Playa del ', '')
                    beach_times.append({
                        'name': beach_name,
                        'time': travel_time,
                        'full_name': beach
                    })
            
            if beach_times:
                # Return nearest beach
                nearest = min(beach_times, key=lambda x: x['time'])
                logger.info(f"Nearest beach: {nearest['name']} ({nearest['time']} minutes)")
                return nearest
            
            return None
            
        except Exception as e:
            logger.error(f"Error finding nearest beach: {str(e)}")
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