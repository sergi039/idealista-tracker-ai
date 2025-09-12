import logging
from typing import Dict, Optional
from decimal import Decimal
from models import ScoringCriteria
from app import db

logger = logging.getLogger(__name__)

class WeightManager:
    """Handles all scoring weight operations"""
    
    def load_profile_weights(self, profile: str) -> Dict[str, float]:
        """Load weights for a specific profile from database, fallback to Config"""
        try:
            criteria = ScoringCriteria.query.filter_by(
                active=True,
                profile=profile
            ).all()
            
            if criteria:
                db_weights = {c.criteria_name: float(c.weight) for c in criteria}
                return self.normalize_weights(db_weights)
            
            # Fallback to config
            from config import Config
            if hasattr(Config, 'SCORING_PROFILES') and profile in Config.SCORING_PROFILES:
                return Config.SCORING_PROFILES[profile].copy()
            
            logger.warning(f"No weights found for profile '{profile}'")
            return {}
            
        except Exception as e:
            logger.error(f"Failed to load profile weights: {str(e)}")
            return {}
    
    def normalize_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Normalize weights to sum to 1.0 (MCDM requirement)"""
        if not weights:
            return {}
        
        total_weight = sum(weights.values())
        if total_weight <= 0:
            logger.error("Invalid weights: sum is zero or negative")
            return {}
        
        # Normalize to sum = 1.0
        normalized = {k: v / total_weight for k, v in weights.items()}
        
        # Validate normalization
        new_sum = sum(normalized.values())
        if abs(new_sum - 1.0) > 0.001:
            logger.warning(f"Weight normalization imprecise: sum={new_sum:.6f}")
        
        logger.info(f"Normalized weights for profile: {normalized}")
        return normalized
    
    def update_profile_weights(self, profile: str, new_weights: Dict[str, float]) -> bool:
        """Update weights for a specific profile"""
        try:
            # Validate profile
            valid_profiles = ['combined', 'investment', 'lifestyle']
            if profile not in valid_profiles:
                raise ValueError(f"Invalid profile: {profile}")
            
            # Normalize weights
            normalized_weights = self.normalize_weights(new_weights)
            if not normalized_weights:
                return False
            
            # Update database
            from models import ScoringCriteria
            for criteria_name, weight in normalized_weights.items():
                criterion = ScoringCriteria.query.filter_by(
                    criteria_name=criteria_name,
                    profile=profile
                ).first()
                
                if criterion:
                    criterion.weight = Decimal(str(weight))
                else:
                    # Create new ScoringCriteria instance properly
                    criterion = ScoringCriteria()
                    criterion.criteria_name = criteria_name
                    criterion.profile = profile
                    criterion.weight = Decimal(str(weight))
                    criterion.active = True
                    db.session.add(criterion)
            
            db.session.commit()
            logger.info(f"Updated weights for profile '{profile}': {normalized_weights}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update weights: {str(e)}")
            db.session.rollback()
            return False