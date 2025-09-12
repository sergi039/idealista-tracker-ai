import logging
from typing import Dict, Optional
from services.scoring.config_manager import ScoringConfigManager
from services.scoring.data_extractor import LandDataExtractor

logger = logging.getLogger(__name__)

class ScoreCalculator:
    """Calculates individual criterion scores using configurable rules"""
    
    def __init__(self):
        self.config_manager = ScoringConfigManager()
        self.data_extractor = LandDataExtractor()
    
    def calculate_individual_scores(self, land) -> Dict[str, Optional[float]]:
        """Calculate all individual criterion scores for a land"""
        scores = {}
        
        # Calculate each criterion score
        scoring_methods = {
            'investment_yield': self._score_investment_yield,
            'location_quality': self._score_location_quality,
            'transport': self._score_transport,
            'infrastructure_basic': self._score_infrastructure_basic,
            'infrastructure_extended': self._score_infrastructure_extended,
            'environment': self._score_environment,
            'physical_characteristics': self._score_physical_characteristics,
            'services_quality': self._score_services_quality,
            'legal_status': self._score_legal_status,
            'development_potential': self._score_development_potential
        }
        
        for criterion, method in scoring_methods.items():
            try:
                score = method(land)
                scores[criterion] = score
                
                if score is not None:
                    logger.debug(f"Calculated {criterion} score: {score:.1f} for land {land.id}")
            except Exception as e:
                logger.error(f"Failed to calculate {criterion} score for land {land.id}: {str(e)}")
                scores[criterion] = None
        
        return scores
    
    def _score_investment_yield(self, land) -> Optional[float]:
        """Score investment yield using configurable thresholds"""
        investment_data = self.data_extractor.extract_investment_data(land)
        rental_yield = investment_data.get('rental_yield')
        cap_rate = investment_data.get('cap_rate')
        
        if rental_yield is None and cap_rate is None:
            return None
        
        # Score rental yield
        yield_score = 0
        cap_score = 0
        
        if rental_yield is not None:
            yield_score = self.config_manager.get_investment_yield_score(rental_yield)
        
        if cap_rate is not None:
            cap_score = self.config_manager.get_investment_yield_score(cap_rate)
        
        # Combine scores with weights
        final_score = 0
        weight_sum = 0
        
        if rental_yield is not None:
            final_score += yield_score * 0.6
            weight_sum += 0.6
        
        if cap_rate is not None:
            final_score += cap_score * 0.4
            weight_sum += 0.4
        
        if weight_sum > 0:
            normalized_score = final_score / weight_sum
            return min(100, max(0, normalized_score))
        
        return None
    
    def _score_infrastructure_basic(self, land) -> Optional[float]:
        """Score basic infrastructure using data extractor"""
        infrastructure_data = self.data_extractor.extract_infrastructure_data(land)
        
        if not any(infrastructure_data.values()):
            return None
        
        # Count available utilities
        available_utilities = sum(1 for available in infrastructure_data.values() if available)
        total_utilities = len(infrastructure_data)
        
        score = (available_utilities / total_utilities) * 100
        
        logger.debug(f"Infrastructure basic score: {score:.1f} ({available_utilities}/{total_utilities} utilities)")
        return score
    
    def _score_transport(self, land) -> Optional[float]:
        """Score transport accessibility using configurable distance thresholds"""
        transport_data = self.data_extractor.extract_transport_data(land)
        
        if not transport_data:
            return None
        
        transport_weights = {
            'train_station': 30,
            'bus_station': 20,
            'airport': 25,
            'highway': 25
        }
        
        total_score = 0
        
        for transport_type, max_points in transport_weights.items():
            if transport_type in transport_data:
                transport_info = transport_data[transport_type]
                if transport_info['available']:
                    distance = transport_info['distance']
                    score = self.config_manager.get_distance_score(distance, max_points)
                    total_score += score
        
        return min(total_score, 100)  # Cap at 100
    
    def _score_environment(self, land) -> Optional[float]:
        """Score environment features"""
        env_data = self.data_extractor.extract_environment_data(land)
        
        if not env_data:
            return None
        
        score = 0
        
        # View bonuses
        if env_data.get('sea_view'):
            score += 40
        if env_data.get('mountain_view'):
            score += 30
        if env_data.get('forest_view'):
            score += 20
        
        # Orientation bonus (south-facing preferred in Spain)
        orientation = env_data.get('orientation', '')
        orientation_scores = {
            'south': 20,
            'southeast': 15,
            'southwest': 15,
            'east': 10,
            'west': 10,
            'north': 0
        }
        
        for direction, points in orientation_scores.items():
            if direction in orientation:
                score += points
                break
        
        return min(score, 100)  # Cap at 100
    
    # Add other scoring methods here following the same pattern...
    def _score_location_quality(self, land) -> Optional[float]:
        # Implementation using data_extractor and config_manager
        return 50  # Placeholder
    
    def _score_infrastructure_extended(self, land) -> Optional[float]:
        # Implementation
        return 50  # Placeholder
    
    def _score_physical_characteristics(self, land) -> Optional[float]:
        # Implementation
        return 50  # Placeholder
    
    def _score_services_quality(self, land) -> Optional[float]:
        # Implementation
        return 50  # Placeholder
    
    def _score_legal_status(self, land) -> Optional[float]:
        # Implementation
        return 50  # Placeholder
    
    def _score_development_potential(self, land) -> Optional[float]:
        # Implementation
        return 50  # Placeholder