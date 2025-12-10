"""
Service for checking and tracking the status of Idealista listings.
Periodically checks if listings are still active or have been removed.
"""

import logging
import requests
import time
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from models import Land, LandHistory
from app import db

logger = logging.getLogger(__name__)


class ListingStatusService:
    """Service to check if Idealista listings are still active"""

    # Patterns that indicate a listing has been removed
    REMOVED_PATTERNS = [
        'this listing is no longer published',
        'ya no está publicado',
        'ya no está disponible',
        'anuncio ya no está disponible',
        'listing is no longer available',
        'The advertiser removed it',
        'El anunciante lo ha eliminado',
        'sorry, this listing is no longer published',
    ]

    # Patterns that indicate we hit a captcha/bot protection
    CAPTCHA_PATTERNS = [
        'captcha-delivery.com',
        'please enable js',
        'checking your browser',
        'just a moment',
        'ddos-guard',
    ]

    # Patterns that indicate listing was sold
    SOLD_PATTERNS = [
        'has been sold',
        'vendido',
        'se ha vendido',
    ]

    # User agent to mimic browser
    USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })

    def check_listing_status(self, url: str) -> Tuple[str, Optional[str]]:
        """
        Check if a listing is still active on Idealista.

        Returns:
            Tuple of (status, removed_date_str)
            status: 'active', 'removed', 'sold', 'error'
            removed_date_str: Date when removed (if found in page), or None
        """
        if not url:
            return 'error', None

        try:
            response = self.session.get(url, timeout=15, allow_redirects=True)

            # Check for 404
            if response.status_code == 404:
                return 'removed', None

            # Check response content
            content = response.text.lower()

            # Check for captcha/bot protection
            for pattern in self.CAPTCHA_PATTERNS:
                if pattern.lower() in content:
                    logger.warning(f"Hit captcha protection for: {url}")
                    return 'error', None

            # Check for sold patterns
            for pattern in self.SOLD_PATTERNS:
                if pattern.lower() in content:
                    logger.info(f"Listing sold: {url}")
                    return 'sold', None

            # Check for removed patterns
            for pattern in self.REMOVED_PATTERNS:
                if pattern.lower() in content:
                    logger.info(f"Listing removed: {url}")
                    # Try to extract removal date
                    removed_date = self._extract_removal_date(response.text)
                    return 'removed', removed_date

            # If we get here and status is 200, listing is likely active
            if response.status_code == 200:
                # Additional check: look for price element to confirm it's a valid listing
                if 'info-data-price' in content or 'precio' in content:
                    return 'active', None

            return 'active', None

        except requests.Timeout:
            logger.warning(f"Timeout checking listing: {url}")
            return 'error', None
        except requests.RequestException as e:
            logger.error(f"Error checking listing {url}: {str(e)}")
            return 'error', None

    def _extract_removal_date(self, html_content: str) -> Optional[str]:
        """Try to extract the removal date from the page content"""
        import re

        # Pattern: "The advertiser removed it on 01/12/2025"
        patterns = [
            r'removed it on (\d{1,2}/\d{1,2}/\d{4})',
            r'lo ha eliminado el (\d{1,2}/\d{1,2}/\d{4})',
            r'(\d{1,2}/\d{1,2}/\d{4})',
        ]

        for pattern in patterns:
            match = re.search(pattern, html_content)
            if match:
                return match.group(1)

        return None

    def check_land_status(self, land: Land) -> Dict:
        """
        Check the listing status for a single Land object.

        Returns dict with status info and updates the land object.
        """
        if not land.url:
            return {
                'success': False,
                'error': 'No URL available',
                'land_id': land.id
            }

        # Check the listing
        status, removed_date_str = self.check_listing_status(land.url)

        result = {
            'success': True,
            'land_id': land.id,
            'previous_status': land.listing_status,
            'new_status': status,
            'changed': False
        }

        # Update last checked time
        land.listing_last_checked = datetime.utcnow()

        # If status changed to removed or sold
        if status in ('removed', 'sold') and land.listing_status == 'active':
            result['changed'] = True

            # Parse removal date if available
            if removed_date_str:
                try:
                    land.listing_removed_date = datetime.strptime(removed_date_str, '%d/%m/%Y')
                except ValueError:
                    land.listing_removed_date = datetime.utcnow()
            else:
                land.listing_removed_date = datetime.utcnow()

            land.listing_status = status

            # Create history record for favorites
            if land.is_favorite:
                snapshot = LandHistory.create_snapshot(land, 'removed_from_listing')
                db.session.add(snapshot)
                logger.info(f"Created removal snapshot for favorite land {land.id}")

            logger.info(f"Land {land.id} status changed: active -> {status}")

        # If status changed back to active (re-listed)
        elif status == 'active' and land.listing_status in ('removed', 'sold'):
            result['changed'] = True
            land.listing_status = 'active'
            land.listing_removed_date = None

            # Create history record for favorites
            if land.is_favorite:
                snapshot = LandHistory.create_snapshot(land, 'relisted')
                db.session.add(snapshot)
                logger.info(f"Land {land.id} was re-listed")

        db.session.commit()
        return result

    def check_favorites_status(self, limit: int = 50) -> Dict:
        """
        Check status of all favorite listings.
        Prioritizes favorites that haven't been checked recently.

        Args:
            limit: Maximum number of listings to check in one run

        Returns:
            Summary of the check operation
        """
        # Get favorites ordered by last checked (oldest first, null first)
        favorites = Land.query.filter(
            Land.is_favorite == True,
            Land.listing_status == 'active',
            Land.url.isnot(None)
        ).order_by(
            Land.listing_last_checked.asc().nullsfirst()
        ).limit(limit).all()

        results = {
            'checked': 0,
            'active': 0,
            'removed': 0,
            'sold': 0,
            'errors': 0,
            'details': []
        }

        for land in favorites:
            # Add small delay between requests to be polite
            time.sleep(random.uniform(1, 3))

            result = self.check_land_status(land)
            results['checked'] += 1

            if result.get('success'):
                status = result.get('new_status', 'error')
                if status == 'active':
                    results['active'] += 1
                elif status == 'removed':
                    results['removed'] += 1
                elif status == 'sold':
                    results['sold'] += 1
                else:
                    results['errors'] += 1

                if result.get('changed'):
                    results['details'].append({
                        'land_id': land.id,
                        'title': land.title[:50] if land.title else 'Unknown',
                        'old_status': result.get('previous_status'),
                        'new_status': status
                    })
            else:
                results['errors'] += 1

        logger.info(f"Checked {results['checked']} favorites: "
                   f"{results['active']} active, {results['removed']} removed, "
                   f"{results['sold']} sold, {results['errors']} errors")

        return results

    def check_all_active_listings(self, limit: int = 100, days_since_check: int = 7) -> Dict:
        """
        Check all active listings that haven't been checked in X days.
        Favorites are checked more frequently (daily), others weekly.

        Args:
            limit: Maximum number to check
            days_since_check: Only check if last check was more than X days ago

        Returns:
            Summary of the check operation
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days_since_check)

        # Get active listings that need checking
        listings = Land.query.filter(
            Land.listing_status == 'active',
            Land.url.isnot(None),
            db.or_(
                Land.listing_last_checked.is_(None),
                Land.listing_last_checked < cutoff_date
            )
        ).order_by(
            Land.is_favorite.desc(),  # Favorites first
            Land.listing_last_checked.asc().nullsfirst()
        ).limit(limit).all()

        results = {
            'checked': 0,
            'active': 0,
            'removed': 0,
            'sold': 0,
            'errors': 0,
            'details': []
        }

        for land in listings:
            # Add delay between requests
            time.sleep(random.uniform(2, 4))

            result = self.check_land_status(land)
            results['checked'] += 1

            if result.get('success'):
                status = result.get('new_status', 'error')
                if status == 'active':
                    results['active'] += 1
                elif status == 'removed':
                    results['removed'] += 1
                elif status == 'sold':
                    results['sold'] += 1
                else:
                    results['errors'] += 1

                if result.get('changed'):
                    results['details'].append({
                        'land_id': land.id,
                        'title': land.title[:50] if land.title else 'Unknown',
                        'is_favorite': land.is_favorite,
                        'old_status': result.get('previous_status'),
                        'new_status': status
                    })
            else:
                results['errors'] += 1

        logger.info(f"Checked {results['checked']} listings: "
                   f"{results['active']} active, {results['removed']} removed, "
                   f"{results['sold']} sold, {results['errors']} errors")

        return results
