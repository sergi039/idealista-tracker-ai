import re
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class EmailParser:
    def __init__(self):
        # Regex patterns for extracting data from Idealista emails
        self.patterns = {
            'price': [
                r'(\d{1,3}(?:,\d{3})*)\s*€',  # English format: 59,000 €
                r'(\d{1,3}(?:\.\d{3})*)\s*€',  # Spanish format: 59.000 €
                r'Price:?\s*(\d{1,3}(?:,\d{3})*)\s*€',  # English with label
                r'Precio:?\s*(\d{1,3}(?:\.\d{3})*)\s*€'  # Spanish with label
            ],
            'area': [
                r'(\d{1,3}(?:,\d{3})*)\s*m[²2]',  # English format: 1,373 m²
                r'(\d{1,3}(?:\.\d{3})*)\s*m[²2]',  # Spanish format: 1.373 m²
                r'(\d+)\s*m[²2]',  # Simple format: 1373 m²
                r'Superficie:?\s*(\d+(?:,\d+)?)\s*m[²2]'  # Spanish with label
            ],
            'url': [
                r'https?://www\.idealista\.com/[^\s]+',
                r'Ver anuncio:?\s*(https?://www\.idealista\.com/[^\s]+)'
            ],
            'municipality': [
                r'en\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ\s]+(?:[A-ZÁÉÍÓÚÑ][a-záéíóúñ\s]*)*)',
                r'Municipio:?\s*([A-ZÁÉÍÓÚÑ][a-záéíóúñ\s]+)'
            ]
        }
        
        # Land type classification (expanded for Spanish market)
        self.land_type_patterns = {
            'developed': [
                'urbano', 'desarrollado', 'urban', 'developed',
                'suelo urbano', 'terreno urbano', 'solar urbano',
                'consolidado', 'edificable'
            ],
            'buildable': [
                'urbanizable', 'buildable', 'para construir',
                'suelo urbanizable', 'apto para construcción',
                'solar', 'parcela', 'terreno', 'finca',
                'rustico', 'rústico', 'rural'
            ]
        }
    
    def parse_idealista_email(self, email_content: Dict) -> Optional[Dict]:
        """Parse Idealista email and extract property data"""
        try:
            subject = email_content.get('subject', '')
            body = email_content.get('body', '')
            
            # Combine subject and body for parsing
            full_text = f"{subject}\n{body}"
            
            # Extract basic information
            extracted_data = {
                'title': self._extract_title(subject),
                'price': self._extract_price(full_text),
                'area': self._extract_area(full_text),
                'url': self._extract_url(full_text),
                'municipality': self._extract_municipality(full_text),
                'description': self._clean_description(body),
                'land_type': self._classify_land_type(full_text),
                'legal_status': self._extract_legal_status(full_text)
            }
            
            # Return if we have essential data (relaxed land type requirement)
            if extracted_data['url'] or extracted_data['title'] or extracted_data['price']:
                # Set default land type if not detected
                if not extracted_data['land_type']:
                    extracted_data['land_type'] = 'buildable'  # Default to buildable
                    logger.info(f"No land type detected, defaulting to 'buildable'")
                
                logger.info(f"Successfully parsed email: {extracted_data['title'][:50] if extracted_data['title'] else 'No title'}...")
                return extracted_data
            else:
                logger.warning(f"Skipping email - missing essential data (URL, title, or price)")
                return None
                
        except Exception as e:
            logger.error(f"Failed to parse email: {str(e)}")
            return None
    
    def _extract_title(self, subject: str) -> str:
        """Extract property title from email subject"""
        # Clean up subject line
        title = re.sub(r'^(Re:|Fwd?:|Idealista:?)\s*', '', subject, flags=re.IGNORECASE)
        title = re.sub(r'\s+', ' ', title).strip()
        return title
    
    def _extract_price(self, text: str) -> Optional[float]:
        """Extract price from text"""
        for pattern in self.patterns['price']:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                price_str = match.group(1)
                # Remove both dots and commas used as thousand separators
                price_str = price_str.replace(',', '').replace('.', '')
                try:
                    return float(price_str)
                except ValueError:
                    continue
        return None
    
    def _extract_area(self, text: str) -> Optional[float]:
        """Extract area from text"""
        for pattern in self.patterns['area']:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                area_str = match.group(1)
                # Remove both dots and commas used as thousand separators
                area_str = area_str.replace(',', '').replace('.', '')
                try:
                    area = float(area_str)
                    # Validate area is reasonable (at least 100 m² for land)
                    if area >= 100:
                        return area
                except ValueError:
                    continue
        return None
    
    def _extract_url(self, text: str) -> Optional[str]:
        """Extract Idealista URL from text"""
        for pattern in self.patterns['url']:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                url = match.group(0) if len(match.groups()) == 0 else match.group(1)
                # Clean up URL
                url = url.strip()
                if url.startswith('http'):
                    return url
        return None
    
    def _extract_municipality(self, text: str) -> Optional[str]:
        """Extract municipality from text"""
        # First try to find location from "Land in [location]" pattern
        # But exclude "your search" patterns
        land_match = re.search(r'Land in ([^€\n]+?)(?:\s+\d+[,.]?\d*\s*€|\s+See \d+|\n)', text, re.IGNORECASE)
        if land_match:
            location = land_match.group(1).strip()
            # Skip if it contains "your search"
            if 'your search' not in location.lower():
                # Clean up the location
                location = re.sub(r'\s+', ' ', location)
                # Remove trailing numbers or commas
                location = re.sub(r',?\s*\d+\s*$', '', location)
                if location and len(location) > 2:
                    return location
        
        # Try to find municipality from other patterns
        for pattern in self.patterns['municipality']:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                municipality = match.group(1).strip()
                # Clean up municipality name
                municipality = re.sub(r'\s+', ' ', municipality)
                if municipality and len(municipality) > 2:
                    return municipality.title()
        
        return None
    
    def _classify_land_type(self, text: str) -> Optional[str]:
        """Classify land type based on text content"""
        text_lower = text.lower()
        
        # Check for developed land indicators
        for keyword in self.land_type_patterns['developed']:
            if keyword in text_lower:
                return 'developed'
        
        # Check for buildable land indicators
        for keyword in self.land_type_patterns['buildable']:
            if keyword in text_lower:
                return 'buildable'
        
        # If no clear indication, try to infer from other clues
        if any(word in text_lower for word in ['solar', 'parcela', 'terreno']):
            if any(word in text_lower for word in ['construir', 'edificar', 'vivienda']):
                return 'buildable'
        
        return None
    
    def _extract_legal_status(self, text: str) -> Optional[str]:
        """Extract legal status information"""
        text_lower = text.lower()
        
        legal_indicators = {
            'Developed': ['urbano consolidado', 'suelo urbano'],
            'Buildable': ['urbanizable', 'apto para construcción'],
            'Rustic': ['rústico', 'rustico', 'no urbanizable']
        }
        
        for status, keywords in legal_indicators.items():
            if any(keyword in text_lower for keyword in keywords):
                return status
        
        return None
    
    def _clean_description(self, body: str) -> str:
        """Clean and format email body for description"""
        # Remove CSS styles and scripts
        description = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.IGNORECASE)
        description = re.sub(r'<script[^>]*>.*?</script>', '', description, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove HTML comments
        description = re.sub(r'<!--.*?-->', '', description, flags=re.DOTALL)
        
        # Extract text from specific patterns in Idealista emails
        # Look for property details between common markers
        property_match = re.search(r'(Land|Detached house|Terreno|Solar|Parcela).*?€.*?m[²2].*?Contact', description, re.DOTALL | re.IGNORECASE)
        if property_match:
            description = property_match.group(0)
        
        # Remove all HTML tags
        description = re.sub(r'<[^>]+>', ' ', description)
        
        # Decode HTML entities
        description = description.replace('&nbsp;', ' ')
        description = description.replace('&amp;', '&')
        description = description.replace('&lt;', '<')
        description = description.replace('&gt;', '>')
        description = description.replace('&quot;', '"')
        description = description.replace('&aacute;', 'á')
        description = description.replace('&eacute;', 'é')
        description = description.replace('&iacute;', 'í')
        description = description.replace('&oacute;', 'ó')
        description = description.replace('&uacute;', 'ú')
        description = description.replace('&ntilde;', 'ñ')
        description = description.replace('&euro;', '€')
        description = description.replace('&sup2;', '²')
        description = description.replace('&#39;', "'")
        
        # Remove extra whitespace and clean up
        description = re.sub(r'\s+', ' ', description)
        description = re.sub(r'\s*\.\s*\.\.+', '...', description)
        
        # Try to extract meaningful content
        if 'Hello' in description:
            # Extract from "Hello" to end of property details
            hello_match = re.search(r'Hello.*?(?:Contact|See all listings|Does this listing)', description, re.DOTALL)
            if hello_match:
                description = hello_match.group(0)
        
        # Clean up common Idealista footer text
        description = re.sub(r'Does this listing match.*', '', description, flags=re.IGNORECASE)
        description = re.sub(r'From Your searches.*', '', description, flags=re.IGNORECASE)
        description = re.sub(r'With the idealista app.*', '', description, flags=re.IGNORECASE)
        description = re.sub(r'If you.re no longer interested.*', '', description, flags=re.IGNORECASE)
        
        # Final cleanup
        description = description.strip()
        
        # Limit length
        if len(description) > 1000:
            description = description[:1000] + '...'
        
        return description if description else "Property listing from Idealista"
