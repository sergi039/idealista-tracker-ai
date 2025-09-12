# New modular ScoringService - delegates to specialized components
# This replaces the monolithic implementation with a clean, maintainable architecture

from services.scoring.scoring_service import ScoringService as ModularScoringService
from typing import Dict

# Export the new modular scoring service as the main ScoringService
# This maintains backward compatibility with existing imports
class ScoringService(ModularScoringService):
    """
    Refactored ScoringService using modular architecture
    
    This class now delegates to specialized components:
    - WeightManager: Handles weight operations
    - ScoreCalculator: Calculates individual criterion scores  
    - ScoringConfigManager: Manages configuration and thresholds
    - LandDataExtractor: Extracts and normalizes land data
    """
    
    # Backward compatibility methods
    def _load_profile_weights(self, profile: str) -> Dict[str, float]:
        """Legacy compatibility method - delegates to WeightManager"""
        return self.weight_manager.load_profile_weights(profile)
    
    def _validate_profiles(self):
        """Legacy compatibility method - no-op in new architecture"""
        pass
    
    def load_custom_weights(self):
        """Legacy compatibility method - no-op in new architecture"""
        pass

# For backward compatibility, expose the same interface
__all__ = ['ScoringService']