import logging
from decimal import Decimal
from typing import Dict, Optional
from config import Config

logger = logging.getLogger(__name__)

class ScoringService:
    def __init__(self):
        self.weights = Config.DEFAULT_SCORING_WEIGHTS
        self.load_custom_weights()
    
    def load_custom_weights(self):
        """Load custom scoring weights from database and normalize using MCDM methodology
        Falls back to Config profiles when no profile-specific DB weights exist"""
        try:
            from models import ScoringCriteria
            
            # Load legacy weights (profile=NULL or 'combined') for backward compatibility
            criteria = ScoringCriteria.query.filter_by(active=True).filter(
                (ScoringCriteria.profile == 'combined') | (ScoringCriteria.profile == None)
            ).all()
            
            if criteria:
                custom_weights = {}
                for criterion in criteria:
                    custom_weights[criterion.criteria_name] = float(criterion.weight)
                
                # MCDM normalization: ensure weights sum to 1.0
                if custom_weights:
                    total_weight = sum(custom_weights.values())
                    if total_weight > 0:
                        # Normalize weights to sum to 1.0 (ISO 31000, RICS standards)
                        for key in custom_weights:
                            custom_weights[key] = custom_weights[key] / total_weight
                    
                    self.weights.update(custom_weights)
                    logger.info(f"Loaded and normalized legacy MCDM weights (sum={sum(custom_weights.values()):.3f}): {custom_weights}")
            
            # Validate profiles on load
            self._validate_profiles()
            
        except Exception as e:
            logger.error(f"Failed to load custom weights: {str(e)}")
    
    def calculate_score(self, land) -> float:
        """Calculate dual scores using MCDM methodology (Multi-Criteria Decision Making)
        Computes Investment Score, Lifestyle Score, and Combined Score
        Compliant with ISO 31000 and RICS professional real estate evaluation standards"""
        try:
            # Calculate individual criterion scores once (each returns 0-100)
            individual_scores = {}
            individual_scores['investment_yield'] = self._score_investment_yield(land)
            individual_scores['location_quality'] = self._score_location_quality(land)
            individual_scores['transport'] = self._score_transport(land)
            individual_scores['infrastructure_basic'] = self._score_infrastructure_basic(land)
            individual_scores['infrastructure_extended'] = self._score_infrastructure_extended(land)
            individual_scores['environment'] = self._score_environment(land)
            individual_scores['physical_characteristics'] = self._score_physical_characteristics(land)
            individual_scores['services_quality'] = self._score_services_quality(land)
            individual_scores['legal_status'] = self._score_legal_status(land)
            individual_scores['development_potential'] = self._score_development_potential(land)
            
            # Calculate Investment Score using Investment Profile
            investment_score = self._calculate_profile_score(individual_scores, 'investment')
            
            # Calculate Lifestyle Score using Lifestyle Profile
            lifestyle_score = self._calculate_profile_score(individual_scores, 'lifestyle')
            
            # Calculate Combined Score using COMBINED_MIX
            from config import Config
            mix = Config.COMBINED_MIX
            combined_score = (investment_score * mix['investment'] + 
                            lifestyle_score * mix['lifestyle'])
            
            # Update land record with all three scores
            land.score_investment = Decimal(str(round(investment_score, 2)))
            land.score_lifestyle = Decimal(str(round(lifestyle_score, 2)))
            land.score_total = Decimal(str(round(combined_score, 2)))
            
            # Store comprehensive MCDM breakdown for transparency
            if not land.environment:
                land.environment = {}
            
            land.environment['scoring'] = {
                'individual_scores': individual_scores,
                'profiles': {
                    'investment': {
                        'score': investment_score,
                        'weights_used': self._get_profile_weights_used(individual_scores, 'investment'),
                        'score_breakdown': self._get_profile_breakdown(individual_scores, 'investment')
                    },
                    'lifestyle': {
                        'score': lifestyle_score,
                        'weights_used': self._get_profile_weights_used(individual_scores, 'lifestyle'),
                        'score_breakdown': self._get_profile_breakdown(individual_scores, 'lifestyle')
                    }
                },
                'combined_mix': mix,
                'combined_score': combined_score
            }
            
            logger.info(f"Dual MCDM scores calculated for land {land.id}: "
                       f"Investment={investment_score:.1f}, Lifestyle={lifestyle_score:.1f}, "
                       f"Combined={combined_score:.1f}")
            return combined_score
            
        except Exception as e:
            logger.error(f"Failed to calculate dual MCDM scores for land {land.id}: {str(e)}")
            return 0
    
    def _score_infrastructure_basic(self, land) -> Optional[float]:
        """Score basic infrastructure (electricity, water, internet, gas)"""
        try:
            if not land.infrastructure_basic:
                return None
            
            basic_infra = land.infrastructure_basic
            score = 0
            max_score = 4  # 4 basic utilities
            
            # Check for basic utilities mentions in description
            description = (land.description or "").lower()
            
            utilities = {
                'electricity': ['electricidad', 'luz', 'eléctrico'],
                'water': ['agua', 'suministro agua', 'abastecimiento'],
                'internet': ['internet', 'fibra', 'adsl', 'wifi'],
                'gas': ['gas', 'butano', 'propano']
            }
            
            for utility, keywords in utilities.items():
                if basic_infra.get(utility) or any(kw in description for kw in keywords):
                    score += 1
            
            return (score / max_score) * 100
            
        except Exception as e:
            logger.error(f"Failed to score basic infrastructure: {str(e)}")
            return None
    
    def _score_infrastructure_extended(self, land) -> Optional[float]:
        """Score extended infrastructure (supermarket, school, restaurants, hospital)"""
        try:
            if not land.infrastructure_extended:
                return None
            
            extended_infra = land.infrastructure_extended
            score = 0
            
            # Score based on availability and distance
            amenities = ['supermarket', 'school', 'restaurant', 'hospital']
            
            for amenity in amenities:
                if extended_infra.get(f'{amenity}_available'):
                    distance = extended_infra.get(f'{amenity}_distance', float('inf'))
                    
                    # Score based on distance (closer is better)
                    if distance <= 1000:  # Within 1km
                        score += 25
                    elif distance <= 3000:  # Within 3km
                        score += 15
                    elif distance <= 5000:  # Within 5km
                        score += 10
                    else:
                        score += 5
            
            return min(score, 100)  # Cap at 100
            
        except Exception as e:
            logger.error(f"Failed to score extended infrastructure: {str(e)}")
            return None
    
    def _score_transport(self, land) -> Optional[float]:
        """Score transport accessibility"""
        try:
            if not land.transport:
                return None
            
            transport = land.transport
            score = 0
            
            # Score transport options
            transport_options = {
                'train_station': 30,
                'bus_station': 20,
                'airport': 25,
                'highway': 25
            }
            
            for option, max_points in transport_options.items():
                if transport.get(f'{option}_available'):
                    distance = transport.get(f'{option}_distance', float('inf'))
                    
                    # Score based on distance
                    if distance <= 2000:  # Within 2km
                        score += max_points
                    elif distance <= 5000:  # Within 5km
                        score += max_points * 0.7
                    elif distance <= 10000:  # Within 10km
                        score += max_points * 0.4
                    else:
                        score += max_points * 0.2
            
            return min(score, 100)  # Cap at 100
            
        except Exception as e:
            logger.error(f"Failed to score transport: {str(e)}")
            return None
    
    def _score_environment(self, land) -> Optional[float]:
        """Score environment features"""
        try:
            if not land.environment:
                return None
            
            environment = land.environment
            score = 0
            
            # View bonuses
            if environment.get('sea_view'):
                score += 40
            if environment.get('mountain_view'):
                score += 30
            if environment.get('forest_view'):
                score += 20
            
            # Orientation bonus (south-facing is preferred in Spain)
            orientation = environment.get('orientation', '').lower()
            if 'south' in orientation:
                score += 20
            elif 'southeast' in orientation or 'southwest' in orientation:
                score += 15
            elif 'east' in orientation or 'west' in orientation:
                score += 10
            
            return min(score, 100)  # Cap at 100
            
        except Exception as e:
            logger.error(f"Failed to score environment: {str(e)}")
            return None
    
    def _score_neighborhood(self, land) -> Optional[float]:
        """Score neighborhood characteristics"""
        try:
            if not land.neighborhood:
                return 50  # Default neutral score
            
            neighborhood = land.neighborhood
            score = 50  # Start with neutral score
            
            # Price level impact
            price_level = neighborhood.get('area_price_level', 'medium')
            if price_level == 'high':
                score += 20
            elif price_level == 'medium':
                score += 10
            
            # New houses nearby (indicates development)
            if neighborhood.get('new_houses'):
                score += 15
            
            # Noise level impact
            noise_level = neighborhood.get('noise', 'medium')
            if noise_level == 'low':
                score += 15
            elif noise_level == 'high':
                score -= 15
            
            return min(max(score, 0), 100)  # Keep between 0-100
            
        except Exception as e:
            logger.error(f"Failed to score neighborhood: {str(e)}")
            return None
    
    def _score_services_quality(self, land) -> Optional[float]:
        """Score quality of nearby services"""
        try:
            if not land.services_quality:
                return None
            
            services = land.services_quality
            score = 0
            count = 0
            
            # Average ratings of nearby services
            service_types = ['school_avg_rating', 'restaurant_avg_rating', 'cafe_avg_rating']
            
            for service_type in service_types:
                rating = services.get(service_type)
                if rating and rating > 0:
                    # Convert rating (1-5 scale) to percentage
                    score += (rating / 5) * 100
                    count += 1
            
            if count > 0:
                return score / count
            else:
                return None
            
        except Exception as e:
            logger.error(f"Failed to score services quality: {str(e)}")
            return None
    
    def _score_legal_status(self, land) -> Optional[float]:
        """Score legal status"""
        try:
            legal_status = (land.legal_status or "").lower()
            land_type = (land.land_type or "").lower()
            
            # Only developed and buildable are acceptable
            if 'developed' in legal_status or land_type == 'developed':
                return 100  # Fully developed land
            elif 'buildable' in legal_status or land_type == 'buildable':
                return 80   # Buildable land (some risk)
            else:
                return 0    # Rustic or other (not suitable)
            
        except Exception as e:
            logger.error(f"Failed to score legal status: {str(e)}")
            return None
    
    def _score_location_quality(self, land) -> Optional[float]:
        """Score location quality based on neighborhood and proximity to urban centers"""
        try:
            score = 50  # Base score
            
            # Municipality quality check
            municipality = (land.municipality or "").lower()
            
            # Premium locations in Spain
            premium_locations = ['madrid', 'barcelona', 'valencia', 'sevilla', 'bilbao', 
                                'málaga', 'santander', 'oviedo', 'gijón']
            secondary_locations = ['suances', 'ribadedeva', 'llanes', 'ribadesella']
            
            for loc in premium_locations:
                if loc in municipality:
                    score = 90
                    break
            
            for loc in secondary_locations:
                if loc in municipality:
                    score = 70
                    break
            
            # Use neighborhood data if available
            if land.neighborhood:
                # Adjust based on neighborhood factors
                if land.neighborhood.get('population_density'):
                    density = land.neighborhood.get('population_density')
                    if density > 1000:  # High density urban
                        score += 10
                    elif density > 100:  # Suburban
                        score += 5
            
            return min(100, score)
            
        except Exception as e:
            logger.error(f"Failed to score location quality: {str(e)}")
            return 50  # Default middle score
    
    def _score_physical_characteristics(self, land) -> Optional[float]:
        """Score physical characteristics like size, shape, topography"""
        try:
            score = 70  # Base score
            
            # Area scoring - ideal range 1000-5000 m²
            if land.area:
                if 1000 <= land.area <= 5000:
                    score += 20  # Ideal size
                elif 500 <= land.area < 1000:
                    score += 10  # Small but acceptable
                elif 5000 < land.area <= 10000:
                    score += 15  # Large, good for development
                elif land.area > 10000:
                    score += 10  # Very large, may have challenges
            
            # Price per m² indicator (if price and area available)
            if land.price and land.area and land.area > 0:
                price_per_sqm = land.price / land.area
                if price_per_sqm < 50:  # Very affordable
                    score += 10
                elif price_per_sqm < 100:  # Reasonable
                    score += 5
            
            return min(100, score)
            
        except Exception as e:
            logger.error(f"Failed to score physical characteristics: {str(e)}")
            return 50  # Default middle score
    
    def _score_development_potential(self, land) -> Optional[float]:
        """Score future development potential"""
        try:
            score = 50  # Base score
            
            # Land type is key indicator
            land_type = (land.land_type or "").lower()
            
            if land_type == 'developed':
                score = 30  # Already developed, less potential
            elif land_type == 'buildable':
                score = 80  # High development potential
            
            # Check for urbanization mentions in description
            description = (land.description or "").lower()
            
            positive_keywords = ['urbanizable', 'desarrollo', 'proyecto aprobado', 
                               'plan parcial', 'licencia', 'permiso']
            negative_keywords = ['protegido', 'rustico', 'no urbanizable', 
                               'restricción', 'zona verde']
            
            for keyword in positive_keywords:
                if keyword in description:
                    score += 10
                    break
            
            for keyword in negative_keywords:
                if keyword in description:
                    score -= 20
                    break
            
            return min(100, max(0, score))
            
        except Exception as e:
            logger.error(f"Failed to score development potential: {str(e)}")
            return 50  # Default middle score
    
    def _score_investment_yield(self, land) -> Optional[float]:
        """Score investment yield based on rental potential and cap rate
        Uses MarketAnalysisService to calculate rental analysis metrics"""
        try:
            from services.market_analysis_service import MarketAnalysisService
            
            market_service = MarketAnalysisService()
            rental_analysis = market_service.calculate_rental_analysis(land)
            
            if not rental_analysis:
                logger.debug(f"No rental analysis data available for land {land.id}")
                return None
            
            # Extract key metrics
            rental_yield = rental_analysis.get('rental_yield')
            cap_rate = rental_analysis.get('cap_rate')
            
            if rental_yield is None and cap_rate is None:
                logger.debug(f"No yield or cap rate data available for land {land.id}")
                return None
            
            # Score rental yield (0-100 scale)
            yield_score = 0
            if rental_yield is not None:
                if rental_yield >= 6.0:
                    # 6%+ yield = 90-100 points (scale with yield up to 10%)
                    yield_score = min(100, 90 + (rental_yield - 6.0) * 2.5)
                elif rental_yield >= 4.0:
                    # 4-6% yield = 75 points
                    yield_score = 75
                elif rental_yield >= 2.0:
                    # 2-4% yield = 50 points
                    yield_score = 50
                else:
                    # 0-2% yield = 20 points
                    yield_score = 20
            
            # Score cap rate (0-100 scale, similar logic)
            cap_score = 0
            if cap_rate is not None:
                if cap_rate >= 6.0:
                    cap_score = min(100, 90 + (cap_rate - 6.0) * 2.5)
                elif cap_rate >= 4.0:
                    cap_score = 75
                elif cap_rate >= 2.0:
                    cap_score = 50
                else:
                    cap_score = 20
            
            # Combine scores: rental_yield (60%) + cap_rate (40%)
            final_score = 0
            weight_sum = 0
            
            if rental_yield is not None:
                final_score += yield_score * 0.6
                weight_sum += 0.6
            
            if cap_rate is not None:
                final_score += cap_score * 0.4
                weight_sum += 0.4
            
            if weight_sum > 0:
                # Normalize by available weights
                normalized_score = final_score / weight_sum
                result = min(100, max(0, normalized_score))
                
                logger.info(f"Investment yield score for land {land.id}: {result:.1f} "
                           f"(rental_yield={rental_yield}%, cap_rate={cap_rate}%)")
                return result
            else:
                return None
            
        except Exception as e:
            logger.error(f"Failed to score investment yield for land {land.id}: {str(e)}")
            return None
    
    def update_weights(self, new_weights: Dict[str, float], profile: str = 'combined') -> bool:
        """Update scoring weights for a specific profile and rescore all lands"""
        try:
            from models import ScoringCriteria, Land
            from app import db
            
            # Validate profile
            if profile not in ['combined', 'investment', 'lifestyle']:
                logger.error(f"Invalid profile: {profile}")
                return False
            
            # Update or create criteria records for this profile
            for criteria_name, weight in new_weights.items():
                criterion = ScoringCriteria.query.filter_by(
                    criteria_name=criteria_name,
                    profile=profile
                ).first()
                
                if criterion:
                    criterion.weight = weight
                else:
                    criterion = ScoringCriteria()
                    criterion.criteria_name = criteria_name
                    criterion.profile = profile
                    criterion.weight = weight
                    db.session.add(criterion)
            
            db.session.commit()
            
            # Update local weights only if profile is 'combined' (legacy compatibility)
            if profile == 'combined':
                self.weights.update(new_weights)
            
            # Rescore all lands (they will use new profile weights)
            lands = Land.query.all()
            for land in lands:
                self.calculate_score(land)
            
            db.session.commit()
            
            logger.info(f"Updated {profile} profile weights and rescored {len(lands)} lands")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update weights for profile {profile}: {str(e)}")
            return False
    
    def get_current_weights(self) -> Dict[str, float]:
        """Get current scoring weights"""
        return self.weights.copy()
    
    def _calculate_profile_score(self, individual_scores: Dict[str, float], profile: str) -> float:
        """Calculate MCDM score for a specific profile (investment or lifestyle)"""
        try:
            from config import Config
            
            if profile not in Config.SCORING_PROFILES:
                logger.error(f"Unknown scoring profile: {profile}")
                return 0
            
            profile_weights = Config.SCORING_PROFILES[profile]
            
            # Ensure profile weights are normalized (MCDM requirement)
            total_weight = sum(profile_weights.values())
            if abs(total_weight - 1.0) > 0.001:
                logger.warning(f"Profile '{profile}' weights not properly normalized (sum={total_weight:.3f})")
                # Normalize on the fly
                profile_weights = {k: v / total_weight for k, v in profile_weights.items() if total_weight > 0}
            
            # Calculate MCDM weighted score for this profile
            total_score = 0
            weight_sum_used = 0
            
            for criterion, weight in profile_weights.items():
                if criterion in individual_scores and individual_scores[criterion] is not None:
                    score = individual_scores[criterion]
                    # MCDM: score * normalized_weight (where weights sum to 1.0)
                    total_score += score * weight
                    weight_sum_used += weight
            
            # Final score with MCDM validation - normalize by actually used weights
            if weight_sum_used > 0:
                # Correct MCDM: normalize by used weights to account for missing data
                normalized_score = total_score / weight_sum_used
                final_score = min(100, max(0, normalized_score))
            else:
                # No valid criteria found for this profile
                final_score = 0
            
            logger.debug(f"Profile '{profile}' score: {final_score:.1f} (weights_sum={weight_sum_used:.3f})")
            return final_score
            
        except Exception as e:
            logger.error(f"Failed to calculate profile score for '{profile}': {str(e)}")
            return 0
    
    def _get_profile_weights_used(self, individual_scores: Dict[str, float], profile: str) -> Dict[str, float]:
        """Get the actual weights used for a profile (excluding criteria with None scores)
        Loads from database first, falls back to Config if not found"""
        try:
            # Get profile weights (DB first, then Config fallback)
            profile_weights = self._load_profile_weights(profile)
            
            if not profile_weights:
                return {}
            
            weights_used = {}
            
            for criterion, weight in profile_weights.items():
                if criterion in individual_scores and individual_scores[criterion] is not None:
                    weights_used[criterion] = weight
            
            return weights_used
            
        except Exception as e:
            logger.error(f"Failed to get profile weights used for '{profile}': {str(e)}")
            return {}
    
    def _get_profile_breakdown(self, individual_scores: Dict[str, float], profile: str) -> Dict[str, float]:
        """Get score breakdown for a profile (only criteria with non-None scores)"""
        try:
            from config import Config
            
            if profile not in Config.SCORING_PROFILES:
                return {}
            
            profile_weights = Config.SCORING_PROFILES[profile]
            breakdown = {}
            
            for criterion, weight in profile_weights.items():
                if criterion in individual_scores and individual_scores[criterion] is not None:
                    breakdown[criterion] = individual_scores[criterion]
            
            return breakdown
            
        except Exception as e:
            logger.error(f"Failed to get profile breakdown for '{profile}': {str(e)}")
            return {}
    
    def _load_profile_weights(self, profile: str) -> Dict[str, float]:
        """Load weights for a specific profile from database, fallback to Config"""
        try:
            from models import ScoringCriteria
            from config import Config
            
            # First try to load from database
            criteria = ScoringCriteria.query.filter_by(
                active=True,
                profile=profile
            ).all()
            
            if criteria:
                db_weights = {}
                for criterion in criteria:
                    db_weights[criterion.criteria_name] = float(criterion.weight)
                
                # Normalize DB weights (MCDM requirement)
                total_weight = sum(db_weights.values())
                if total_weight > 0:
                    normalized_weights = {k: v / total_weight for k, v in db_weights.items()}
                    logger.info(f"Loaded {profile} profile weights from DB: {normalized_weights}")
                    return normalized_weights
            
            # Fallback to Config if no DB weights found
            if hasattr(Config, 'SCORING_PROFILES') and profile in Config.SCORING_PROFILES:
                config_weights = Config.SCORING_PROFILES[profile].copy()
                logger.info(f"Using Config fallback for {profile} profile: {config_weights}")
                return config_weights
            
            logger.warning(f"No weights found for profile '{profile}' in DB or Config")
            return {}
            
        except Exception as e:
            logger.error(f"Failed to load profile weights for '{profile}': {str(e)}")
            return {}

    def _validate_profiles(self):
        """Validate that SCORING_PROFILES and COMBINED_MIX are properly configured
        Called during service initialization to ensure data integrity"""
        try:
            from config import Config
            
            # Validate SCORING_PROFILES
            if not hasattr(Config, 'SCORING_PROFILES'):
                logger.error("SCORING_PROFILES not found in config")
                return
            
            for profile_name, weights in Config.SCORING_PROFILES.items():
                if not isinstance(weights, dict):
                    logger.error(f"Profile '{profile_name}' weights must be a dictionary")
                    continue
                
                # Check that weights sum to 1.0 (±0.001 tolerance)
                total_weight = sum(weights.values())
                if abs(total_weight - 1.0) > 0.001:
                    logger.warning(f"Profile '{profile_name}' weights sum to {total_weight:.3f}, expected 1.0. "
                                 f"Weights will be normalized at runtime.")
                
                # Check for unknown criteria
                valid_criteria = set(Config.DEFAULT_SCORING_WEIGHTS.keys())
                for criterion in weights.keys():
                    if criterion not in valid_criteria:
                        logger.warning(f"Profile '{profile_name}' contains unknown criterion: '{criterion}'")
            
            # Validate COMBINED_MIX
            if not hasattr(Config, 'COMBINED_MIX'):
                logger.error("COMBINED_MIX not found in config")
                return
            
            mix = Config.COMBINED_MIX
            required_keys = {'investment', 'lifestyle'}
            mix_keys = set(mix.keys())
            
            if mix_keys != required_keys:
                logger.error(f"COMBINED_MIX must contain exactly {required_keys}, got {mix_keys}")
            
            mix_sum = sum(mix.values())
            if abs(mix_sum - 1.0) > 0.001:
                logger.warning(f"COMBINED_MIX weights sum to {mix_sum:.3f}, expected 1.0")
            
            logger.info("Profile validation completed successfully")
            
        except Exception as e:
            logger.error(f"Failed to validate profiles: {str(e)}")
