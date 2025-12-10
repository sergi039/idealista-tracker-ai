#!/usr/bin/env python3
"""
Batch enrichment script for existing land records without coordinates or scoring
"""

import os
import sys
import logging
from datetime import datetime

# Add the current directory to the path so we can import our modules
sys.path.append('.')

from app import app, db
from models import Land
from services.enrichment_service import EnrichmentService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def batch_enrich_lands(limit=None):
    """Enrich existing lands that are missing coordinates or scoring"""
    
    with app.app_context():
        # Find lands needing enrichment
        query = Land.query.filter(
            db.or_(
                Land.location_lat.is_(None),
                Land.location_lon.is_(None),
                Land.score_total.is_(None)
            )
        )
        
        if limit:
            lands_to_enrich = query.limit(limit).all()
        else:
            lands_to_enrich = query.all()
        
        logger.info(f"Found {len(lands_to_enrich)} lands needing enrichment")
        
        if not lands_to_enrich:
            logger.info("No lands need enrichment")
            return 0
        
        enrichment_service = EnrichmentService()
        success_count = 0
        error_count = 0
        
        for i, land in enumerate(lands_to_enrich, 1):
            try:
                logger.info(f"Processing {i}/{len(lands_to_enrich)}: Land {land.id} - {land.title[:50]}...")
                
                # Skip if municipality is clearly bad
                if not land.municipality or land.municipality.lower() in ['and', 'cantabria']:
                    logger.warning(f"Skipping land {land.id} with bad municipality: '{land.municipality}'")
                    error_count += 1
                    continue
                
                success = enrichment_service.enrich_land(land.id)
                
                if success:
                    success_count += 1
                    logger.info(f"Successfully enriched land {land.id}")
                else:
                    error_count += 1
                    logger.warning(f"Failed to enrich land {land.id}")
                
                # Small delay to avoid overwhelming external APIs
                if i % 10 == 0:
                    logger.info(f"Processed {i} lands so far. Success: {success_count}, Errors: {error_count}")
                    
            except Exception as e:
                error_count += 1
                logger.error(f"Exception enriching land {land.id}: {str(e)}")
                continue
        
        logger.info(f"Batch enrichment completed. Success: {success_count}, Errors: {error_count}")
        return success_count

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Batch enrich land records")
    parser.add_argument("--limit", type=int, help="Limit number of records to process")
    parser.add_argument("--test", action="store_true", help="Test run with first 5 records")
    
    args = parser.parse_args()
    
    if args.test:
        print("Running test with 5 records...")
        result = batch_enrich_lands(limit=5)
    else:
        result = batch_enrich_lands(limit=args.limit)
    
    print(f"Enriched {result} land records successfully")