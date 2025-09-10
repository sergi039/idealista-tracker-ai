#!/usr/bin/env python3
"""
Test the enhanced enrichment pipeline fixes
"""

import os
import sys
sys.path.insert(0, '.')

import logging
logging.basicConfig(level=logging.INFO)

def test_enhanced_geocoding():
    """Test the enhanced geocoding with municipality cleaning and duplicate detection"""
    from services.enrichment_service import EnrichmentService
    
    print("=== TESTING ENHANCED GEOCODING ===")
    enrichment = EnrichmentService()
    
    # Create mock land objects with problematic municipalities
    class MockLand:
        def __init__(self, land_id, municipality):
            self.id = land_id
            self.municipality = municipality
    
    test_cases = [
        MockLand(999, "And"),  # Should be rejected as bad data
        MockLand(998, "Aronces, Cudillero"),  # Should work with specific location
        MockLand(997, "Cantabria, Spain"),  # Should be rejected as too generic
        MockLand(996, "NORIEGA, Ribadedeva"),  # Should work
        MockLand(995, "Caserio Cuesta Ayones, 22, San Claudio-Trubia-Las Caldas, Oviedo")  # Should work with precise address
    ]
    
    for test_land in test_cases:
        print(f"\nTesting municipality: '{test_land.municipality}'")
        
        # Test municipality cleaning
        cleaned = enrichment._clean_municipality(test_land.municipality)
        print(f"  Cleaned: '{cleaned}'")
        
        if cleaned:
            # Test if too generic
            too_generic = enrichment._is_too_generic(cleaned)
            print(f"  Too generic: {too_generic}")
            
            # Test regional fallbacks
            fallbacks = enrichment._get_regional_fallbacks(cleaned)
            print(f"  Regional fallbacks: {fallbacks}")
            
            # Test geocoding
            result = enrichment._geocode_with_accuracy(test_land)
            if result:
                print(f"  SUCCESS: {result['lat']}, {result['lng']} (accuracy: {result['accuracy']})")
            else:
                print(f"  FAILED: No coordinates found")
        else:
            print(f"  REJECTED: Invalid municipality data")

def test_fallback_travel_times():
    """Test the fallback travel time calculations"""
    from services.travel_time_service import TravelTimeService
    
    print("\n=== TESTING FALLBACK TRAVEL TIME CALCULATIONS ===")
    travel_service = TravelTimeService()
    
    # Test with a sample coordinate
    test_origin = "43.3636546,-4.5727598"  # Noriega, Ribadedeva
    
    test_destinations = [
        ('Oviedo', travel_service.destinations['oviedo']),
        ('Gijón', travel_service.destinations['gijon']),
        ('San Lorenzo Beach', 'Playa de San Lorenzo, Gijón, Spain'),
        ('Santander Airport', 'Santander Airport, Santander, Spain')
    ]
    
    for name, destination in test_destinations:
        print(f"\nTesting travel to {name}:")
        result = travel_service._get_travel_time_and_distance(test_origin, destination)
        if result:
            print(f"  SUCCESS: {result['time']} minutes, {result['distance']} km")
        else:
            print(f"  FAILED: Could not calculate travel time")
    
    # Test nearest beach functionality
    print(f"\nTesting nearest beach from {test_origin}:")
    beach_result = travel_service._find_nearest_beach(test_origin)
    if beach_result:
        print(f"  SUCCESS: {beach_result['name']} - {beach_result['time']} minutes")
    else:
        print(f"  FAILED: Could not find nearest beach")

def test_claude_enhancement():
    """Test Claude description enhancement"""
    from services.description_service import DescriptionService
    
    print("\n=== TESTING CLAUDE DESCRIPTION ENHANCEMENT ===")
    desc_service = DescriptionService()
    
    # Test with real property description from database
    test_descriptions = [
        "Terreno 2800 m² en Caserio Cuesta Ayones, 22, San Claudio-Trubia-Las Caldas, Oviedo (95k€)",
        "Terreno 1750 m² en Cantabria (100k€)",
        "Land in Lugar Castro, 4045, Parroquias Norte, Siero 36,000 €"
    ]
    
    for desc in test_descriptions:
        print(f"\nTesting description: '{desc}'")
        
        # Test data extraction
        extracted = desc_service.extract_key_data(desc)
        print(f"  Extracted data: {extracted}")
        
        # Test enhancement
        enhanced = desc_service.enhance_description(desc)
        print(f"  Enhancement status: {enhanced.get('processing_status', 'unknown')}")
        if enhanced.get('enhanced_description'):
            print(f"  Enhanced description: {enhanced['enhanced_description'][:100]}...")

def test_full_enrichment():
    """Test full enrichment on a sample property"""
    from services.enrichment_service import EnrichmentService
    from models import Land
    
    print("\n=== TESTING FULL ENRICHMENT PIPELINE ===")
    
    # Find a property with missing enrichment data
    try:
        # Look for properties with missing travel times
        properties_to_test = Land.query.filter(
            Land.travel_time_nearest_beach.is_(None),
            Land.location_lat.isnot(None),
            Land.location_lon.isnot(None)
        ).limit(2).all()
        
        if not properties_to_test:
            # If no properties with coordinates but missing travel times, try those with coordinates
            properties_to_test = Land.query.filter(
                Land.location_lat.isnot(None),
                Land.location_lon.isnot(None)
            ).limit(2).all()
        
        enrichment_service = EnrichmentService()
        
        for land in properties_to_test:
            print(f"\nTesting enrichment for land ID {land.id}:")
            print(f"  Title: {land.title}")
            print(f"  Municipality: {land.municipality}")
            print(f"  Current coordinates: {land.location_lat}, {land.location_lon}")
            print(f"  Current beach time: {land.travel_time_nearest_beach}")
            
            # Run enrichment
            success = enrichment_service.enrich_land(land.id)
            print(f"  Enrichment result: {'SUCCESS' if success else 'FAILED'}")
            
            # Check results
            from app import db
            db.session.refresh(land)
            print(f"  Updated beach time: {land.travel_time_nearest_beach}")
            print(f"  Updated Oviedo time: {land.travel_time_oviedo}")
            print(f"  Updated Gijón time: {land.travel_time_gijon}")
    
    except Exception as e:
        print(f"  ERROR: {str(e)}")

if __name__ == "__main__":
    print("Testing enhanced enrichment pipeline...")
    
    # Initialize app context
    from app import app
    with app.app_context():
        # Test individual components
        test_enhanced_geocoding()
        test_fallback_travel_times()
        test_claude_enhancement()
        
        # Test full pipeline
        test_full_enrichment()
    
    print("\n=== TESTING COMPLETE ===")