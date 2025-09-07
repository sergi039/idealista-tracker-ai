#!/usr/bin/env python3
"""
Script to recalculate enrichment data for properties
This will update travel times for local amenities
"""

import os
import sys
from app import app, db
from models import Land
from services.enrichment_service import EnrichmentService

def recalculate_enrichment_for_lands(limit=5):
    """Recalculate enrichment data for top-scoring lands"""
    with app.app_context():
        try:
            # Initialize enrichment service
            enrichment_service = EnrichmentService()
            
            # Get top scoring lands that need recalculation
            lands = Land.query.filter(
                Land.score_total > 40,
                Land.location_lat.isnot(None),
                Land.location_lon.isnot(None)
            ).order_by(Land.score_total.desc()).limit(limit).all()
            
            print(f"Found {len(lands)} lands to recalculate")
            
            for land in lands:
                print(f"\nProcessing land {land.id}: {land.title[:50]}...")
                
                # Clear existing data to force recalculation
                land.infrastructure_extended = {}
                land.transport = {}
                land.services_quality = {}
                
                # Re-enrich with updated calculations
                enrichment_service._enrich_with_google_places(land)
                enrichment_service._enrich_with_osm_data(land)
                
                # Commit changes
                db.session.commit()
                print(f"✓ Updated enrichment data for land {land.id}")
                
                # Show sample of new data
                if land.infrastructure_extended:
                    print("  Infrastructure extended data:")
                    for key in ['cafe_distance', 'cafe_travel_time', 'hospital_distance', 'hospital_travel_time']:
                        if key in land.infrastructure_extended:
                            value = land.infrastructure_extended[key]
                            if 'distance' in key:
                                print(f"    {key}: {value/1000:.1f}km" if value else f"    {key}: N/A")
                            else:
                                print(f"    {key}: {value}min" if value else f"    {key}: N/A")
                
                if land.transport:
                    print("  Transport data:")
                    for key in ['airport_available', 'airport_distance', 'airport_travel_time']:
                        if key in land.transport:
                            value = land.transport[key]
                            if key == 'airport_available':
                                print(f"    {key}: {'Yes' if value else 'No'}")
                            elif 'distance' in key and value:
                                print(f"    {key}: {value/1000:.1f}km")
                            elif 'time' in key and value:
                                print(f"    {key}: {value}min")
            
            print(f"\n✅ Successfully recalculated enrichment for {len(lands)} lands")
            
        except Exception as e:
            print(f"❌ Error recalculating enrichment: {str(e)}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    recalculate_enrichment_for_lands(limit)