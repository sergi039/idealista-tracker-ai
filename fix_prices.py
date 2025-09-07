#!/usr/bin/env python3
"""
Fix prices for lands that were incorrectly parsed (showing 0 or very small values)
"""

import re
import logging
from app import app, db
from models import Land

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def extract_price_from_description(description: str) -> float:
    """Extract price from description text"""
    if not description:
        return None
    
    # Patterns for price extraction (both English and Spanish formats)
    patterns = [
        r'(\d{1,3}(?:,\d{3})*)\s*€',  # English format: 59,000 €
        r'(\d{1,3}(?:\.\d{3})*)\s*€',  # Spanish format: 59.000 €
    ]
    
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            price_str = match.group(1)
            # Remove both dots and commas used as thousand separators
            price_str = price_str.replace(',', '').replace('.', '')
            try:
                price = float(price_str)
                # Validate price is reasonable (not area or other number)
                if price >= 1000:  # Minimum reasonable price for land
                    return price
            except ValueError:
                continue
    
    return None

def fix_land_prices():
    """Fix prices for lands with incorrect values"""
    with app.app_context():
        try:
            # Get lands with suspicious prices (0 or very small values < 1000)
            suspicious_lands = Land.query.filter(
                (Land.price == None) | (Land.price == 0) | (Land.price < 1000)
            ).all()
            
            logger.info(f"Found {len(suspicious_lands)} lands with suspicious prices")
            
            fixed_count = 0
            for land in suspicious_lands:
                old_price = land.price
                
                # Try to extract price from description
                new_price = extract_price_from_description(land.description)
                
                if new_price and new_price != old_price:
                    land.price = new_price
                    fixed_count += 1
                    logger.info(f"Fixed land {land.id}: '{land.title[:40]}...' - Price: {old_price} -> {new_price}")
            
            db.session.commit()
            logger.info(f"Successfully fixed {fixed_count} land prices")
            
            # Show statistics
            lands_with_price = Land.query.filter(Land.price > 0).count()
            total_lands = Land.query.count()
            logger.info(f"Now {lands_with_price}/{total_lands} lands have valid prices")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to fix prices: {str(e)}")
            db.session.rollback()
            return False

def main():
    """Main function to fix land prices"""
    logger.info("Starting price fix process...")
    
    if fix_land_prices():
        logger.info("✓ Price fix completed successfully")
    else:
        logger.error("✗ Failed to fix prices")

if __name__ == "__main__":
    main()