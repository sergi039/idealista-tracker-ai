"""
Description Processing Service
Enhances raw property descriptions using AI to create professional, structured content
"""

import logging
import json
import re
from typing import Dict, Optional, Any
from services.anthropic_service import get_anthropic_service

logger = logging.getLogger(__name__)

class DescriptionService:
    """Service for processing and enhancing property descriptions"""
    
    def __init__(self):
        self.anthropic_service = get_anthropic_service()
    
    def extract_key_data(self, raw_description: str) -> Dict[str, Any]:
        """
        Extract structured data from raw property description
        """
        if not raw_description:
            return {}
            
        # Basic regex patterns for common data
        price_pattern = r'(\d{1,3}(?:,\d{3})*(?:\.\d{3})*)\s*€'
        area_pattern = r'(\d{1,5}(?:,\d{3})*(?:\.\d{3})*)\s*m[²2]'
        discount_pattern = r'(?:dropped|reduced|bajado).*?(\d+)%'
        
        # Extract data using regex
        prices = re.findall(price_pattern, raw_description)
        areas = re.findall(area_pattern, raw_description)
        discounts = re.findall(discount_pattern, raw_description, re.IGNORECASE)
        
        # Parse extracted data
        extracted = {}
        
        if prices:
            # Remove commas and convert to numbers
            price_values = [int(p.replace(',', '').replace('.', '')) for p in prices]
            if len(price_values) >= 2:
                extracted['original_price'] = max(price_values)
                extracted['current_price'] = min(price_values)
            elif len(price_values) == 1:
                extracted['current_price'] = price_values[0]
        
        if areas:
            # Take the largest area mentioned (likely the main property area)
            area_values = [int(a.replace(',', '').replace('.', '')) for a in areas]
            extracted['area'] = max(area_values)
        
        if discounts:
            extracted['discount_percentage'] = int(discounts[0])
        
        return extracted
    
    def enhance_description(self, raw_description: str, property_data: Dict = None) -> Dict[str, Any]:
        """
        Use AI to create a professional, structured description
        """
        try:
            if not raw_description.strip():
                return {'enhanced_description': 'No description available', 'language': 'en'}
            
            # Extract basic data first
            extracted_data = self.extract_key_data(raw_description)
            
            # Prepare context for AI
            context = f"""
ORIGINAL DESCRIPTION: {raw_description}

EXTRACTED DATA: {extracted_data}
"""
            
            if property_data:
                context += f"""
PROPERTY METADATA:
- Price: €{property_data.get('price', 'N/A')}
- Area: {property_data.get('area', 'N/A')} m²
- Location: {property_data.get('municipality', 'N/A')}
- Type: {property_data.get('land_type', 'N/A')}
"""
            
            # Create AI prompt for description enhancement
            prompt = f"""{context}

TASK: Create a professional, structured property description in English based on the original content.

REQUIREMENTS:
1. Extract and highlight key information (price, area, location, features)
2. Remove duplicated information and email-style text
3. Create a clear, marketing-focused description
4. Use professional real estate language
5. Highlight any special offers or price reductions
6. Keep it concise but informative

Provide response in this JSON format:
{{
    "enhanced_description": "Professional English description here",
    "key_highlights": ["highlight 1", "highlight 2", "highlight 3"],
    "price_info": {{
        "current_price": current_price_if_found,
        "original_price": original_price_if_different,
        "discount": discount_percentage_if_applicable
    }},
    "property_type": "extracted_property_type",
    "location": "extracted_location",
    "confidence_score": 0.8
}}

Focus on creating professional, engaging content that would appeal to potential buyers."""

            # Call Claude API
            try:
                message = self.anthropic_service.client.messages.create(
                    model="claude-sonnet-4-20250514",  # Latest model
                    max_tokens=800,
                    temperature=0.3,
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
                
                # Parse JSON response
                enhanced_data = json.loads(response_text)
                enhanced_data['original_description'] = raw_description
                enhanced_data['processing_status'] = 'success'
                
                return enhanced_data
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse AI response as JSON: {e}")
                return {
                    'enhanced_description': self._create_fallback_description(raw_description, extracted_data),
                    'original_description': raw_description,
                    'processing_status': 'fallback',
                    'error': 'AI response parsing failed'
                }
            except Exception as e:
                logger.error(f"AI enhancement failed: {e}")
                return {
                    'enhanced_description': self._create_fallback_description(raw_description, extracted_data),
                    'original_description': raw_description,
                    'processing_status': 'fallback',
                    'error': str(e)
                }
                
        except Exception as e:
            logger.error(f"Description enhancement failed: {e}")
            return {
                'enhanced_description': raw_description,
                'original_description': raw_description,
                'processing_status': 'failed',
                'error': str(e)
            }
    
    def _create_fallback_description(self, raw_description: str, extracted_data: Dict) -> str:
        """
        Create a basic enhanced description when AI fails
        """
        # Clean up the raw description
        cleaned = raw_description.strip()
        
        # Remove common email prefixes
        cleaned = re.sub(r'^Hello\s+\w+,?\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^Hola\s+\w+,?\s*', '', cleaned, flags=re.IGNORECASE)
        
        # Remove duplicated price information
        if 'current_price' in extracted_data and 'original_price' in extracted_data:
            price_text = f"€{extracted_data['current_price']:,}"
            if extracted_data.get('discount_percentage'):
                price_text += f" (Reduced from €{extracted_data['original_price']:,} - {extracted_data['discount_percentage']}% off!)"
            cleaned = f"{price_text}\n\n{cleaned}"
        
        # Basic cleanup
        cleaned = re.sub(r'\s+', ' ', cleaned)  # Multiple spaces to single
        cleaned = cleaned.replace('...', '.')   # Clean up ellipsis
        
        return cleaned.strip()
    
    def get_description_variants(self, land_id: int) -> Dict[str, str]:
        """
        Get both enhanced and original descriptions for a property
        """
        try:
            from models import Land
            from app import db
            
            land = db.session.query(Land).filter_by(id=land_id).first()
            if not land:
                return {'error': 'Property not found'}
            
            # Check if enhanced description already exists
            if hasattr(land, 'enhanced_description') and land.enhanced_description:
                try:
                    enhanced_data = json.loads(land.enhanced_description)
                    return {
                        'enhanced': enhanced_data.get('enhanced_description', land.description),
                        'original': enhanced_data.get('original_description', land.description),
                        'status': enhanced_data.get('processing_status', 'unknown')
                    }
                except (json.JSONDecodeError, AttributeError):
                    pass
            
            # Return original description only
            return {
                'enhanced': land.description,
                'original': land.description,
                'status': 'not_processed'
            }
            
        except Exception as e:
            logger.error(f"Failed to get description variants for land {land_id}: {e}")
            return {'error': str(e)}