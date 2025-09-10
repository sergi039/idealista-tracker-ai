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
    
    def calculate_construction_value(self, land: Land) -> Dict:
        """
        Calculate construction value estimates based on land characteristics
        """
        try:
            area = float(land.area) if land.area else 0
            land_type = land.land_type or 'default'
            score = float(land.score_total) if land.score_total else 50
            
            # Determine buildable area based on land type
            buildability_ratio = self.BUILDABILITY_RATIOS.get(land_type, self.BUILDABILITY_RATIOS['default'])
            buildable_area = area * buildability_ratio
            
            # Adjust construction type based on land score
            if score >= 70:
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
                'value_per_m2': costs['avg'],
                'total_investment_min': round(total_investment_min),
                'total_investment_avg': round(total_investment_avg),
                'total_investment_max': round(total_investment_max),
                'buildability_ratio': f"{buildability_ratio * 100:.0f}%"
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
                if land.score_total and land.score_total > 70:
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
        construction estimates and market analysis
        """
        construction_data = self.calculate_construction_value(land)
        market_data = self.analyze_market_trends(land)
        
        return {
            'construction_value_estimation': construction_data,
            'market_price_dynamics': market_data
        }