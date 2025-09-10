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
                r'(?:en|in)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ\s,\-]+(?:[A-ZÁÉÍÓÚÑ][a-záéíóúñ\s,\-]*)*)',
                r'Municipio:?\s*([A-ZÁÉÍÓÚÑ][a-záéíóúñ\s,\-]+)',
                r'([A-ZÁÉÍÓÚÑ][a-záéíóúñ\s,\-]+),\s*(?:Asturias|Cantabria)'
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
                'title': self._extract_title(full_text),
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
    
    def _extract_title(self, text: str) -> str:
        """Extract property title from email content"""
        # Try to extract real property title from HTML content
        title_patterns = [
            # Look for property descriptions in quotes or specific HTML structures
            r'<strong[^>]*>([^<]+(?:m²|m2)[^<]*)</strong>',  # Bold text with area
            r'<h[1-6][^>]*>([^<]+(?:terreno|finca|parcela|solar)[^<]*)</h[1-6]>',  # Headers with land keywords
            r'<td[^>]*>([^<]*(?:terreno|finca|parcela|solar)[^<]{10,50})</td>',  # Table cells with descriptions
            r'(?:Terreno|Finca|Parcela|Solar)\s+[^\.]{10,80}',  # Land descriptions
            r'Land\s+[^\.]{10,80}',  # English land descriptions
            r'Plot\s+[^\.]{10,80}',  # Plot descriptions
            # Look for text after price/area information
            r'€[^a-zA-Z]*([A-Z][^€\n]{15,80})',  # Text after price
            r'(\d+,?\d*\s*m²[^€\n]{5,60})',  # Area followed by description
            r'>([^<>]{20,80}(?:terreno|finca|parcela|solar|plot|land)[^<>]{0,20})<',  # HTML content with keywords
        ]
        
        for pattern in title_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
            for match in matches:
                title = match.strip()
                # Clean up HTML tags, entities and extra whitespace
                title = self._clean_html(title)
                title = re.sub(r'\s+', ' ', title)
                title = title.strip()
                
                # Validate the title (should be descriptive, not too short/generic)
                if (len(title) >= 15 and 
                    not any(skip in title.lower() for skip in ['your search', 'cantabria land', 'new plot', 'idealista']) and
                    any(keyword in title.lower() for keyword in ['terreno', 'finca', 'parcela', 'solar', 'plot', 'land', 'm²', 'm2'])):
                    return title[:100]  # Limit length
        
        # If no specific property title found, create a descriptive one from available info
        # Try to extract location info
        location_match = re.search(r'([A-ZÁÉÍÓÚ][a-záéíóúñ\s-]+)\s*,\s*(?:Cantabria|Asturias)', text, re.IGNORECASE)
        if location_match:
            location = location_match.group(1).strip()
            return f"Terreno en {location}"
        
        # Fallback to area-based title if available
        area_match = re.search(r'(\d{1,3}(?:[,\.]\d{3})*)\s*m[²2]', text, re.IGNORECASE)
        if area_match:
            area = area_match.group(1)
            return f"Terreno de {area} m²"
        
        # Last resort: generic but better than email subject
        return "Terreno en Cantabria"
    
    def _clean_html(self, text: str) -> str:
        """Remove HTML tags and clean up the text"""
        if not text:
            return ""
        
        # Remove complete HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Remove incomplete/broken HTML tags (starting with < but without closing >)
        text = re.sub(r'<[^<]*$', '', text)
        # Remove HTML entities
        text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)
        # Remove extra whitespace and normalize
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
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
        """Extract Idealista URL from text - prioritize property links over logo links"""
        # First try to find property-specific URL (with /inmueble/)
        property_pattern = r'https?://www\.idealista\.com/[a-z]+/inmueble/\d+[^"\s]*'
        property_match = re.search(property_pattern, text, re.IGNORECASE)
        if property_match:
            url = property_match.group(0).strip()
            # Remove trailing quotes if present
            url = url.rstrip('"\'')
            return url
        
        # Fallback to general patterns (avoid logo links)
        for pattern in self.patterns['url']:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                url = match.group(0) if len(match.groups()) == 0 else match.group(1)
                url = url.strip()
                # Skip logo links
                if 'logo' not in url and url.startswith('http'):
                    return url
        return None
    
    def _extract_municipality(self, text: str) -> Optional[str]:
        """Extract municipality from text"""
        logger.debug(f"Extracting municipality from text: {text[:200]}...")
        
        # Normalize the text first - fix encoding issues
        normalized_text = self._normalize_email_text(text)
        logger.debug(f"Normalized text: {normalized_text[:200]}...")
        
        # First try to find location from "Land in [location]" pattern with improved regex
        # Use lookahead instead of literal euro symbol to handle different encodings
        land_pattern = r'Land in\s+(.+?)(?=\s+\d{1,3}(?:[.,]\d{3})*(?:\s*[€]|\s*EUR|\s*&euro;|\s*â‚¬)|\s+See\s+\d+|[\r\n]|$)'
        land_match = re.search(land_pattern, normalized_text, re.IGNORECASE)
        if land_match:
            location = land_match.group(1).strip()
            logger.debug(f"Found 'Land in' match: '{location}'")
            # Skip if it contains "your search"
            if 'your search' not in location.lower():
                # Clean up the location - remove trailing commas/numbers
                location = re.sub(r'[,\s]*(\d{1,3}(?:[.,]\d{3})*)$', '', location)
                location = re.sub(r'\s+', ' ', location).strip()
                if location and len(location) > 2:
                    logger.debug(f"Extracted municipality from 'Land in': '{location}'")
                    return location
        
        # Try to find municipality from other patterns with hardened fallbacks
        for i, pattern in enumerate(self.patterns['municipality']):
            match = re.search(pattern, normalized_text, re.IGNORECASE)
            if match:
                municipality = match.group(1).strip()
                logger.debug(f"Pattern {i} matched: '{municipality}'")
                
                # Apply hardened validation
                if self._is_valid_municipality(municipality):
                    logger.debug(f"Extracted municipality from pattern {i}: '{municipality.title()}'")
                    return municipality.title()
                else:
                    logger.debug(f"Rejected municipality '{municipality}' - failed validation")
        
        logger.debug("No municipality found")
        return None
    
    def _normalize_email_text(self, text: str) -> str:
        """Normalize email text by fixing common encoding issues"""
        # Convert non-breaking space to regular space
        text = text.replace('\xa0', ' ')
        
        # Normalize euro symbols to standard euro
        text = text.replace('&euro;', '€')
        text = text.replace('â‚¬', '€')
        text = text.replace('&nbsp;', ' ')
        
        # Basic HTML entity cleanup
        import html
        text = html.unescape(text)
        
        return text
    
    def _is_valid_municipality(self, municipality: str) -> bool:
        """Validate if a municipality name is legitimate"""
        if not municipality or len(municipality) <= 2:
            return False
        
        # Reject if contains digits
        if re.search(r'\d', municipality):
            return False
        
        # Define stopwords (common Spanish/English words that aren't locations)
        stopwords = {'and', 'en', 'de', 'del', 'la', 'el', 'por', 'con', 'y', 'e', 'with', 'for', 'in', 'of', 'the'}
        
        # Check if first word is a stopword
        first_word = municipality.split()[0].lower()
        if first_word in stopwords:
            return False
        
        # Require either:
        # a) Contains a comma (e.g., 'Corias, Pravia')
        # b) Ends with known region
        # c) Contains at least two meaningful tokens
        if (',' in municipality or 
            re.search(r'\b(?:Asturias|Cantabria|Spain)\b', municipality, re.IGNORECASE) or
            len(municipality.split()) >= 2):
            return True
        
        # Single word must be a proper location name (capitalized, reasonable length)
        if (municipality.istitle() and 
            3 <= len(municipality) <= 30 and
            municipality.isalpha()):
            return True
        
        return False
    
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
        
        # Remove all HTML tags including links
        description = re.sub(r'<a\s+[^>]*href[^>]*>.*?</a>', '', description, flags=re.DOTALL | re.IGNORECASE)
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
            # Extract from "Hello" to end of property details - be more specific about end markers
            hello_match = re.search(r'Hello.*?(?:Contact us|See all listings from|From Your searches|With the idealista app)', description, re.DOTALL)
            if hello_match:
                description = hello_match.group(0)
            else:
                # If no clear end marker found, take all text after "Hello" but limit length
                hello_start = description.find('Hello')
                if hello_start >= 0:
                    description = description[hello_start:]
        
        # Clean up common Idealista footer text - be more flexible with "Does this listing" patterns
        description = re.sub(r'Does this listing.*', '', description, flags=re.IGNORECASE | re.DOTALL)
        description = re.sub(r'From Your searches.*', '', description, flags=re.IGNORECASE)
        description = re.sub(r'With the idealista app.*', '', description, flags=re.IGNORECASE)
        description = re.sub(r'If you.re no longer interested.*', '', description, flags=re.IGNORECASE)
        
        # Final cleanup
        description = description.strip()
        
        # Limit length
        if len(description) > 1000:
            description = description[:1000] + '...'
        
        return description if description else "Property listing from Idealista"
