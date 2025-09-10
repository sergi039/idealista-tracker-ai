"""
Anthropic Claude API Service
Uses claude_key from secrets for authentication
"""

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
                if hasattr(content_block, 'text'):
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