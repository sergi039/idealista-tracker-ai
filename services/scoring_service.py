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
        """Load custom scoring weights from database and normalize using MCDM methodology"""
        try:
            from models import ScoringCriteria
            
            criteria = ScoringCriteria.query.filter_by(active=True).all()
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
                    logger.info(f"Loaded and normalized MCDM weights (sum={sum(custom_weights.values()):.3f}): {custom_weights}")
            
        except Exception as e:
            logger.error(f"Failed to load custom weights: {str(e)}")
    
    def calculate_score(self, land) -> float:
        """Calculate total score using MCDM methodology (Multi-Criteria Decision Making)
        Compliant with ISO 31000 and RICS professional real estate evaluation standards"""
        try:
            scores = {}
            
            # Calculate individual scores (each returns 0-100)
            scores['investment_yield'] = self._score_investment_yield(land)
            scores['location_quality'] = self._score_location_quality(land)
            scores['transport'] = self._score_transport(land)
            scores['infrastructure_basic'] = self._score_infrastructure_basic(land)
            scores['infrastructure_extended'] = self._score_infrastructure_extended(land)
            scores['environment'] = self._score_environment(land)
            scores['physical_characteristics'] = self._score_physical_characteristics(land)
            scores['services_quality'] = self._score_services_quality(land)
            scores['legal_status'] = self._score_legal_status(land)
            scores['development_potential'] = self._score_development_potential(land)
            
            # Ensure weights are normalized (MCDM requirement)
            total_weight = sum(self.weights.values())
            if abs(total_weight - 1.0) > 0.001:
                logger.warning(f"Weights not properly normalized (sum={total_weight:.3f}), normalizing...")
                for key in self.weights:
                    self.weights[key] = self.weights[key] / total_weight if total_weight > 0 else 0
            
            # Calculate MCDM weighted score (0-100 scale)
            total_score = 0
            weight_sum_used = 0
            
            for criterion, score in scores.items():
                if score is not None and criterion in self.weights:
                    weight = self.weights[criterion]
                    # MCDM: score * normalized_weight (where weights sum to 1.0)
                    total_score += score * weight
                    weight_sum_used += weight
            
            # Final score with MCDM validation - normalize by actually used weights
            if weight_sum_used > 0:
                # Correct MCDM: normalize by used weights to account for missing data
                normalized_score = total_score / weight_sum_used
                final_score = min(100, max(0, normalized_score))
            else:
                # No valid criteria found
                final_score = 0
            
            # Update land record - convert to Decimal for proper database storage
            land.score_total = Decimal(str(round(final_score, 2)))
            
            # Store MCDM breakdown for transparency
            if not land.environment:
                land.environment = {}
            land.environment['score_breakdown'] = scores
            land.environment['mcdm_weights_used'] = dict(self.weights)
            land.environment['mcdm_weight_sum'] = weight_sum_used
            
            logger.info(f"MCDM score calculated for land {land.id}: {final_score} (weights_sum={weight_sum_used:.3f})")
            return final_score
            
        except Exception as e:
            logger.error(f"Failed to calculate MCDM score for land {land.id}: {str(e)}")
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
    
    def update_weights(self, new_weights: Dict[str, float]) -> bool:
        """Update scoring weights and rescore all lands"""
        try:
            from models import ScoringCriteria, Land
            from app import db
            
            # Update or create criteria records
            for criteria_name, weight in new_weights.items():
                criterion = ScoringCriteria.query.filter_by(
                    criteria_name=criteria_name
                ).first()
                
                if criterion:
                    criterion.weight = weight
                else:
                    criterion = ScoringCriteria()
                    criterion.criteria_name = criteria_name
                    criterion.weight = weight
                    db.session.add(criterion)
            
            db.session.commit()
            
            # Update local weights
            self.weights.update(new_weights)
            
            # Rescore all lands
            lands = Land.query.all()
            for land in lands:
                self.calculate_score(land)
            
            db.session.commit()
            
            logger.info(f"Updated scoring weights and rescored {len(lands)} lands")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update weights: {str(e)}")
            return False
    
    def get_current_weights(self) -> Dict[str, float]:
        """Get current scoring weights"""
        return self.weights.copy()
