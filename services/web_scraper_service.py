import logging
import requests
import trafilatura
from urllib.parse import urlparse
from typing import Dict, Optional, List
import re
import time

logger = logging.getLogger(__name__)

class WebScraperService:
    def __init__(self):
        # SSRF Protection: Only allow these domains
        self.allowed_domains = [
            'www.idealista.com',
            'idealista.com',
            'www.idealista.es', 
            'idealista.es'
        ]
        
        # Request settings
        self.timeout = 30
        self.max_retries = 3
        self.retry_delay = 2
        
        # User agent to appear as regular browser
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
    
    def _validate_url(self, url: str) -> bool:
        """Validate URL to prevent SSRF attacks"""
        try:
            parsed = urlparse(url)
            
            # Must be HTTP or HTTPS
            if parsed.scheme not in ['http', 'https']:
                logger.warning(f"Invalid scheme in URL: {url}")
                return False
                
            # Must be from allowed domains
            domain = parsed.netloc.lower()
            if not any(domain == allowed or domain.endswith('.' + allowed) 
                      for allowed in self.allowed_domains):
                logger.warning(f"Domain not allowed: {domain}")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"URL validation error: {e}")
            return False
    
    def _fetch_html(self, url: str) -> Optional[str]:
        """Safely fetch HTML content from URL with retries and secure redirect handling"""
        if not self._validate_url(url):
            return None
            
        for attempt in range(self.max_retries):
            try:
                logger.info(f"Fetching HTML from {url} (attempt {attempt + 1})")
                
                # Secure fetch with manual redirect handling
                final_response = self._fetch_with_secure_redirects(url)
                
                if final_response and final_response.text:
                    return final_response.text
                else:
                    logger.warning(f"Empty response or secure redirect failed for {url}")
                    
            except requests.exceptions.RequestException as e:
                logger.warning(f"HTTP request failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    
        logger.error(f"Failed to fetch HTML after {self.max_retries} attempts")
        return None
    
    def _fetch_with_secure_redirects(self, url: str, max_redirects: int = 5) -> Optional[requests.Response]:
        """Fetch URL with manual redirect handling to prevent SSRF attacks"""
        current_url = url
        redirects_followed = 0
        
        while redirects_followed < max_redirects:
            # Validate current URL before each request
            if not self._validate_url(current_url):
                logger.warning(f"Redirect target failed validation: {current_url}")
                return None
                
            try:
                response = requests.get(
                    current_url,
                    headers=self.headers,
                    timeout=self.timeout,
                    allow_redirects=False  # Disable automatic redirects
                )
                
                # Check if this is a redirect response
                if response.status_code in [301, 302, 303, 307, 308]:
                    redirect_url = response.headers.get('Location')
                    if not redirect_url:
                        logger.warning(f"Redirect response without Location header: {current_url}")
                        return None
                    
                    # Convert relative URLs to absolute
                    if redirect_url.startswith('/'):
                        from urllib.parse import urljoin
                        redirect_url = urljoin(current_url, redirect_url)
                    
                    logger.info(f"Following redirect from {current_url} to {redirect_url}")
                    current_url = redirect_url
                    redirects_followed += 1
                    continue
                    
                # Not a redirect, check if successful
                response.raise_for_status()
                return response
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request failed for {current_url}: {e}")
                return None
        
        logger.warning(f"Too many redirects ({max_redirects}) from {url}")
        return None
    
    def parse_idealista_html(self, url: str) -> Dict:
        """Parse Idealista property page and extract structured data"""
        # Initialize result with contract structure
        result = {
            'basic_features': {
                'total_area_sqm': None,
                'buildable_area_sqm': None,
                'main_road_access': None
            },
            'land_type': {
                'buildable_land': None,
                'certifications': []
            },
            'amenities': {
                'water_supply': None,
                'electricity': None, 
                'sewer_system': None,
                'street_lighting': None
            },
            'meta': {
                'status': 'error',
                'last_fetched_at': None,
                'source_url': url,
                'extraction_method': 'trafilatura'
            }
        }
        
        try:
            
            # Fetch HTML content
            html_content = self._fetch_html(url)
            if not html_content:
                result['meta']['status'] = 'fetch_failed'
                return result
            
            # Extract main content using trafilatura  
            extracted_text = trafilatura.extract(html_content, include_comments=False)
            if not extracted_text:
                logger.warning(f"No content extracted from {url}")
                result['meta']['status'] = 'no_content'
                return result
            
            # Parse structured data from extracted text
            self._parse_basic_features(extracted_text, result)
            self._parse_land_type(extracted_text, result)
            self._parse_amenities(extracted_text, result)
            
            # Success
            result['meta']['status'] = 'ok'
            result['meta']['last_fetched_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            
            logger.info(f"Successfully parsed Idealista data from {url}")
            return result
            
        except Exception as e:
            logger.error(f"Error parsing Idealista HTML: {e}")
            result['meta']['status'] = 'parse_error'
            return result
    
    def _parse_basic_features(self, content: str, result: Dict) -> None:
        """Extract basic property features from content"""
        try:
            # Total area patterns (m², m2, metros cuadrados)
            area_patterns = [
                r'(\d{1,4}(?:[.,]\d{1,3})?)\s*m[²2]',
                r'(\d{1,4}(?:[.,]\d{1,3})?)\s*metros?\s*cuadrados?',
                r'superficie:?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*m[²2]',
                r'parcela:?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*m[²2]'
            ]
            
            for pattern in area_patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    area_str = match.group(1).replace(',', '.')
                    result['basic_features']['total_area_sqm'] = float(area_str)
                    break
            
            # Buildable area patterns
            buildable_patterns = [
                r'edificabilidad:?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*m[²2]',
                r'construible:?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*m[²2]',
                r'superficie\s+construible:?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*m[²2]'
            ]
            
            for pattern in buildable_patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    area_str = match.group(1).replace(',', '.')
                    result['basic_features']['buildable_area_sqm'] = float(area_str)
                    break
            
            # Main road access indicators
            road_access_indicators = [
                'carretera principal', 'acceso carretera', 'frente a carretera',
                'junto a carretera', 'carretera nacional', 'autovia', 'autopista',
                'road access', 'main road', 'highway access'
            ]
            
            content_lower = content.lower()
            for indicator in road_access_indicators:
                if indicator in content_lower:
                    result['basic_features']['main_road_access'] = True
                    break
            else:
                # Check for negative indicators
                negative_indicators = ['sin acceso', 'no access', 'camino privado', 'private road']
                for indicator in negative_indicators:
                    if indicator in content_lower:
                        result['basic_features']['main_road_access'] = False
                        break
                        
        except Exception as e:
            logger.error(f"Error parsing basic features: {e}")
    
    def _parse_land_type(self, content: str, result: Dict) -> None:
        """Extract land type information from content"""
        try:
            content_lower = content.lower()
            
            # Buildable land indicators
            buildable_indicators = [
                'suelo urbano', 'urbanizable', 'edificable', 'solar',
                'urban land', 'buildable', 'construction land'
            ]
            
            non_buildable_indicators = [
                'suelo rústico', 'no urbanizable', 'protegido',
                'rustic land', 'non-buildable', 'protected land'
            ]
            
            # Check for buildable status
            for indicator in buildable_indicators:
                if indicator in content_lower:
                    result['land_type']['buildable_land'] = True
                    break
            else:
                for indicator in non_buildable_indicators:
                    if indicator in content_lower:
                        result['land_type']['buildable_land'] = False
                        break
            
            # Extract certifications/classifications
            cert_patterns = [
                r'clasificación:?\s*([^.\n]+)',
                r'calificación:?\s*([^.\n]+)',
                r'uso:?\s*([^.\n]+)',
                r'zonificación:?\s*([^.\n]+)'
            ]
            
            for pattern in cert_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    cert = match.strip().lower()
                    if cert and len(cert) > 3:  # Filter short meaningless matches
                        result['land_type']['certifications'].append(cert)
                        
        except Exception as e:
            logger.error(f"Error parsing land type: {e}")
    
    def _parse_amenities(self, content: str, result: Dict) -> None:
        """Extract amenities and utilities information from content"""
        try:
            content_lower = content.lower()
            
            # Water supply
            water_positive = ['agua corriente', 'suministro agua', 'agua red', 'water supply', 'mains water']
            water_negative = ['sin agua', 'no water', 'pozo', 'well water']
            
            for indicator in water_positive:
                if indicator in content_lower:
                    result['amenities']['water_supply'] = True
                    break
            else:
                for indicator in water_negative:
                    if indicator in content_lower:
                        result['amenities']['water_supply'] = False
                        break
            
            # Electricity
            electricity_positive = ['electricidad', 'luz', 'suministro eléctrico', 'electricity', 'power supply']
            electricity_negative = ['sin luz', 'sin electricidad', 'no electricity', 'no power']
            
            for indicator in electricity_positive:
                if indicator in content_lower:
                    result['amenities']['electricity'] = True
                    break
            else:
                for indicator in electricity_negative:
                    if indicator in content_lower:
                        result['amenities']['electricity'] = False
                        break
            
            # Sewer system
            sewer_positive = ['alcantarillado', 'saneamiento', 'red cloacal', 'sewer', 'sewerage']
            sewer_negative = ['sin alcantarillado', 'fosa séptica', 'no sewer', 'septic tank']
            
            for indicator in sewer_positive:
                if indicator in content_lower:
                    result['amenities']['sewer_system'] = True
                    break
            else:
                for indicator in sewer_negative:
                    if indicator in content_lower:
                        result['amenities']['sewer_system'] = False
                        break
            
            # Street lighting
            lighting_positive = ['alumbrado público', 'farolas', 'iluminación', 'street lighting', 'street lights']
            lighting_negative = ['sin alumbrado', 'no lighting', 'oscuro']
            
            for indicator in lighting_positive:
                if indicator in content_lower:
                    result['amenities']['street_lighting'] = True
                    break
            else:
                for indicator in lighting_negative:
                    if indicator in content_lower:
                        result['amenities']['street_lighting'] = False
                        break
                        
        except Exception as e:
            logger.error(f"Error parsing amenities: {e}")
    
    def scrape_property_details(self, idealista_url: str) -> Optional[Dict]:
        """Main method to scrape property details from Idealista URL"""
        if not idealista_url or not self._validate_url(idealista_url):
            logger.warning(f"Invalid Idealista URL: {idealista_url}")
            return None
            
        return self.parse_idealista_html(idealista_url)