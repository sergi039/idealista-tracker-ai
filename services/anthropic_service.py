"""
Anthropic Claude API Service
Uses claude_key from secrets for authentication
"""

import json
import os
import sys
import logging
from typing import Optional, Dict, Any, List
import anthropic
from anthropic import Anthropic
from sqlalchemy import and_, or_, func

logger = logging.getLogger(__name__)

# Import market analysis service for enriched data
try:
    from services.market_analysis_service import MarketAnalysisService
except ImportError:
    MarketAnalysisService = None
    logger.warning("MarketAnalysisService not available")

# IMPORTANT: Using claude_key from secrets for authentication
# The newest Anthropic model is "claude-3-5-sonnet-20241022", not older models
# Always prefer using the latest model for best performance
DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

class AnthropicService:
    """Service for interacting with Anthropic Claude API"""
    
    def __init__(self):
        """Initialize Anthropic client with API key from secrets"""
        # Get API key from environment variable (claude_key in secrets)
        self.api_key = os.environ.get('claude_key')
        
        if not self.api_key:
            logger.error("claude_key not found in environment variables")
            raise ValueError("claude_key environment variable must be set in secrets")
        
        try:
            self.client = Anthropic(api_key=self.api_key)
            logger.info("Anthropic client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {str(e)}")
            raise
    
    def analyze_property(self, property_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Analyze property data using Claude AI
        
        Args:
            property_data: Dictionary containing property information
            
        Returns:
            AI analysis result or None if failed
        """
        try:
            # Prepare property information for analysis
            property_text = self._format_property_data(property_data)
            
            # Create prompt for property analysis
            prompt = f"""Analyze the following real estate property and provide insights:

{property_text}

Please provide:
1. Investment potential assessment
2. Key advantages and disadvantages
3. Recommended improvements
4. Market positioning

Format your response in clear sections."""
            
            # Call Claude API
            message = self.client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=1024,
                temperature=0.7,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )
            
            # Extract response
            response_text = ""
            if message.content and len(message.content) > 0:
                content_block = message.content[0]
                if hasattr(content_block, 'text') and content_block.text:
                    response_text = content_block.text
            
            return {
                'analysis': response_text,
                'model': DEFAULT_MODEL,
                'status': 'success'
            }
            
        except Exception as e:
            logger.error(f"Failed to analyze property with Claude: {str(e)}")
            return {
                'analysis': None,
                'error': str(e),
                'status': 'failed'
            }
    
    def generate_property_summary(self, description: str) -> Optional[str]:
        """
        Generate a concise summary of property description
        
        Args:
            description: Full property description text
            
        Returns:
            Summarized text or None if failed
        """
        try:
            prompt = f"""Summarize the following property description in 2-3 sentences, 
            focusing on the most important features and investment potential:

{description}"""
            
            message = self.client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=256,
                temperature=0.5,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )
            
            if message.content and len(message.content) > 0:
                content_block = message.content[0]
                if hasattr(content_block, 'text'):
                    return content_block.text
            return None
            
        except Exception as e:
            logger.error(f"Failed to generate summary: {str(e)}")
            return None
    
    def find_similar_properties(self, current_property: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        """
        Find similar properties based on characteristics
        
        Args:
            current_property: Current property data
            limit: Maximum number of similar properties to return
            
        Returns:
            List of similar properties
        """
        try:
            # Import here to avoid circular imports
            from models import Land
            
            current_id = current_property.get('id')
            current_price = current_property.get('price')
            current_area = current_property.get('area')
            current_municipality = current_property.get('municipality')
            current_land_type = current_property.get('land_type')
            current_beach_time = current_property.get('travel_time_nearest_beach')
            
            # Debug logging
            logger.info(f"Finding similar properties for ID: {current_id} (type: {type(current_id)})")
            
            # Start with base query (exclude current property)
            query = Land.query.filter(Land.id != current_id)
            
            # Build similarity conditions
            similarity_conditions = []
            
            # Same land type (high priority)
            if current_land_type:
                similarity_conditions.append(Land.land_type == current_land_type)
            
            # Similar price range (±30%)
            if current_price and current_price > 0:
                price_min = float(current_price) * 0.7
                price_max = float(current_price) * 1.3
                similarity_conditions.append(
                    and_(Land.price >= price_min, Land.price <= price_max)
                )
            
            # Similar area (±40%)
            if current_area and current_area > 0:
                area_min = float(current_area) * 0.6
                area_max = float(current_area) * 1.4
                similarity_conditions.append(
                    and_(Land.area >= area_min, Land.area <= area_max)
                )
            
            # Same municipality (medium priority)
            if current_municipality:
                similarity_conditions.append(Land.municipality == current_municipality)
            
            # Similar beach access (±20 minutes)
            if current_beach_time:
                beach_min = max(1, current_beach_time - 20)
                beach_max = current_beach_time + 20
                similarity_conditions.append(
                    and_(
                        Land.travel_time_nearest_beach >= beach_min,
                        Land.travel_time_nearest_beach <= beach_max
                    )
                )
            
            # Apply conditions with OR (any match gives similarity)
            if similarity_conditions:
                query = query.filter(or_(*similarity_conditions))
            
            # Order by score (best properties first), then by created date
            query = query.order_by(
                Land.score_total.desc().nullslast(),
                Land.created_at.desc()
            )
            
            # Get similar properties
            similar_properties = query.limit(limit * 2).all()  # Get more to filter later
            
            # Convert to dictionaries with similarity scoring
            results = []
            for prop in similar_properties:
                if len(results) >= limit:
                    break
                
                # Double-check exclusion of current property
                if prop.id == current_id:
                    logger.warning(f"Found current property {current_id} in similar results - skipping")
                    continue
                    
                similarity_score = self._calculate_similarity_score(current_property, prop)
                if similarity_score > 0:  # Only include if there's some similarity
                    prop_dict = {
                        'id': prop.id,
                        'title': prop.title,
                        'price': float(prop.price) if prop.price else None,
                        'area': float(prop.area) if prop.area else None,
                        'municipality': prop.municipality,
                        'land_type': prop.land_type,
                        'score_total': float(prop.score_total) if prop.score_total else None,
                        'travel_time_nearest_beach': prop.travel_time_nearest_beach,
                        'url': prop.url,
                        'similarity_score': similarity_score
                    }
                    results.append(prop_dict)
                    logger.info(f"Added similar property ID: {prop.id} with score: {similarity_score:.2f}")
            
            # Sort by similarity score (highest first)
            results.sort(key=lambda x: x['similarity_score'], reverse=True)
            
            return results[:limit]
            
        except Exception as e:
            logger.error(f"Failed to find similar properties: {str(e)}")
            return []
    
    def _calculate_similarity_score(self, current: Dict[str, Any], other) -> float:
        """Calculate similarity score between two properties"""
        score = 0.0
        max_score = 0.0
        
        # Land type match (high weight)
        max_score += 3.0
        if current.get('land_type') == other.land_type:
            score += 3.0
        
        # Municipality match (medium weight)  
        max_score += 2.0
        if current.get('municipality') == other.municipality:
            score += 2.0
        
        # Price similarity (medium weight)
        max_score += 2.0
        current_price = current.get('price')
        other_price = float(other.price) if other.price else None
        if current_price and other_price and current_price > 0 and other_price > 0:
            price_ratio = min(current_price, other_price) / max(current_price, other_price)
            if price_ratio >= 0.7:  # Within 30% difference
                score += 2.0 * price_ratio
        
        # Area similarity (medium weight)
        max_score += 2.0
        current_area = current.get('area')
        other_area = float(other.area) if other.area else None
        if current_area and other_area and current_area > 0 and other_area > 0:
            area_ratio = min(current_area, other_area) / max(current_area, other_area)
            if area_ratio >= 0.6:  # Within 40% difference
                score += 2.0 * area_ratio
        
        # Beach time similarity (low weight)
        max_score += 1.0
        current_beach = current.get('travel_time_nearest_beach')
        other_beach = other.travel_time_nearest_beach
        if current_beach and other_beach:
            time_diff = abs(current_beach - other_beach)
            if time_diff <= 20:  # Within 20 minutes
                score += 1.0 * (1.0 - time_diff / 20.0)
        
        return (score / max_score) if max_score > 0 else 0.0
    
    def analyze_property_structured(self, property_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Structured AI analysis for property cards with 5 analysis blocks
        
        Args:
            property_data: Dictionary containing comprehensive property information
            
        Returns:
            Structured analysis with 5 blocks or None if failed
        """
        try:
            # Get enriched market data if available
            enriched_data = {}
            if MarketAnalysisService:
                try:
                    from models import Land
                    from app import db
                    market_service = MarketAnalysisService()
                    land = db.session.query(Land).filter_by(id=property_data.get('id')).first()
                    if land:
                        enriched_data = market_service.get_enriched_data(land)
                except Exception as e:
                    logger.warning(f"Could not get enriched data: {str(e)}")
            
            # Prepare comprehensive property data
            property_text = self._format_comprehensive_data(property_data)
            
            # Add construction and market data to the context
            if enriched_data.get('construction_value_estimation'):
                construction = enriched_data['construction_value_estimation']
                property_text += f"\n\nCONSTRUCTION ESTIMATES (Asturias 2024-2025):"
                property_text += f"\nBuildable area: {construction.get('buildable_area', 'N/A')}m² ({construction.get('buildability_ratio', 'N/A')} of land)"
                property_text += f"\nConstruction cost per m²: €{construction.get('value_per_m2', 1000)}"
                property_text += f"\nEstimated construction: €{construction.get('minimum_value', 0):,.0f} - €{construction.get('maximum_value', 0):,.0f}"
                property_text += f"\nTotal investment needed: €{construction.get('total_investment_min', 0):,.0f} - €{construction.get('total_investment_max', 0):,.0f}"
            
            if enriched_data.get('market_price_dynamics'):
                market = enriched_data['market_price_dynamics']
                property_text += f"\n\nMARKET DATA (Based on {market.get('sample_size', 0)} similar properties):"
                property_text += f"\nAverage price per m²: €{market.get('avg_price_per_m2', 50)}"
                property_text += f"\nPrice range: €{market.get('min_price_per_m2', 30)} - €{market.get('max_price_per_m2', 150)}/m²"
                property_text += f"\nCurrent trend: {market.get('price_trend', 'STABLE')}"
                property_text += f"\nAnnual growth: {market.get('annual_growth_rate', 3.5):.1f}%"
            
            if enriched_data.get('rental_market_analysis'):
                rental = enriched_data['rental_market_analysis']
                property_text += f"\n\nRENTAL MARKET ANALYSIS ({rental.get('location_type', 'Unknown')} area):"
                property_text += f"\nEstimated monthly rent: €{rental.get('monthly_rent_min', 0):,.0f} - €{rental.get('monthly_rent_max', 0):,.0f} (avg: €{rental.get('monthly_rent_avg', 0):,.0f})"
                property_text += f"\nAnnual rental income: €{rental.get('annual_rent_min', 0):,.0f} - €{rental.get('annual_rent_max', 0):,.0f}"
                property_text += f"\nRental yield: {rental.get('rental_yield', 0):.1f}% (expected range: {rental.get('expected_yield_range', 'N/A')})"
                property_text += f"\nPrice-to-rent ratio: {rental.get('price_to_rent_ratio', 0):.1f}"
                property_text += f"\nPayback period: {rental.get('payback_period_years', 0):.1f} years"
                property_text += f"\nCap rate: {rental.get('cap_rate', 0):.1f}%"
                property_text += f"\nInvestment rating: {rental.get('investment_rating', 'N/A')}"
            
            # Check for existing analysis for enrichment
            existing_analysis = property_data.get('existing_analysis')
            is_enrichment = existing_analysis is not None
            
            # Find similar properties for comparison
            similar_properties = self.find_similar_properties(property_data, limit=3)
            similar_text = ""
            if similar_properties:
                similar_text = "\n\nSimilar properties in our database:"
                for i, prop in enumerate(similar_properties, 1):
                    similar_text += f"\n{i}. {prop['title'][:80]}... - €{prop['price']:,.0f} - {prop['area']:.0f}m² - {prop['municipality']} - Score: {prop['score_total']:.1f}/100" if prop['price'] and prop['area'] and prop['score_total'] else f"\n{i}. {prop['title'][:80]}... - {prop['municipality']}"
            
            # Create prompt for structured analysis
            if is_enrichment:
                # Enrichment prompt - focus on missing/incomplete sections
                existing_sections = list(existing_analysis.keys()) if existing_analysis else []
                missing_sections = []
                incomplete_sections = []
                
                for section in ['rental_market_analysis', 'construction_value_estimation', 'market_price_dynamics']:
                    if section not in existing_sections:
                        missing_sections.append(section)
                    elif not existing_analysis.get(section):
                        incomplete_sections.append(section)
                
                enrichment_focus = missing_sections + incomplete_sections
                
                prompt = f"""ENRICHMENT TASK: You are enhancing an existing real estate analysis. Focus on completing missing or incomplete sections.

EXISTING ANALYSIS:
{existing_analysis}

PROPERTY DATA:
{property_text}{similar_text}

ENRICHMENT PRIORITY: Focus especially on these sections: {', '.join(enrichment_focus) if enrichment_focus else 'rental_market_analysis, market insights'}

Provide ONLY the missing or enhanced data in this EXACT JSON format (keep all text in English). Include ALL sections but focus enrichment on priority areas:"""
            else:
                # Fresh analysis prompt
                prompt = f"""Analyze this Asturias real estate property and provide structured insights in ENGLISH:

{property_text}{similar_text}

Provide analysis in this EXACT JSON format (keep all text in English):
{{
    "price_analysis": {{
        "verdict": "Fair Price|Overpriced|Underpriced",
        "summary": "Brief market comparison and price per m² analysis",
        "price_per_m2": estimated_market_price_per_m2,
        "recommendation": "Short recommendation about pricing"
    }},
    "investment_potential": {{
        "rating": "HIGH|MEDIUM|LOW",
        "forecast": "Growth forecast with timeframe",
        "key_drivers": ["main factor 1", "main factor 2", "main factor 3"],
        "risk_level": "LOW|MEDIUM|HIGH"
    }},
    "risks_analysis": {{
        "major_risks": ["significant risk 1", "significant risk 2"],
        "minor_issues": ["minor issue 1", "minor issue 2"],
        "advantages": ["advantage 1", "advantage 2", "advantage 3"],
        "mitigation": "How to address main risks"
    }},
    "development_ideas": {{
        "best_use": "Recommended development type",
        "building_size": "Recommended building size and type",
        "special_features": "Unique opportunities for this property",
        "estimated_cost": "Development cost estimate"
    }},
    "comparable_analysis": {{
        "market_position": "Position vs similar properties",
        "advantages_vs_similar": ["what makes this better"],
        "disadvantages_vs_similar": ["what makes this worse"],
        "price_comparison": "How price compares to similar properties"
    }},
    "similar_objects": {{
        "comparison_summary": "Brief comparison with similar properties from our database",
        "recommended_alternatives": ["ID:1 - Brief reason why this is similar", "ID:2 - Brief reason", "ID:3 - Brief reason"]
    }},
    "construction_value_estimation": {{
        "minimum_value": estimated_minimum_construction_value,
        "maximum_value": estimated_maximum_construction_value,
        "average_value": estimated_average_construction_value,
        "construction_type": "Modern house type recommended for this plot",
        "value_per_m2": estimated_value_per_m2_for_built_property,
        "total_investment": "Land price + construction cost estimate"
    }},
    "market_price_dynamics": {{
        "price_trend": "RISING|STABLE|DECLINING",
        "annual_growth_rate": estimated_annual_growth_percentage,
        "trend_period": "Time period for this trend (e.g., '2020-2025')",
        "trend_analysis": "Brief explanation of what drives the price trend in this area",
        "future_outlook": "1-3 year price forecast for similar properties",
        "market_factors": ["key factor 1 affecting prices", "key factor 2", "key factor 3"]
    }},
    "rental_market_analysis": {{
        "monthly_rent_min": minimum_monthly_rental,
        "monthly_rent_avg": average_monthly_rental,
        "monthly_rent_max": maximum_monthly_rental,
        "annual_rent_avg": average_annual_rental,
        "rental_yield": expected_rental_yield_percentage,
        "price_to_rent_ratio": price_to_annual_rent_ratio,
        "payback_period_years": years_to_recover_investment,
        "cap_rate": capitalization_rate_percentage,
        "investment_rating": "EXCELLENT|GOOD|MODERATE|BELOW AVERAGE",
        "demand_factors": ["rental demand factor 1", "factor 2", "factor 3"],
        "rental_strategy": "Recommended rental strategy (long-term, vacation, etc.)"
    }}
}}

IMPORTANT: Use the provided CONSTRUCTION ESTIMATES and MARKET DATA in your analysis. Base your construction_value_estimation and market_price_dynamics on the real data provided above, not on general estimates.

Keep all responses concise and in English. Focus on practical investment insights for Asturias real estate market."""
            
            # Call Claude API
            message = self.client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=1500,
                temperature=0.7,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )
            
            # Extract response
            response_text = ""
            if message.content and len(message.content) > 0:
                content_block = message.content[0]
                if hasattr(content_block, 'text') and content_block.text:
                    response_text = content_block.text
            
            # Try to parse JSON response
            try:
                analysis_data = json.loads(response_text)
                
                # Add actual similar properties data for display
                if similar_properties:
                    # Add similar properties data for frontend to use
                    analysis_data['similar_properties_data'] = similar_properties
                
                return {
                    'structured_analysis': analysis_data,
                    'model': DEFAULT_MODEL,
                    'status': 'success'
                }
            except json.JSONDecodeError:
                # If JSON parsing fails, return raw response
                return {
                    'raw_analysis': response_text,
                    'model': DEFAULT_MODEL,
                    'status': 'partial_success'
                }
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to analyze property structure with Claude: {error_msg}")
            
            # Parse specific API errors for better user messages
            if "529" in error_msg or "overloaded" in error_msg.lower():
                user_msg = "Claude AI service is temporarily overloaded. Please try again in a few minutes."
            elif "401" in error_msg or "unauthorized" in error_msg.lower():
                user_msg = "API authentication failed. Please check your API key configuration."
            elif "429" in error_msg or "rate limit" in error_msg.lower():
                user_msg = "Too many requests. Please wait a moment before trying again."
            elif "timeout" in error_msg.lower():
                user_msg = "Request timed out. Please try again."
            else:
                user_msg = "AI analysis service is temporarily unavailable. Please try again later."
            
            return {
                'error': user_msg,
                'technical_error': error_msg,
                'status': 'failed'
            }
    
    def _format_comprehensive_data(self, property_data: Dict[str, Any]) -> str:
        """Format property data for comprehensive AI analysis"""
        text_parts = []
        
        # Basic info
        text_parts.append(f"PROPERTY: {property_data.get('title', 'N/A')}")
        if property_data.get('price'):
            text_parts.append(f"PRICE: €{property_data['price']:,.0f}")
        if property_data.get('area'):
            text_parts.append(f"AREA: {property_data['area']:,.0f}m²")
            if property_data.get('price') and property_data.get('area'):
                price_per_m2 = property_data['price'] / property_data['area']
                text_parts.append(f"PRICE PER M²: €{price_per_m2:.0f}/m²")
        
        text_parts.append(f"LOCATION: {property_data.get('municipality', 'N/A')}")
        text_parts.append(f"TYPE: {property_data.get('land_type', 'N/A')}")
        text_parts.append(f"TOTAL SCORE: {property_data.get('score_total', 'N/A')}/100")
        
        # Travel times
        if property_data.get('travel_time_nearest_beach'):
            text_parts.append(f"BEACH ACCESS: {property_data['travel_time_nearest_beach']} min to {property_data.get('nearest_beach_name', 'beach')}")
        if property_data.get('travel_time_oviedo'):
            text_parts.append(f"OVIEDO: {property_data['travel_time_oviedo']} min")
        if property_data.get('travel_time_gijon'):
            text_parts.append(f"GIJÓN: {property_data['travel_time_gijon']} min")
        if property_data.get('travel_time_airport'):
            text_parts.append(f"AIRPORT: {property_data['travel_time_airport']} min")
        
        # Infrastructure
        if property_data.get('infrastructure_basic'):
            infra = property_data['infrastructure_basic']
            infra_items = []
            for key, value in infra.items():
                if value:
                    infra_items.append(key)
            if infra_items:
                text_parts.append(f"INFRASTRUCTURE: {', '.join(infra_items)}")
        
        # Description
        if property_data.get('description'):
            text_parts.append(f"DESCRIPTION: {property_data['description'][:500]}...")
        
        return "\n".join(text_parts)

    def score_property_description(self, description: str) -> Optional[float]:
        """
        Generate an AI-based quality score for property description
        
        Args:
            description: Property description text
            
        Returns:
            Score from 0-100 or None if failed
        """
        try:
            prompt = f"""Rate the following property on a scale of 0-100 based on its description.
            Consider: location quality, infrastructure, investment potential, and description clarity.
            
Property description:
{description}

Respond with ONLY a number between 0 and 100, nothing else."""
            
            message = self.client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=10,
                temperature=0.3,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )
            
            response = ""
            if message.content and len(message.content) > 0:
                content_block = message.content[0]
                if hasattr(content_block, 'text'):
                    response = content_block.text
            # Extract numeric score
            score = float(response.strip())
            return min(100, max(0, score))  # Ensure within bounds
            
        except Exception as e:
            logger.error(f"Failed to score property: {str(e)}")
            return None
    
    def _format_property_data(self, property_data: Dict[str, Any]) -> str:
        """Format property data for AI analysis"""
        lines = []
        
        if 'title' in property_data:
            lines.append(f"Title: {property_data['title']}")
        if 'price' in property_data:
            lines.append(f"Price: €{property_data['price']:,.0f}")
        if 'area' in property_data:
            lines.append(f"Area: {property_data['area']:,.0f} m²")
        if 'municipality' in property_data:
            lines.append(f"Location: {property_data['municipality']}")
        if 'land_type' in property_data:
            lines.append(f"Type: {property_data['land_type']}")
        if 'score_total' in property_data:
            lines.append(f"Current Score: {property_data['score_total']:.1f}/100")
        if 'description' in property_data:
            lines.append(f"\nDescription:\n{property_data['description']}")
        
        return '\n'.join(lines)

# Singleton instance
_anthropic_service = None

def get_anthropic_service():
    """Get or create Anthropic service instance"""
    global _anthropic_service
    if _anthropic_service is None:
        _anthropic_service = AnthropicService()
    return _anthropic_service