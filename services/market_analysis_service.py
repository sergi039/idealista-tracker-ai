"""
Market Analysis Service
Provides construction cost estimates and market price dynamics
based on real data from Asturias region
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy import func, and_, or_
from models import Land
from app import db

logger = logging.getLogger(__name__)


class MarketAnalysisService:
    """Service for analyzing market trends and construction costs"""
    
    # Asturias average construction costs per m² (2024-2025 data)
    CONSTRUCTION_COSTS = {
        'basic': {
            'min': 800,   # €/m² - Basic construction
            'avg': 1000,  # €/m² - Standard construction  
            'max': 1200   # €/m² - Good quality construction
        },
        'premium': {
            'min': 1200,  # €/m² - Premium construction
            'avg': 1500,  # €/m² - High-end construction
            'max': 2000   # €/m² - Luxury construction
        }
    }
    
    # Typical buildability ratios for different land types in Asturias
    BUILDABILITY_RATIOS = {
        'developed': 0.25,      # 25% of land area can be built
        'undeveloped': 0.20,    # 20% for undeveloped land
        'rural': 0.15,          # 15% for rural land
        'default': 0.20         # Default 20%
    }
    
    # Rental market data for Asturias (2024-2025)
    RENTAL_YIELDS = {
        'urban': {
            'min': 3.5,    # % annual yield in major cities
            'avg': 4.5,    # % average yield
            'max': 5.5     # % maximum yield
        },
        'suburban': {
            'min': 4.0,    # % annual yield in suburban areas
            'avg': 5.0,    # % average yield  
            'max': 6.0     # % maximum yield
        },
        'rural': {
            'min': 5.0,    # % annual yield in rural areas
            'avg': 6.0,    # % average yield
            'max': 7.5     # % maximum yield (vacation rentals)
        }
    }
    
    # Average rental prices per m² per month in Asturias
    RENTAL_PRICES = {
        'urban': {'min': 8, 'avg': 10, 'max': 13},      # €/m²/month
        'suburban': {'min': 6, 'avg': 8, 'max': 10},    # €/m²/month
        'rural': {'min': 5, 'avg': 7, 'max': 9}         # €/m²/month
    }
    
    def _evaluate_construction_quality_objective(self, land: Land) -> Dict:
        """
        Evaluate construction quality based on objective criteria (score-independent)
        Returns construction tier and quality factors for transparency
        """
        quality_points = 0
        max_points = 100
        quality_factors = []
        
        # 1. Land Type Quality (25 points max)
        land_type = land.land_type or 'buildable'
        if land_type == 'developed':
            quality_points += 25
            quality_factors.append("Fully developed land - supports premium construction")
        elif land_type == 'buildable':
            quality_points += 20
            quality_factors.append("Buildable land - suitable for quality construction")
        
        # 2. Municipality Quality (20 points max)
        municipality = (land.municipality or "").lower()
        major_cities = ['gijón', 'oviedo', 'avilés', 'madrid', 'barcelona']
        secondary_cities = ['suances', 'ribadedeva', 'llanes', 'ribadesella', 'santander']
        
        if any(city in municipality for city in major_cities):
            quality_points += 20
            quality_factors.append(f"Premium location: {land.municipality}")
        elif any(city in municipality for city in secondary_cities):
            quality_points += 15
            quality_factors.append(f"Good location: {land.municipality}")
        else:
            quality_points += 10
            quality_factors.append(f"Rural location: {land.municipality or 'Unknown'}")
        
        # 3. Infrastructure Basic (20 points max)
        if land.infrastructure_basic:
            infra_count = 0
            basic_utilities = ['electricity', 'water', 'internet', 'gas']
            for utility in basic_utilities:
                if land.infrastructure_basic.get(utility):
                    infra_count += 1
            
            infra_score = (infra_count / len(basic_utilities)) * 20
            quality_points += infra_score
            quality_factors.append(f"Basic infrastructure: {infra_count}/4 utilities available")
        
        # 4. Area Optimization (15 points max)
        if land.area:
            area_val = float(land.area)
            if 1000 <= area_val <= 5000:
                quality_points += 15
                quality_factors.append(f"Optimal area: {area_val}m² - ideal for premium construction")
            elif 500 <= area_val < 1000:
                quality_points += 10
                quality_factors.append(f"Adequate area: {area_val}m² - suitable for standard construction")
            elif area_val > 5000:
                quality_points += 12
                quality_factors.append(f"Large area: {area_val}m² - excellent for estate development")
            else:
                quality_points += 5
                quality_factors.append(f"Small area: {area_val}m² - limited construction options")
        
        # 5. Transport Accessibility (10 points max)
        transport_score = 0
        if land.travel_time_airport and land.travel_time_airport <= 60:
            transport_score += 3
        if land.travel_time_train_station and land.travel_time_train_station <= 30:
            transport_score += 3
        if land.travel_time_hospital and land.travel_time_hospital <= 20:
            transport_score += 2
        if land.travel_time_oviedo and land.travel_time_oviedo <= 45:
            transport_score += 2
        
        quality_points += transport_score
        if transport_score >= 8:
            quality_factors.append("Excellent transport connectivity")
        elif transport_score >= 5:
            quality_factors.append("Good transport access")
        else:
            quality_factors.append("Limited transport connectivity")
        
        # 6. Environment Quality (10 points max)
        if land.environment:
            env_score = 0
            if land.environment.get('sea_view'):
                env_score += 5
                quality_factors.append("Sea view premium")
            if land.environment.get('mountain_view'):
                env_score += 3
                quality_factors.append("Mountain view")
            if land.environment.get('forest_view'):
                env_score += 2
                quality_factors.append("Forest surroundings")
            
            orientation = land.environment.get('orientation', '').lower()
            if 'south' in orientation:
                env_score += 2
                quality_factors.append("Optimal south orientation")
            
            quality_points += min(env_score, 10)
        
        # Determine construction tier based on total quality points
        quality_percentage = (quality_points / max_points) * 100
        
        if quality_percentage >= 65:
            return {
                'construction_tier': 'premium',
                'quality_score': round(quality_percentage, 1),
                'quality_factors': quality_factors,
                'tier_reasoning': f'High quality score ({quality_percentage:.1f}%) indicates premium construction potential'
            }
        else:
            return {
                'construction_tier': 'basic',
                'quality_score': round(quality_percentage, 1),
                'quality_factors': quality_factors,
                'tier_reasoning': f'Quality score ({quality_percentage:.1f}%) indicates standard construction level'
            }

    def calculate_construction_value(self, land: Land) -> Dict:
        """
        Calculate construction value estimates based on objective land characteristics
        Uses score-independent criteria to avoid circular dependency
        """
        try:
            area = float(land.area) if land.area else 0
            land_type = land.land_type or 'default'
            
            # Use objective quality evaluation instead of score dependency
            quality_eval = self._evaluate_construction_quality_objective(land)
            construction_tier = quality_eval['construction_tier']
            
            # Determine buildable area based on land type
            buildability_ratio = self.BUILDABILITY_RATIOS.get(land_type, self.BUILDABILITY_RATIOS['default'])
            buildable_area = area * buildability_ratio
            
            # Select construction costs based on objective quality evaluation
            if construction_tier == 'premium':
                costs = self.CONSTRUCTION_COSTS['premium']
                construction_type = "Premium villa with modern amenities"
            else:
                costs = self.CONSTRUCTION_COSTS['basic']
                construction_type = "Standard single-family home"
            
            # Calculate values
            min_value = buildable_area * costs['min']
            avg_value = buildable_area * costs['avg']
            max_value = buildable_area * costs['max']
            
            # Total investment including land price
            land_price = float(land.price) if land.price else 0
            total_investment_min = land_price + min_value
            total_investment_avg = land_price + avg_value
            total_investment_max = land_price + max_value
            
            return {
                'minimum_value': round(min_value),
                'average_value': round(avg_value),
                'maximum_value': round(max_value),
                'buildable_area': round(buildable_area),
                'construction_type': construction_type,
                'construction_tier': construction_tier,
                'value_per_m2': costs['avg'],
                'total_investment_min': round(total_investment_min),
                'total_investment_avg': round(total_investment_avg),
                'total_investment_max': round(total_investment_max),
                'buildability_ratio': f"{buildability_ratio * 100:.0f}%",
                'quality_evaluation': quality_eval
            }
            
        except Exception as e:
            logger.error(f"Error calculating construction value: {str(e)}")
            return {}
    
    def analyze_market_trends(self, land: Land) -> Dict:
        """
        Analyze market price dynamics based on similar properties in the database
        """
        try:
            # Get similar properties from the same municipality or nearby areas
            similar_query = db.session.query(Land).filter(
                and_(
                    Land.id != land.id,
                    or_(
                        Land.municipality == land.municipality,
                        Land.province == land.province
                    )
                )
            )
            
            # Calculate price per m² statistics
            price_per_m2_data = []
            for prop in similar_query.all():
                if prop.price and prop.area and prop.area > 0:
                    price_per_m2 = float(prop.price) / float(prop.area)
                    price_per_m2_data.append({
                        'price_per_m2': price_per_m2,
                        'date': prop.created_at,
                        'land_type': prop.land_type
                    })
            
            if not price_per_m2_data:
                return self._get_default_market_trends()
            
            # Sort by date to analyze trends
            price_per_m2_data.sort(key=lambda x: x['date'] if x['date'] else datetime.now())
            
            # Calculate statistics
            prices = [d['price_per_m2'] for d in price_per_m2_data]
            avg_price = sum(prices) / len(prices)
            min_price = min(prices)
            max_price = max(prices)
            
            # Analyze trend (simplified - comparing recent vs older prices)
            recent_cutoff = datetime.now() - timedelta(days=180)
            recent_prices = [d['price_per_m2'] for d in price_per_m2_data if d['date'] and d['date'] > recent_cutoff]
            older_prices = [d['price_per_m2'] for d in price_per_m2_data if d['date'] and d['date'] <= recent_cutoff]
            
            if recent_prices and older_prices:
                recent_avg = sum(recent_prices) / len(recent_prices)
                older_avg = sum(older_prices) / len(older_prices)
                growth_rate = ((recent_avg - older_avg) / older_avg) * 100
                
                if growth_rate > 5:
                    trend = "RISING"
                    trend_analysis = f"Prices have increased by {growth_rate:.1f}% in the last 6 months"
                elif growth_rate < -5:
                    trend = "DECLINING"
                    trend_analysis = f"Prices have decreased by {abs(growth_rate):.1f}% in the last 6 months"
                else:
                    trend = "STABLE"
                    trend_analysis = "Prices have remained relatively stable"
            else:
                trend = "STABLE"
                growth_rate = 3.5  # Default Asturias average
                trend_analysis = "Insufficient data for trend analysis - using regional average"
            
            # Market factors specific to Asturias
            market_factors = []
            if land.municipality:
                if any(city in land.municipality.lower() for city in ['gijón', 'oviedo', 'avilés']):
                    market_factors.append("Major urban center driving demand")
                if 'beach' in str(land.scoring_results).lower() or land.beach_access_min:
                    market_factors.append("Coastal location premium")
                # Check for high-quality infrastructure using objective criteria
                high_quality_infra = False
                if land.infrastructure_basic:
                    basic_count = sum(1 for v in land.infrastructure_basic.values() if v)
                    if basic_count >= 3:  # Most utilities available
                        high_quality_infra = True
                if land.infrastructure_extended:
                    extended_count = sum(1 for k, v in land.infrastructure_extended.items() 
                                       if k.endswith('_available') and v)
                    if extended_count >= 2:  # Multiple amenities available
                        high_quality_infra = True
                        
                if high_quality_infra:
                    market_factors.append("High-quality infrastructure and services")
            
            if not market_factors:
                market_factors = [
                    "Rural tourism development in Asturias",
                    "Growing remote work population",
                    "Infrastructure improvements in the region"
                ]
            
            return {
                'price_trend': trend,
                'annual_growth_rate': abs(growth_rate) if growth_rate else 3.5,
                'trend_period': '2024-2025',
                'trend_analysis': trend_analysis,
                'future_outlook': f"Expected {abs(growth_rate):.1f}% annual growth based on current market conditions",
                'market_factors': market_factors,
                'avg_price_per_m2': round(avg_price),
                'min_price_per_m2': round(min_price),
                'max_price_per_m2': round(max_price),
                'sample_size': len(price_per_m2_data)
            }
            
        except Exception as e:
            logger.error(f"Error analyzing market trends: {str(e)}")
            return self._get_default_market_trends()
    
    def calculate_rental_analysis(self, land: Land, construction_data: Dict = None) -> Dict:
        """
        Calculate rental market analysis and investment metrics
        """
        try:
            # Determine location type based on municipality
            location_type = 'rural'  # default
            if land.municipality:
                municipality_lower = land.municipality.lower()
                if any(city in municipality_lower for city in ['gijón', 'oviedo', 'avilés']):
                    location_type = 'urban'
                elif any(area in municipality_lower for city in ['gijón', 'oviedo', 'avilés'] for area in [city]):
                    # Check if it's near major cities (simplified logic)
                    location_type = 'suburban'
            
            # Get rental yields and prices for location
            yields = self.RENTAL_YIELDS.get(location_type, self.RENTAL_YIELDS['rural'])
            rental_prices = self.RENTAL_PRICES.get(location_type, self.RENTAL_PRICES['rural'])
            
            # Calculate property value (land + construction)
            if not construction_data:
                construction_data = self.calculate_construction_value(land)
            
            total_investment = construction_data.get('total_investment_avg', 0)
            buildable_area = construction_data.get('buildable_area', 0)
            
            # Calculate monthly rental estimates based on buildable area
            monthly_rent_min = buildable_area * rental_prices['min']
            monthly_rent_avg = buildable_area * rental_prices['avg']
            monthly_rent_max = buildable_area * rental_prices['max']
            
            # Annual rental income
            annual_rent_min = monthly_rent_min * 12
            annual_rent_avg = monthly_rent_avg * 12
            annual_rent_max = monthly_rent_max * 12
            
            # Calculate investment metrics
            if total_investment > 0:
                # Rental yield (annual rent / total investment * 100)
                rental_yield = (annual_rent_avg / total_investment) * 100
                
                # Price-to-rent ratio (property price / annual rent)
                price_to_rent_ratio = total_investment / annual_rent_avg if annual_rent_avg > 0 else 0
                
                # Payback period in years
                payback_period = total_investment / annual_rent_avg if annual_rent_avg > 0 else 0
                
                # Cap rate (Net Operating Income / Property Value)
                # Assuming 25% expenses (maintenance, taxes, management)
                noi = annual_rent_avg * 0.75
                cap_rate = (noi / total_investment) * 100
            else:
                rental_yield = yields['avg']
                price_to_rent_ratio = 0
                payback_period = 0
                cap_rate = 0
            
            # Rental demand indicators
            demand_factors = []
            if location_type == 'urban':
                demand_factors = [
                    "High demand from professionals and students",
                    "Proximity to business centers and universities",
                    "Strong rental market with quick tenant turnover"
                ]
            elif location_type == 'suburban':
                demand_factors = [
                    "Growing demand from families",
                    "Good schools and amenities nearby",
                    "Balance of urban access and quality of life"
                ]
            else:  # rural
                demand_factors = [
                    "Vacation rental potential (Airbnb)",
                    "Rural tourism growth in Asturias",
                    "Weekend home rental opportunities"
                ]
            
            return {
                'location_type': location_type.capitalize(),
                'monthly_rent_min': round(monthly_rent_min),
                'monthly_rent_avg': round(monthly_rent_avg),
                'monthly_rent_max': round(monthly_rent_max),
                'annual_rent_min': round(annual_rent_min),
                'annual_rent_avg': round(annual_rent_avg),
                'annual_rent_max': round(annual_rent_max),
                'rental_yield': round(rental_yield, 1),
                'expected_yield_range': f"{yields['min']}-{yields['max']}%",
                'price_to_rent_ratio': round(price_to_rent_ratio, 1),
                'payback_period_years': round(payback_period, 1),
                'cap_rate': round(cap_rate, 1),
                'rental_price_per_m2': f"€{rental_prices['avg']}/m²/month",
                'demand_factors': demand_factors,
                'investment_rating': self._get_investment_rating(rental_yield, cap_rate),
                'market_comparison': f"Average rental yield in {location_type} Asturias: {yields['avg']}%"
            }
            
        except Exception as e:
            logger.error(f"Error calculating rental analysis: {str(e)}")
            return {
                'error': 'Unable to calculate rental analysis',
                'location_type': 'Unknown',
                'monthly_rent_avg': 0,
                'annual_rent_avg': 0,
                'rental_yield': 0
            }
    
    def _get_investment_rating(self, rental_yield: float, cap_rate: float) -> str:
        """
        Determine investment rating based on yield and cap rate
        """
        if rental_yield >= 6 and cap_rate >= 5:
            return "EXCELLENT - High returns expected"
        elif rental_yield >= 5 and cap_rate >= 4:
            return "GOOD - Above average returns"
        elif rental_yield >= 4 and cap_rate >= 3:
            return "MODERATE - Standard market returns"
        else:
            return "BELOW AVERAGE - Consider other options"
    
    def _get_default_market_trends(self) -> Dict:
        """Return default market trends for Asturias region"""
        return {
            'price_trend': 'STABLE',
            'annual_growth_rate': 3.5,
            'trend_period': '2024-2025',
            'trend_analysis': 'Asturias property market shows steady growth',
            'future_outlook': 'Expected 3-5% annual appreciation in line with regional averages',
            'market_factors': [
                'Stable tourism industry',
                'Growing interest in rural properties',
                'Infrastructure development in the region'
            ],
            'avg_price_per_m2': 50,
            'min_price_per_m2': 30,
            'max_price_per_m2': 150,
            'sample_size': 0
        }
    
    def get_enriched_data(self, land: Land) -> Dict:
        """
        Get comprehensive enriched data for a property including
        construction estimates, market analysis, and rental analysis
        """
        construction_data = self.calculate_construction_value(land)
        market_data = self.analyze_market_trends(land)
        rental_data = self.calculate_rental_analysis(land, construction_data)
        
        return {
            'construction_value_estimation': construction_data,
            'market_price_dynamics': market_data,
            'rental_market_analysis': rental_data
        }