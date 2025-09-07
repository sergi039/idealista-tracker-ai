#!/usr/bin/env python3
"""
Fix areas for lands that were incorrectly parsed
"""

import re
import logging
from app import app, db
from models import Land

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def extract_area_from_description(description: str) -> float:
    """Extract area from description text"""
    if not description:
        return None
    
    # Patterns for area extraction
    patterns = [
        r'(\d{1,3}(?:,\d{3})*)\s*m[²2]',  # English format: 1,373 m²
        r'(\d{1,3}(?:\.\d{3})*)\s*m[²2]',  # Spanish format: 1.373 m²
        r'(\d+)\s*m[²2]',  # Simple format: 1373 m²
    ]
    
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            area_str = match.group(1)
            # Remove commas and dots used as thousand separators
            area_str = area_str.replace(',', '').replace('.', '')
            try:
                area = float(area_str)
                # Validate area is reasonable (at least 100 m² for land)
                if area >= 100:
                    return area
            except ValueError:
                continue
    
    return None

def fix_land_areas():
    """Fix areas for lands with incorrect values"""
    with app.app_context():
        try:
            # Get lands with suspicious areas (< 100 m²)
            suspicious_lands = Land.query.filter(
                (Land.area == None) | (Land.area < 100)
            ).all()
            
            logger.info(f"Found {len(suspicious_lands)} lands with suspicious areas")
            
            fixed_count = 0
            for land in suspicious_lands:
                old_area = land.area
                
                # Try to extract area from description
                new_area = extract_area_from_description(land.description)
                
                if new_area and new_area != old_area:
                    land.area = new_area
                    fixed_count += 1
                    logger.info(f"Fixed land {land.id}: '{land.title[:40]}...' - Area: {old_area} -> {new_area} m²")
            
            db.session.commit()
            logger.info(f"Successfully fixed {fixed_count} land areas")
            
            # Show statistics
            lands_with_area = Land.query.filter(Land.area > 0).count()
            total_lands = Land.query.count()
            logger.info(f"Now {lands_with_area}/{total_lands} lands have valid areas")
            
            # Show price per m² statistics
            lands_with_both = Land.query.filter(Land.price > 0, Land.area > 100).all()
            if lands_with_both:
                price_per_sqm_list = [float(land.price / land.area) for land in lands_with_both]
                avg_price_per_sqm = sum(price_per_sqm_list) / len(price_per_sqm_list)
                logger.info(f"Average price per m²: {avg_price_per_sqm:.2f} €/m²")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to fix areas: {str(e)}")
            db.session.rollback()
            return False

def main():
    """Main function to fix land areas"""
    logger.info("Starting area fix process...")
    
    if fix_land_areas():
        logger.info("✓ Area fix completed successfully")
    else:
        logger.error("✗ Failed to fix areas")

if __name__ == "__main__":
    main()