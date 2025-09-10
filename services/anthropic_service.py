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

logger = logging.getLogger(__name__)

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
    
    def analyze_property_structured(self, property_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Structured AI analysis for property cards with 5 analysis blocks
        
        Args:
            property_data: Dictionary containing comprehensive property information
            
        Returns:
            Structured analysis with 5 blocks or None if failed
        """
        try:
            # Prepare comprehensive property data
            property_text = self._format_comprehensive_data(property_data)
            
            # Create prompt for structured analysis
            prompt = f"""Analyze this Asturias real estate property and provide structured insights in ENGLISH:

{property_text}

Provide analysis in this EXACT JSON format (keep all text in English):
{{
    "price_analysis": {{
        "verdict": "FAIR_PRICE|OVERPRICED|UNDERPRICED",
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
    }}
}}

Keep all responses concise and in English. Focus on practical investment insights."""
            
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