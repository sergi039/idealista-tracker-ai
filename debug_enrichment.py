#!/usr/bin/env python3
"""
Debug script to test enrichment services individually
"""

import os
import sys
import logging

# Add the app directory to the path
sys.path.insert(0, '.')

# Configure logging for debugging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_geocoding():
    """Test geocoding service with problematic municipalities"""
    from utils.geocoding import GeocodingService
    
    geocoding = GeocodingService()
    
    # Test cases from problematic data
    test_addresses = [
        "And",  # This is clearly bad data
        "Aronces, Cudillero",  # This should work
        "Cantabria, Spain",  # This is too generic but should still work
        "Caserio Cuesta Ayones, 22, San Claudio-Trubia-Las Caldas, Oviedo",  # This should be very specific
        "NORIEGA, Ribadedeva"  # This should work
    ]
    
    print("=== TESTING GEOCODING SERVICE ===")
    for address in test_addresses:
        print(f"\nTesting: '{address}'")
        result = geocoding.geocode_address(address)
        if result:
            print(f"  SUCCESS: {result['lat']}, {result['lng']}")
            print(f"  Formatted: {result['formatted_address']}")
        else:
            print(f"  FAILED: No coordinates found")

def test_travel_time():
    """Test travel time service"""
    from services.travel_time_service import TravelTimeService
    
    travel_service = TravelTimeService()
    
    # Test with a known good coordinate (one from our data that has travel times)
    test_coords = "42.5892012,-5.5633195"  # Property ID 33 that has working data
    
    print("\n=== TESTING TRAVEL TIME SERVICE ===")
    print(f"Testing from coordinates: {test_coords}")
    
    # Test Oviedo travel time
    oviedo_time = travel_service._get_travel_time(test_coords, travel_service.destinations['oviedo'])
    print(f"Oviedo travel time: {oviedo_time} minutes")
    
    # Test Gijon travel time
    gijon_time = travel_service._get_travel_time(test_coords, travel_service.destinations['gijon'])
    print(f"Gijon travel time: {gijon_time} minutes")
    
    # Test nearest beach
    beach_data = travel_service._find_nearest_beach(test_coords)
    print(f"Nearest beach: {beach_data}")

def test_claude_enhancement():
    """Test Claude description enhancement"""
    from services.description_service import DescriptionService
    
    desc_service = DescriptionService()
    
    # Test with a sample description
    test_description = "Terreno 2800 m² en Caserio Cuesta Ayones, 22, San Claudio-Trubia-Las Caldas, Oviedo (95k€)"
    
    print("\n=== TESTING CLAUDE DESCRIPTION ENHANCEMENT ===")
    print(f"Original: {test_description}")
    
    result = desc_service.enhance_description(test_description)
    print(f"Enhanced result: {result}")

def test_api_keys():
    """Test if API keys are accessible"""
    print("=== TESTING API KEY ACCESS ===")
    google_key = os.environ.get("Google_api")
    claude_key = os.environ.get("claude_key")
    
    print(f"Google API key exists: {bool(google_key)}")
    print(f"Claude API key exists: {bool(claude_key)}")
    
    if google_key:
        print(f"Google key length: {len(google_key)}")
    if claude_key:
        print(f"Claude key length: {len(claude_key)}")

if __name__ == "__main__":
    print("Starting enrichment service debugging...")
    
    # Test API keys first
    test_api_keys()
    
    # Test geocoding
    test_geocoding()
    
    # Test travel times
    test_travel_time()
    
    # Test Claude enhancement
    test_claude_enhancement()
    
    print("\nDebugging complete!")