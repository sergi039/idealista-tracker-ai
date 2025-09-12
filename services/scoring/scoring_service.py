import logging
from decimal import Decimal
from typing import Dict, Optional
from services.scoring.weight_manager import WeightManager
from services.scoring.score_calculator import ScoreCalculator

logger = logging.getLogger(__name__)

class ScoringService:
    """Main scoring service - coordinates other scoring components"""
    
    def __init__(self):
        self.weight_manager = WeightManager()
        self.score_calculator = ScoreCalculator()
    
    def calculate_score(self, land) -> float:
        """Calculate dual scores using MCDM methodology"""
        try:
            # Calculate individual criterion scores
            individual_scores = self.score_calculator.calculate_individual_scores(land)
            
            # Calculate profile scores
            investment_score = self._calculate_profile_score(individual_scores, 'investment')
            lifestyle_score = self._calculate_profile_score(individual_scores, 'lifestyle')
            
            # Calculate combined score
            combined_score = self._calculate_combined_score(investment_score, lifestyle_score)
            
            # Update land record
            land.score_investment = Decimal(str(round(investment_score, 2)))
            land.score_lifestyle = Decimal(str(round(lifestyle_score, 2)))
            land.score_total = Decimal(str(round(combined_score, 2)))
            
            # Store scoring breakdown for transparency
            self._store_scoring_breakdown(land, individual_scores, investment_score, lifestyle_score, combined_score)
            
            logger.info(f"Calculated scores for land {land.id}: "
                       f"Investment={investment_score:.1f}, Lifestyle={lifestyle_score:.1f}, "
                       f"Combined={combined_score:.1f}")
            
            return combined_score
            
        except Exception as e:
            logger.error(f"Failed to calculate score for land {land.id}: {str(e)}")
            return 0.0
    
    def _calculate_profile_score(self, individual_scores: Dict[str, Optional[float]], profile: str) -> float:
        """Calculate MCDM score for a specific profile"""
        try:
            profile_weights = self.weight_manager.load_profile_weights(profile)
            
            if not profile_weights:
                logger.error(f"No weights found for profile: {profile}")
                return 0.0
            
            total_score = 0.0
            weight_sum_used = 0.0
            
            for criterion, weight in profile_weights.items():
                if criterion in individual_scores and individual_scores[criterion] is not None:
                    score = individual_scores[criterion]
                    total_score += score * weight
                    weight_sum_used += weight
            
            # Normalize by actually used weights (handle missing data)
            if weight_sum_used > 0:
                normalized_score = total_score / weight_sum_used
                return min(100, max(0, normalized_score))
            else:
                logger.warning(f"No valid criteria found for profile '{profile}'")
                return 0.0
                
        except Exception as e:
            logger.error(f"Failed to calculate profile score for '{profile}': {str(e)}")
            return 0.0
    
    def _calculate_combined_score(self, investment_score: float, lifestyle_score: float) -> float:
        """Calculate combined score using configured mix"""
        try:
            from config import Config
            mix = getattr(Config, 'COMBINED_MIX', {'investment': 0.32, 'lifestyle': 0.68})
            
            combined = (investment_score * mix['investment'] + 
                       lifestyle_score * mix['lifestyle'])
            
            return min(100, max(0, combined))
            
        except Exception as e:
            logger.error(f"Failed to calculate combined score: {str(e)}")
            return 0.0
    
    def _store_scoring_breakdown(self, land, individual_scores, investment_score, lifestyle_score, combined_score):
        """Store detailed scoring breakdown for transparency"""
        try:
            if not hasattr(land, 'environment') or land.environment is None:
                land.environment = {}
            
            scoring_data = {
                'individual_scores': {k: v for k, v in individual_scores.items() if v is not None},
                'profile_scores': {
                    'investment': round(investment_score, 2),
                    'lifestyle': round(lifestyle_score, 2),
                    'combined': round(combined_score, 2)
                },
                'weights_used': {
                    'investment': self.weight_manager.load_profile_weights('investment'),
                    'lifestyle': self.weight_manager.load_profile_weights('lifestyle')
                },
                'scoring_timestamp': None  # Will be set by enrichment service
            }
            
            if isinstance(land.environment, dict):
                land.environment['scoring'] = scoring_data
            else:
                # Handle JSONB column properly
                env_dict = land.environment if land.environment else {}
                env_dict['scoring'] = scoring_data
                land.environment = env_dict
                
        except Exception as e:
            logger.error(f"Failed to store scoring breakdown: {str(e)}")
    
    # Legacy method compatibility with existing code
    def update_weights(self, weights: Dict[str, float], profile: str = 'combined') -> bool:
        """Legacy compatibility method - delegates to WeightManager"""
        return self.weight_manager.update_profile_weights(profile, weights)
    
    # Support for dual profile updates
    def update_dual_profile_weights(self, investment_weights: Dict[str, float], 
                                  lifestyle_weights: Dict[str, float]) -> bool:
        """Update both investment and lifestyle profile weights"""
        try:
            investment_success = self.weight_manager.update_profile_weights('investment', investment_weights)
            lifestyle_success = self.weight_manager.update_profile_weights('lifestyle', lifestyle_weights)
            
            return investment_success and lifestyle_success
            
        except Exception as e:
            logger.error(f"Failed to update dual profile weights: {str(e)}")
            return False
    
    def batch_calculate_scores(self, lands, batch_size: int = 50) -> int:
        """Calculate scores for multiple lands efficiently"""
        try:
            from app import db
            
            total_processed = 0
            failed_count = 0
            
            for i in range(0, len(lands), batch_size):
                batch = lands[i:i + batch_size]
                
                for land in batch:
                    try:
                        self.calculate_score(land)
                        total_processed += 1
                    except Exception as e:
                        logger.error(f"Failed to score land {land.id} in batch: {str(e)}")
                        failed_count += 1
                
                # Commit batch
                try:
                    db.session.commit()
                    logger.info(f"Committed batch {i//batch_size + 1}: {len(batch)} lands processed")
                except Exception as e:
                    logger.error(f"Failed to commit batch {i//batch_size + 1}: {str(e)}")
                    db.session.rollback()
                    failed_count += len(batch)
            
            logger.info(f"Batch scoring completed: {total_processed} successful, {failed_count} failed")
            return total_processed
            
        except Exception as e:
            logger.error(f"Failed during batch scoring: {str(e)}")
            return 0