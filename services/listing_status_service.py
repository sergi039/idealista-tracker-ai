"""
Service for checking and tracking the status of Idealista listings.
Periodically checks if listings are still active or have been removed.
"""

import logging
import re
import requests
import time
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple

from models import Land, LandHistory, Property, SyncHistory
from app import db
from utils.http import request_with_retries

logger = logging.getLogger(__name__)


class ListingStatusService:
    """Service to check if Idealista listings are still active"""

    # Patterns that indicate a listing has been removed
    REMOVED_PATTERNS = [
        "this listing is no longer published",
        "ya no está publicado",
        "ya no está disponible",
        "anuncio ya no está disponible",
        "listing is no longer available",
        "The advertiser removed it",
        "El anunciante lo ha eliminado",
        "sorry, this listing is no longer published",
    ]

    # Patterns that indicate we hit a captcha/bot protection
    CAPTCHA_PATTERNS = [
        "captcha-delivery.com",
        "please enable js",
        "checking your browser",
        "just a moment",
        "ddos-guard",
    ]

    # Patterns that indicate listing was sold
    SOLD_PATTERNS = [
        "has been sold",
        "vendido",
        "se ha vendido",
    ]

    # User agent to mimic browser
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        )

    def check_listing_status(self, url: str) -> Tuple[str, Optional[str]]:
        """
        Check if a listing is still active on Idealista.

        Returns:
            Tuple of (status, removed_date_str)
            status: 'active', 'removed', 'sold', 'error'
            removed_date_str: Date when removed (if found in page), or None
        """
        if not url:
            return "error", None

        try:
            response = request_with_retries(
                self.session.get,
                url,
                timeout=15,
                allow_redirects=True,
                logger=logger,
            )

            # Check for 404
            if response.status_code == 404:
                return "removed", None

            # Check response content
            content = response.text.lower()

            # Check for captcha/bot protection
            for pattern in self.CAPTCHA_PATTERNS:
                if pattern.lower() in content:
                    logger.warning(f"Hit captcha protection for: {url}")
                    return "error", None

            # Everything below reads the body as if it were the listing page,
            # so refuse anything that is not a plain 200 first. An error page
            # that happens to carry "no longer published" is not evidence the
            # advertiser removed anything, and a 403 or 5xx is not evidence the
            # listing is alive either -- both mean the check did not happen.
            if response.status_code != 200:
                logger.warning(
                    "Unexpected HTTP %s checking listing %s; reporting as error",
                    response.status_code,
                    url,
                )
                return "error", None

            # Check for sold patterns
            for pattern in self.SOLD_PATTERNS:
                if pattern.lower() in content:
                    logger.info(f"Listing sold: {url}")
                    return "sold", None

            # Check for removed patterns
            for pattern in self.REMOVED_PATTERNS:
                if pattern.lower() in content:
                    logger.info(f"Listing removed: {url}")
                    # Try to extract removal date
                    removed_date = self._extract_removal_date(response.text)
                    return "removed", removed_date

            # A 200 only proves the listing is up if what came back *is* the
            # listing page. idealista answers plenty of requests by sending us
            # somewhere else -- a search page, the home page -- with a perfectly
            # good status code, and calling that "active" would be a false
            # confirmation (issue #136).
            if not self._looks_like_listing_page(url, response):
                logger.warning(
                    "200 for %s did not come back as the listing page; reporting as error",
                    url,
                )
                return "error", None

            return "active", None

        except requests.Timeout:
            logger.warning(f"Timeout checking listing: {url}")
            return "error", None
        except requests.RequestException:
            logger.error("Error checking listing %s", url, exc_info=True)
            return "error", None

    @staticmethod
    def _listing_id_from_url(url: str) -> Optional[str]:
        """The /inmueble/<id>/ number, which is what identifies a listing page."""
        match = re.search(r"/inmueble/(\d+)", url or "")
        return match.group(1) if match else None

    def _looks_like_listing_page(self, url: str, response) -> bool:
        """Did a 200 actually hand us the listing we asked for?

        Judged on the **final URL only**, after redirects. That is where the
        server says what it served; the body is not, because any page can echo
        or link the URL we asked for -- an error page reading "could not load
        /inmueble/1234/" would otherwise pass as the listing itself.

        The id needs a boundary after it: plain substring matching accepts
        /inmueble/12345/ as an answer for /inmueble/1234/, which is a different
        listing entirely. A URL with no id to anchor on falls back to the status
        code rather than refusing every non-standard link.
        """
        listing_id = self._listing_id_from_url(url)
        if not listing_id:
            return True

        final_url = (getattr(response, "url", "") or "").lower()
        return bool(re.search(rf"inmueble/{listing_id}(?!\d)", final_url))

    def _extract_removal_date(self, html_content: str) -> Optional[str]:
        """Try to extract the removal date from the page content"""

        # Pattern: "The advertiser removed it on 01/12/2025"
        patterns = [
            r"removed it on (\d{1,2}/\d{1,2}/\d{4})",
            r"lo ha eliminado el (\d{1,2}/\d{1,2}/\d{4})",
            r"(\d{1,2}/\d{1,2}/\d{4})",
        ]

        for pattern in patterns:
            match = re.search(pattern, html_content)
            if match:
                return match.group(1)

        return None

    def _apply_observed_status(
        self, record, status: str, removed_date_str: Optional[str]
    ) -> Tuple[bool, Optional[str]]:
        """Write a freshly observed status onto a Land or Property row.

        Returns (changed, transition), where transition is 'deactivated',
        'relisted' or None.

        An 'error' observation writes nothing at all -- not even
        listing_last_checked. A blocked or failed fetch is not a check, and
        stamping a date on it would make the page read "Status: active,
        Checked: today" about a listing nobody ever verified. That is the exact
        false confirmation this work exists to remove (issue #136).
        """
        if status == "error":
            return False, None

        record.listing_last_checked = datetime.now(timezone.utc)
        current = record.listing_status or "active"

        if status in ("removed", "sold") and current == "active":
            if removed_date_str:
                try:
                    record.listing_removed_date = datetime.strptime(
                        removed_date_str, "%d/%m/%Y"
                    )
                except ValueError:
                    record.listing_removed_date = datetime.now(timezone.utc)
            else:
                record.listing_removed_date = datetime.now(timezone.utc)

            record.listing_status = status
            return True, "deactivated"

        if status == "active" and current in ("removed", "sold"):
            record.listing_status = "active"
            record.listing_removed_date = None
            return True, "relisted"

        return False, None

    def check_land_status(self, land: Land) -> Dict:
        """
        Check the listing status for a single Land object.

        Returns dict with status info and updates the land object.
        """
        if not land.url:
            return {"success": False, "error": "No URL available", "land_id": land.id}

        # Check the listing
        status, removed_date_str = self.check_listing_status(land.url)
        previous_status = land.listing_status

        changed, transition = self._apply_observed_status(
            land, status, removed_date_str
        )

        if changed:
            # Create history record for favorites
            if land.is_favorite:
                event = (
                    "removed_from_listing"
                    if transition == "deactivated"
                    else "relisted"
                )
                db.session.add(LandHistory.create_snapshot(land, event))
                logger.info("Created %s snapshot for favorite land %s", event, land.id)

            logger.info(
                "Land %s status changed: %s -> %s",
                land.id,
                previous_status,
                land.listing_status,
            )

        db.session.commit()
        return {
            "success": True,
            "land_id": land.id,
            "previous_status": previous_status,
            "new_status": status,
            "changed": changed,
        }

    def check_property_status(self, prop: Property) -> Dict:
        """
        Check the listing status for a single universal Property row.

        Properties have no history table, so unlike Land there is no snapshot
        side effect here -- the row itself carries status, removal date and
        last-checked time.
        """
        if not prop.url:
            return {
                "success": False,
                "error": "No URL available",
                "property_id": prop.id,
            }

        status, removed_date_str = self.check_listing_status(prop.url)
        previous_status = prop.listing_status

        changed, _ = self._apply_observed_status(prop, status, removed_date_str)

        if changed:
            logger.info(
                "Property %s status changed: %s -> %s",
                prop.id,
                previous_status,
                prop.listing_status,
            )

        db.session.commit()
        return {
            "success": True,
            "property_id": prop.id,
            "previous_status": previous_status,
            "new_status": status,
            "changed": changed,
        }

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
        favorites = (
            Land.query.filter(
                Land.is_favorite,
                Land.listing_status == "active",
                Land.url.isnot(None),
            )
            .order_by(Land.listing_last_checked.asc().nullsfirst())
            .limit(limit)
            .all()
        )

        results = {
            "checked": 0,
            "active": 0,
            "removed": 0,
            "sold": 0,
            "errors": 0,
            "details": [],
        }

        for land in favorites:
            # Add small delay between requests to be polite
            time.sleep(random.uniform(1, 3))

            result = self.check_land_status(land)
            results["checked"] += 1

            if result.get("success"):
                status = result.get("new_status", "error")
                if status == "active":
                    results["active"] += 1
                elif status == "removed":
                    results["removed"] += 1
                elif status == "sold":
                    results["sold"] += 1
                else:
                    results["errors"] += 1

                if result.get("changed"):
                    results["details"].append(
                        {
                            "land_id": land.id,
                            "title": land.title[:50] if land.title else "Unknown",
                            "old_status": result.get("previous_status"),
                            "new_status": status,
                        }
                    )
            else:
                results["errors"] += 1

        logger.info(
            f"Checked {results['checked']} favorites: "
            f"{results['active']} active, {results['removed']} removed, "
            f"{results['sold']} sold, {results['errors']} errors"
        )

        return results

    def check_all_active_listings(
        self, limit: int = 100, days_since_check: int = 7, record_sync: bool = True
    ) -> Dict:
        """
        Check all active listings that haven't been checked in X days.
        Favorites are checked more frequently (daily), others weekly.

        Args:
            limit: Maximum number to check
            days_since_check: Only check if last check was more than X days ago
            record_sync: Whether to record this check in SyncHistory

        Returns:
            Summary of the check operation
        """
        start_time = datetime.now(timezone.utc)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_since_check)

        # Get active listings that need checking
        listings = (
            Land.query.filter(
                Land.listing_status == "active",
                Land.url.isnot(None),
                db.or_(
                    Land.listing_last_checked.is_(None),
                    Land.listing_last_checked < cutoff_date,
                ),
            )
            .order_by(
                Land.is_favorite.desc(),  # Favorites first
                Land.listing_last_checked.asc().nullsfirst(),
            )
            .limit(limit)
            .all()
        )

        results = {
            "checked": 0,
            "active": 0,
            "removed": 0,
            "sold": 0,
            "errors": 0,
            "details": [],
        }

        for land in listings:
            # Add delay between requests
            time.sleep(random.uniform(2, 4))

            result = self.check_land_status(land)
            results["checked"] += 1

            if result.get("success"):
                status = result.get("new_status", "error")
                if status == "active":
                    results["active"] += 1
                elif status == "removed":
                    results["removed"] += 1
                elif status == "sold":
                    results["sold"] += 1
                else:
                    results["errors"] += 1

                if result.get("changed"):
                    results["details"].append(
                        {
                            "land_id": land.id,
                            "title": land.title[:50] if land.title else "Unknown",
                            "is_favorite": land.is_favorite,
                            "old_status": result.get("previous_status"),
                            "new_status": status,
                        }
                    )
            else:
                results["errors"] += 1

        # Record in SyncHistory if any listings were removed/sold
        if record_sync and (results["removed"] > 0 or results["sold"] > 0):
            try:
                sync_history = SyncHistory(
                    sync_type="status_check",
                    backend="web_scrape",
                    total_emails_found=results["checked"],
                    new_properties_added=0,
                    price_updated_count=0,
                    expired_count=results["removed"] + results["sold"],
                    status="completed",
                    started_at=start_time,
                    completed_at=datetime.now(timezone.utc),
                    sync_duration=int(
                        (datetime.now(timezone.utc) - start_time).total_seconds()
                    ),
                )
                db.session.add(sync_history)
                db.session.commit()
                logger.info(
                    f"Recorded status check in SyncHistory: {results['removed']} removed, {results['sold']} sold"
                )
            except Exception:
                logger.error(
                    "Failed to record status check in SyncHistory", exc_info=True
                )

        logger.info(
            f"Checked {results['checked']} listings: "
            f"{results['active']} active, {results['removed']} removed, "
            f"{results['sold']} sold, {results['errors']} errors"
        )

        return results
