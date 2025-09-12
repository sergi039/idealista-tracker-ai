import logging
from typing import Dict, Any, Optional
from services.scoring.config_manager import ScoringConfigManager

logger = logging.getLogger(__name__)

class LandDataExtractor:
    """Extracts and normalizes data from Land objects"""
    
    def __init__(self):
        self.config_manager = ScoringConfigManager()
    
    def extract_infrastructure_data(self, land) -> Dict[str, bool]:
        """Extract infrastructure data from JSONB and description"""
        result = {
            'electricity': False,
            'water': False,
            'internet': False,
            'gas': False
        }
        
        # First check JSONB data
        if land.infrastructure_basic:
            for utility in result.keys():
                if land.infrastructure_basic.get(utility):
                    result[utility] = True
        
        # Then check description for missing utilities
        description = (land.description or "").lower()
        
        for utility in result.keys():
            if not result[utility]:  # Only check if not already found
                keywords = self.config_manager.get_infrastructure_keywords(utility)
                if any(keyword in description for keyword in keywords):
                    result[utility] = True
                    logger.debug(f"Found {utility} in description for land {land.id}")
        
        return result
    
    def extract_transport_data(self, land) -> Dict[str, Any]:
        """Extract transport accessibility data"""
        if not land.transport:
            return {}
        
        return {
            'train_station': {
                'available': land.transport.get('train_station_available', False),
                'distance': land.transport.get('train_station_distance', float('inf'))
            },
            'bus_station': {
                'available': land.transport.get('bus_station_available', False),
                'distance': land.transport.get('bus_station_distance', float('inf'))
            },
            'airport': {
                'available': land.transport.get('airport_available', False),
                'distance': land.transport.get('airport_distance', float('inf'))
            },
            'highway': {
                'available': land.transport.get('highway_available', False),
                'distance': land.transport.get('highway_distance', float('inf'))
            }
        }
    
    def extract_environment_data(self, land) -> Dict[str, Any]:
        """Extract environment and view data"""
        if not land.environment:
            return {}
        
        return {
            'sea_view': land.environment.get('sea_view', False),
            'mountain_view': land.environment.get('mountain_view', False),
            'forest_view': land.environment.get('forest_view', False),
            'orientation': land.environment.get('orientation', '').lower()
        }
    
    def extract_investment_data(self, land) -> Dict[str, Optional[float]]:
        """Extract investment-related data"""
        try:
            # Try to get from market analysis service
            from services.market_analysis_service import MarketAnalysisService
            
            market_service = MarketAnalysisService()
            rental_analysis = market_service.calculate_rental_analysis(land)
            
            if rental_analysis:
                return {
                    'rental_yield': rental_analysis.get('rental_yield'),
                    'cap_rate': rental_analysis.get('cap_rate'),
                    'monthly_rent': rental_analysis.get('monthly_rent')
                }
        except Exception as e:
            logger.warning(f"Could not extract investment data: {str(e)}")
        
        return {'rental_yield': None, 'cap_rate': None, 'monthly_rent': None}