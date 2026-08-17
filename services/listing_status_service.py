"""
Service for checking and tracking the status of Idealista listings.
Periodically checks if listings are still active or have been removed.
"""

import logging
import re
import requests
import threading
import time
import random
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional, Dict, Tuple
from urllib.parse import urlsplit

from models import Land, LandHistory, Property, SyncHistory
from app import db
from utils.http import request_with_retries

logger = logging.getLogger(__name__)


class Observation(NamedTuple):
    """What one fetch established, and -- when it established nothing -- why.

    `status` keeps the four values the storage path knows ('active', 'removed',
    'sold', 'error'); `refusal` is the reason behind an 'error' and is None on
    every other status. Splitting them is what lets the page say "idealista is
    blocking this machine" instead of "something went wrong", without changing
    what gets written: an 'error' still writes nothing at all.
    """

    status: str
    removed_date: Optional[str]
    refusal: Optional[str] = None


class RefusalBreaker:
    """Stop dialling a host that has already said no, and say so.

    idealista answers this machine with DataDome bot protection -- measured
    2026-08-15 over 76 consecutive properties, every one of them a captcha, not
    one listing page reached. The service was right to record nothing, but it
    kept spending a request per press to learn the same thing, and the reader
    got a generic failure each time.

    So refusals are counted across calls. After `threshold` in a row the
    breaker opens and later checks return immediately, spending nothing, and
    reporting the refusal as the standing condition it is. When the cooldown
    expires exactly one request goes out -- the breaker does not heal on a
    timer, it heals on evidence -- and a refusal re-arms it.

    Deliberately process-local and in-memory. It paces outbound traffic, so it
    must be cheap and must not need a table; each gunicorn worker keeping its
    own count means at worst `workers x threshold` requests before everything
    is quiet, which is a handful, not a sweep. It is a class attribute rather
    than an instance one because every caller builds a fresh service.
    """

    def __init__(self, threshold: int, cooldown_s: int):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._consecutive = 0
        self._blocked_until: Optional[datetime] = None
        self._last_reason: Optional[str] = None
        self._last_refusal_at: Optional[datetime] = None

    def should_skip(self, now: Optional[datetime] = None) -> bool:
        """May this caller dial? Answering False **claims** the probe.

        Deliberately not a pure query, and named in `observe` as the gate it is.
        A read-only version had a race an independent review found: with the
        cooldown expiring at 12:30:00, two request threads calling at 12:30:01
        both saw "not blocked" before either recorded a result, and both dialled
        a host that is refusing us. Exactly the failure this class exists to
        prevent, one level up -- and the docstring above promised "exactly one
        request goes out", which the code did not deliver.

        So the expiry is consumed inside the lock: the first caller through
        re-arms the window and dials, and everyone behind it keeps skipping
        until that probe reports. `record_success` clears the window if the
        probe reached the listing; `record_refusal` re-arms it if it did not,
        which is what it would have done anyway.
        """
        now = now or datetime.now(timezone.utc)
        with self._lock:
            if self._blocked_until is None:
                return False
            if now < self._blocked_until:
                return True
            # The cooldown has expired and this caller is the one probe.
            self._blocked_until = now + timedelta(seconds=self.cooldown_s)
            return False

    def record_refusal(self, reason: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._consecutive += 1
            self._last_reason = reason
            self._last_refusal_at = now
            if self._consecutive >= self.threshold:
                self._blocked_until = now + timedelta(seconds=self.cooldown_s)

    def record_success(self, now: Optional[datetime] = None) -> None:
        """A fetch reached the listing page: the host is answering us again."""
        with self._lock:
            self._consecutive = 0
            self._blocked_until = None
            self._last_reason = None

    def state(self) -> Dict:
        with self._lock:
            return {
                "open": self._blocked_until is not None,
                "consecutive_refusals": self._consecutive,
                "last_reason": self._last_reason,
                "last_refusal_at": self._last_refusal_at.isoformat()
                if self._last_refusal_at
                else None,
                "blocked_until": self._blocked_until.isoformat()
                if self._blocked_until
                else None,
            }

    def reset(self) -> None:
        """For tests and for a deliberate retry after the owner changes the route."""
        with self._lock:
            self._consecutive = 0
            self._blocked_until = None
            self._last_reason = None
            self._last_refusal_at = None


class HostBreakers:
    """One `RefusalBreaker` per host, because a refusal is about one host.

    There used to be a single process-wide breaker, which was right while
    every listing was on idealista.com and became wrong the moment a second
    site arrived. idealista refuses this machine *permanently* -- measured
    2026-08-15 over 76 consecutive properties, every one a DataDome block --
    so its breaker is open essentially always. A shared breaker therefore does
    not degrade fotocasa checks, it forbids them: three idealista refusals,
    which arrive the moment anybody presses anything, and the next fotocasa
    check returns `backing_off` for half an hour without a request going out.
    One host's wall would have become every host's.

    Keyed on the hostname rather than the full URL: the refusal is the site
    saying no, and per-URL counting would need `threshold` refusals from each
    listing before it stopped, which is the sweep the breaker exists to stop.
    An unparseable URL keys on the empty string -- one bucket for the
    malformed, which cannot be reached by a real fetch anyway.
    """

    def __init__(self, threshold: int, cooldown_s: int):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._by_host: Dict[str, RefusalBreaker] = {}

    @staticmethod
    def host_of(url: Optional[str]) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        if "//" not in raw:
            raw = "https://" + raw
        try:
            return (urlsplit(raw).hostname or "").lower()
        except ValueError:
            return ""

    def for_url(self, url: Optional[str]) -> RefusalBreaker:
        host = self.host_of(url)
        with self._lock:
            breaker = self._by_host.get(host)
            if breaker is None:
                breaker = RefusalBreaker(
                    threshold=self.threshold, cooldown_s=self.cooldown_s
                )
                self._by_host[host] = breaker
            return breaker

    def state(self) -> Dict:
        """Every host that has been dialled, and what it is saying.

        A report over hosts rather than one aggregate: "the breaker is open"
        was a true sentence about idealista and a false one about everything
        else, and a reader cannot tell those apart from a single flag.
        """
        with self._lock:
            hosts = dict(self._by_host)
        return {host or "(no host)": breaker.state() for host, breaker in hosts.items()}

    def reset(self) -> None:
        """Forget every host. `tests/conftest.py` calls this between tests."""
        with self._lock:
            breakers = list(self._by_host.values())
            self._by_host.clear()
        for breaker in breakers:
            breaker.reset()


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

    # When idealista refuses this machine it refuses everything: measured
    # 2026-08-10, the home page answers 403 with the same DataDome block page a
    # listing does, with or without a full set of browser headers. A sweep that
    # keeps going then spends one outbound request per remaining row to learn
    # the same thing, and hammering a site that has just said no is the wrong
    # answer besides. Stop after this many refusals in a row and report that
    # the run was cut short -- an unfinished sweep is not a finished one.
    CONSECUTIVE_ERROR_LIMIT = 3

    # The cross-call counterpart of the limit above. That one ends a sweep;
    # this one outlives it, so the next sweep -- and the next press of the
    # per-listing button -- does not start the same wall from scratch.
    REFUSAL_BREAKER_THRESHOLD = 3
    REFUSAL_COOLDOWN_S = 30 * 60

    # Shared by every instance: each caller constructs its own service, so an
    # instance attribute would forget the refusal the moment it answered. One
    # breaker per host -- see `HostBreakers` for why a single one silently
    # forbade every non-idealista check.
    breakers = HostBreakers(
        threshold=REFUSAL_BREAKER_THRESHOLD, cooldown_s=REFUSAL_COOLDOWN_S
    )

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

        The two-value shape every caller has always unpacked. `observe` below
        carries the same answer plus the reason behind an 'error'.
        """
        observation = self.observe(url)
        return observation.status, observation.removed_date

    def observe(self, url: str) -> Observation:
        """`check_listing_status` plus the reason a refusal was a refusal.

        Every path that returns 'error' names why, and tells the breaker. The
        storage contract is unchanged: 'error' still writes nothing, so no
        reason here can become a status.
        """
        if not url:
            return Observation("error", None, "no_url")

        # Resolved once, from this URL: the wall belongs to one host, and the
        # rest of this method must not reach for a different one.
        breaker = self.breakers.for_url(url)

        if breaker.should_skip():
            state = breaker.state()
            logger.info(
                "Skipping the status check for %s: %s refused the last %s "
                "checks (%s); backing off until %s",
                url,
                self.breakers.host_of(url) or "the host",
                state["consecutive_refusals"],
                state["last_reason"],
                state["blocked_until"],
            )
            return Observation("error", None, "backing_off")

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
                # A reached answer, not a refusal: the host talked to us.
                breaker.record_success()
                return Observation("removed", None)

            # Check response content
            content = response.text.lower()

            # Check for captcha/bot protection
            for pattern in self.CAPTCHA_PATTERNS:
                if pattern.lower() in content:
                    logger.warning(f"Hit captcha protection for: {url}")
                    return self._refused("blocked", url)

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
                # 403 is how the bot protection answers once it stops serving
                # the captcha page itself, so it is a refusal by this host and
                # not a fault of ours; the rest are the server's own trouble.
                reason = (
                    "blocked" if response.status_code in (401, 403) else "http_error"
                )
                return self._refused(reason, url)

            # Check for sold patterns
            for pattern in self.SOLD_PATTERNS:
                if pattern.lower() in content:
                    logger.info(f"Listing sold: {url}")
                    breaker.record_success()
                    return Observation("sold", None)

            # Check for removed patterns
            for pattern in self.REMOVED_PATTERNS:
                if pattern.lower() in content:
                    logger.info(f"Listing removed: {url}")
                    # Try to extract removal date
                    removed_date = self._extract_removal_date(response.text)
                    breaker.record_success()
                    return Observation("removed", removed_date)

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
                # The other shape of the block: idealista takes the request and
                # answers 200 with its home page instead of the listing.
                return self._refused("not_the_listing_page", url)

            breaker.record_success()
            return Observation("active", None)

        except requests.Timeout:
            logger.warning(f"Timeout checking listing: {url}")
            return self._refused("timeout", url)
        except requests.RequestException:
            logger.error("Error checking listing %s", url, exc_info=True)
            return self._refused("unreachable", url)

    def _refused(self, reason: str, url: str) -> Observation:
        """One exit for every path that learned nothing about the listing."""
        breaker = self.breakers.for_url(url)
        breaker.record_refusal(reason)
        state = breaker.state()
        if state["open"]:
            logger.warning(
                "%s has refused %s checks in a row (%s); pausing status "
                "checks until %s",
                self.breakers.host_of(url) or "the host",
                state["consecutive_refusals"],
                reason,
                state["blocked_until"],
            )
        return Observation("error", None, reason)

    # How each site names a listing in its own URL, and the pattern that
    # recognises that same name in the URL finally served. Two entries, one
    # rule: without the fotocasa one, `_looks_like_listing_page` fell through
    # to "any 200 is the listing" for all 56 fotocasa rows in this table, so a
    # redirect to a search page would have been recorded as a live listing --
    # the false confirmation of #136, at a second host.
    _LISTING_ID_PATTERNS = (
        (re.compile(r"/inmueble/(\d+)"), "inmueble/{id}(?!\\d)"),
        (re.compile(r"/(\d{4,})/d/?(?:[?#]|$)"), "/{id}/d(?![0-9])"),
    )

    @classmethod
    def _listing_id_from_url(cls, url: str) -> Optional[str]:
        """The number that identifies a listing page, in either site's spelling.

        `/inmueble/<id>/` on idealista, `/<id>/d` on fotocasa. Measured
        2026-08-17: all 56 stored fotocasa URLs end in `/<id>/d`, so the second
        pattern really covers them rather than covering the one example.
        """
        for pattern, _ in cls._LISTING_ID_PATTERNS:
            match = pattern.search(url or "")
            if match:
                return match.group(1)
        return None

    @classmethod
    def _served_listing_pattern(cls, url: str) -> Optional[str]:
        """The regex that recognises this URL's listing in the URL served."""
        for pattern, template in cls._LISTING_ID_PATTERNS:
            match = pattern.search(url or "")
            if match:
                return template.format(id=re.escape(match.group(1)))
        return None

    def _looks_like_listing_page(self, url: str, response) -> bool:
        """Did a 200 actually hand us the listing we asked for?

        Judged on the **final URL only**, after redirects. That is where the
        server says what it served; the body is not, because any page can echo
        or link the URL we asked for -- an error page reading "could not load
        /inmueble/1234/" would otherwise pass as the listing itself.

        The id needs a boundary after it: plain substring matching accepts
        /inmueble/12345/ as an answer for /inmueble/1234/, which is a different
        listing entirely. A URL with no id to anchor on falls back to the status
        code rather than refusing every non-standard link -- one row here is an
        agency's own site, whose URL grammar this cannot know.
        """
        served = self._served_listing_pattern(url)
        if not served:
            return True

        final_url = (getattr(response, "url", "") or "").lower()
        return bool(re.search(served, final_url))

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
        # This is the one path that read the listing page, so it is the one path
        # that may claim so. A hand-set status and a status taken from
        # idealista's removal email stamp their own source instead.
        record.listing_status_source = "check"
        current = record.listing_status or "active"

        # Every observation that differs from the stored value is applied.
        # Enumerating only active<->terminal left `sold` -> `removed` (and the
        # reverse) discarded while the stamps above had already been written, so
        # the page kept the old verdict, dated today, credited to the very check
        # that contradicted it -- the false confirmation of #136, in the one
        # transition it did not cover (#224).
        if status == current:
            return False, None

        if status == "active":
            record.listing_status = "active"
            record.listing_removed_date = None
            return True, "relisted"

        if status in ("removed", "sold"):
            observed_date = None
            if removed_date_str:
                try:
                    observed_date = datetime.strptime(removed_date_str, "%d/%m/%Y")
                except ValueError:
                    observed_date = datetime.now(timezone.utc)

            if current == "active":
                # It left the market now; the page's own date wins if it gives one.
                record.listing_removed_date = observed_date or datetime.now(
                    timezone.utc
                )
                transition = "deactivated"
            else:
                # Already off the market: keep the earlier, better date unless
                # the page states one, and never invent a second removal date.
                if observed_date is not None:
                    record.listing_removed_date = observed_date
                elif record.listing_removed_date is None:
                    record.listing_removed_date = datetime.now(timezone.utc)
                transition = "restated"

            record.listing_status = status
            return True, transition

        return False, None

    def check_land_status(self, land: Land) -> Dict:
        """
        Check the listing status for a single Land object.

        Returns dict with status info and updates the land object.
        """
        if not land.url:
            return {"success": False, "error": "No URL available", "land_id": land.id}

        # Check the listing
        status, removed_date_str, refusal = self.observe(land.url)
        previous_status = land.listing_status

        changed, transition = self._apply_observed_status(
            land, status, removed_date_str
        )

        if changed:
            # Create history record for favorites. `restated` (sold <-> removed)
            # is a change of wording about a listing that was already off the
            # market, so it is neither of the two events the history models —
            # recording it as a relisting would be worse than not recording it.
            event = {"deactivated": "removed_from_listing", "relisted": "relisted"}.get(
                transition
            )
            if land.is_favorite and event:
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
            "refusal": refusal,
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

        status, removed_date_str, refusal = self.observe(prop.url)
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
            # Why nothing was learned, when nothing was. The page says
            # "idealista is refusing this machine" off this rather than
            # reporting every refusal as an unexplained failure.
            "refusal": refusal,
            # This listing's host, not every host: a reader looking at one
            # fotocasa listing is not helped by idealista's wall.
            "breaker": self.breakers.for_url(prop.url).state(),
        }

    @staticmethod
    def _was_refused(result: Dict) -> bool:
        """Did this row's check fail to reach the listing page?

        Both shapes count: a row the service could not even try (no URL) and a
        fetch idealista refused, which `check_listing_status` reports as the
        observed status 'error'. Neither told us anything about the listing.

        The first shape cannot reach the sweeps that use this -- both select on
        `Land.url.isnot(None)` -- so a run cut short really is idealista saying
        no, not a gap in our own data. It is counted anyway because a caller
        that skips that filter would still be learning nothing per request.
        """
        if not result.get("success"):
            return True
        return result.get("new_status") == "error"

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
            "stopped_early": False,
            "unchecked": 0,
            "details": [],
        }

        consecutive_errors = 0
        for index, land in enumerate(favorites):
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

            consecutive_errors = (
                consecutive_errors + 1 if self._was_refused(result) else 0
            )
            if consecutive_errors >= self.CONSECUTIVE_ERROR_LIMIT:
                results["stopped_early"] = True
                results["unchecked"] = len(favorites) - (index + 1)
                logger.warning(
                    "Stopping the favourites sweep after %s refusals in a row; "
                    "%s left unchecked",
                    consecutive_errors,
                    results["unchecked"],
                )
                break

        logger.info(
            f"Checked {results['checked']} favorites: "
            f"{results['active']} active, {results['removed']} removed, "
            f"{results['sold']} sold, {results['errors']} errors"
        )

        # What the run met, not just what it did: a sweep that checked nothing
        # because the host is refusing us reads identically to a quiet one
        # without this.
        results["breaker"] = self.breakers.state()
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
            "stopped_early": False,
            "unchecked": 0,
            "details": [],
        }

        consecutive_errors = 0
        for index, land in enumerate(listings):
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

            consecutive_errors = (
                consecutive_errors + 1 if self._was_refused(result) else 0
            )
            if consecutive_errors >= self.CONSECUTIVE_ERROR_LIMIT:
                results["stopped_early"] = True
                results["unchecked"] = len(listings) - (index + 1)
                logger.warning(
                    "Stopping the listing sweep after %s refusals in a row; "
                    "%s left unchecked",
                    consecutive_errors,
                    results["unchecked"],
                )
                break

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

        # What the run met, not just what it did: a sweep that checked nothing
        # because the host is refusing us reads identically to a quiet one
        # without this.
        results["breaker"] = self.breakers.state()
        return results
