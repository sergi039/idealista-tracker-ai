import logging
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from email import message_from_bytes
from typing import Any, Dict, List, Optional

from imapclient import IMAPClient
from sqlalchemy.exc import IntegrityError

from app import db
from config import Config
from models import Property, SyncHistory
from utils.email_headers import decode_header_value
from utils.google_spend import (
    CAP_INGEST_GEOCODE,
    CAP_INGEST_TRAVEL,
    authorized_spend,
)
from services import fotocasa_source, milanuncios_source, yaencontre_source
from services.settings_service import SettingsService
from services.search_profile_service import SearchProfileService
from services.property_classification_service import PropertyClassificationService
from utils.email_parser import EmailParser
from utils.uid_cursor import UidBatchCursor, read_uid_file, write_uid_file
from utils.idealista_extractors import (
    extract_area_m2,
    extract_idealista_property_id,
    extract_listing_title,
    extract_municipality_from_title,
    extract_price,
    extract_price_change,
    extract_property_attributes,
    extract_url,
    is_truncated_municipality,
    resolve_truncated_municipality,
    truncation_stem_ends_at_connective,
)

logger = logging.getLogger(__name__)

# Fotocasa serves its "SENTIMOS LA INTERRUPCIÓN" page with a 200 after a
# handful of closely spaced requests (measured 2026-08-17 during the
# advertiser backfill: five requests at 3 s were enough), and the block then
# stands for several minutes. After this many refusals in a row the run stops
# fetching that portal's pages instead of walking the rest of the batch into
# the same wall -- the pattern `utils/backfill_advertiser.py` already uses.
# The emails whose pages were never read hold the UID cursor and are re-read
# next run, so stopping early loses nothing. One counter per portal host
# (`HostBreakers`' lesson): fotocasa refusing must not stop a milanuncios
# fetch that would have answered.
PORTAL_MAX_CONSECUTIVE_REFUSALS = 3

# The portals whose alert email this loop can read, beside idealista itself:
# which Config key names their senders, and which email_data type their
# entries carry. `services/ingest_policy.py` still governs who may run this
# at all; this table only says what an already-authorized run recognizes.
PORTAL_SENDER_KEYS = {
    "fotocasa": "FOTOCASA_ALERT_SENDERS",
    "milanuncios": "MILANUNCIOS_ALERT_SENDERS",
    "yaencontre": "YAENCONTRE_ALERT_SENDERS",
}


class PropertyIMAPService:
    """Universal IMAP ingestion into Property (sale-first)."""

    _PROPERTY_ID_RE = re.compile(r"/inmueble/(\d+)", re.IGNORECASE)
    _RENTAL_RE = re.compile(
        r"\b(alquiler|en\s+alquiler|for\s+rent|to\s+rent|rental)\b", re.IGNORECASE
    )

    def __init__(self):
        self.host = Config.IMAP_HOST
        self.port = Config.IMAP_PORT
        self.ssl = Config.IMAP_SSL
        self.timeout = Config.IMAP_TIMEOUT_SECONDS
        self.user = Config.IMAP_USER
        self.password = Config.IMAP_PASSWORD
        self.folder = Config.IMAP_FOLDER
        self.search_query = Config.IMAP_SEARCH_QUERY
        self.max_emails = Config.MAX_EMAILS_PER_RUN
        self.email_parser = EmailParser()
        self.last_seen_uid = self._get_last_seen_uid()
        # Set by get_idealista_emails(); run_ingestion() advances it per email
        # only after that email's DB work is committed (issue #24).
        self._uid_cursor: Optional[UidBatchCursor] = None
        # Consecutive page refusals per portal in this run; a success resets
        # its portal and PORTAL_MAX_CONSECUTIVE_REFUSALS stops that portal's
        # fetching for the rest of the run.
        self._portal_refusals: Dict[str, int] = {}
        # What MAX_EMAILS_PER_RUN left behind, or None when it did not bite.
        # Set by get_idealista_emails(), read by run_ingestion() so a capped
        # run says so instead of looking like a complete one.
        self._truncation: Optional[Dict[str, int]] = None
        # The exception class name when the IMAP read failed, or None. Same
        # reason: that read swallows its failure so the emails it did parse
        # survive, and the run must not then report itself complete.
        self._fetch_error: Optional[str] = None

    @staticmethod
    def _portal_senders(portal: str) -> List[str]:
        """The senders a portal's alert mail is expected from, or [] when off.

        Read from Config at call time rather than frozen in __init__, the way
        `_excluded_categories` already is, so a test or a settings change does
        not need a fresh service instance to be seen.
        """
        raw = getattr(Config, PORTAL_SENDER_KEYS[portal], "") or ""
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _all_portal_senders(self) -> List[str]:
        return [
            s for portal in PORTAL_SENDER_KEYS for s in self._portal_senders(portal)
        ]

    def _portal_email_entry(
        self,
        *,
        uid: int,
        subject: str,
        body: str,
        email_source_id: str,
        internal_date: Any,
        email_sender: str,
    ) -> Optional[Dict[str, Any]]:
        """One email_data entry for a portal alert email, or None.

        Recognition is by what the email carries, not by its sender: the
        sender lists gate each portal on/off and shape the Gmail query, and
        the link shapes decide the rest -- the same order idealista's own
        recognition runs in. Fotocasa and milanuncios entries carry only
        what to fetch; yaencontre entries carry the parsed cards, because
        its portal answers DataDome to every request from these machines
        (measured 2026-08-30) and the email is the whole source.
        """
        base: Dict[str, Any] = {
            "uid": uid,
            "source_email_id": email_source_id,
            "email_received_at": internal_date,
            "email_subject": subject,
            "email_sender": email_sender,
        }

        if self._portal_senders("fotocasa"):
            fotocasa_urls = fotocasa_source.listing_urls_in_text(
                body
            ) or fotocasa_source.listing_urls_in_text(subject)
            if fotocasa_urls:
                profile = SearchProfileService.resolve_profile(subject, body)
                return {
                    **base,
                    "type": "fotocasa_listings",
                    "urls": fotocasa_urls,
                    "search_profile_id": profile.id if profile else None,
                }

        if self._portal_senders("yaencontre"):
            cards = yaencontre_source.cards_in_email(body)
            if cards:
                profile = SearchProfileService.resolve_profile(subject, body)
                return {
                    **base,
                    "type": "yaencontre_listings",
                    "cards": [asdict(card) for card in cards],
                    "search_profile_id": profile.id if profile else None,
                }

        if self._portal_senders("milanuncios"):
            tracker_urls = milanuncios_source.card_tracker_urls(body)
            if tracker_urls:
                profile = SearchProfileService.resolve_profile(subject, body)
                return {
                    **base,
                    "type": "milanuncios_listings",
                    "tracker_urls": tracker_urls,
                    "search_profile_id": profile.id if profile else None,
                }

        return None

    def _gmail_search_query(self) -> str:
        """The whole Gmail X-GM-RAW query.

        The label applies to the idealista term alone. It exists to keep two
        builds off one mailbox (docs/DEV_RULES.md), and the owner's Gmail
        filter puts it on idealista mail only -- measured 2026-08-30, all 65
        fotocasa, 26 milanuncios and 29 yaencontre mails in the mailbox
        carried no label, so a query that demanded it for the portal senders
        fetched none of them, ever. The portal terms therefore stand on the
        sender alone; the UID cursor is what keeps historical portal mail
        from being reprocessed (only UIDs past it are fetched), and a
        `run_full_sync` deliberately re-reads everything, portal mail
        included. ORs are explicit and parenthesised: two adjacent `from:`
        terms mean AND to Gmail and match nothing.
        """
        idealista = "from:noresponder@idealista.com"
        label_part = self._gmail_label_query(self.folder)
        portal_terms = [f"from:{s}" for s in self._all_portal_senders()]
        if not portal_terms:
            return f"{idealista} {label_part}" if label_part else idealista
        if label_part:
            idealista = f"({idealista} {label_part})"
        return " OR ".join([idealista, *portal_terms])

    @staticmethod
    def _gmail_label_query(label: Optional[str]) -> Optional[str]:
        """Return a Gmail X-GM-RAW label query part or None.

        We reuse IMAP_FOLDER as the Gmail label name to allow running multiple builds side-by-side
        without reading the same emails.
        """
        raw = str(label or "").strip()
        if not raw:
            return None

        safe = raw.replace('"', '\\"')
        if any(ch.isspace() for ch in safe) or '"' in raw:
            return f'label:"{safe}"'
        return f"label:{safe}"

    @staticmethod
    def _parse_email_received_at(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            import email.utils

            raw = value.decode() if isinstance(value, bytes) else value
            return email.utils.parsedate_to_datetime(raw)
        except Exception as e:
            logger.warning("Failed to parse email date: %s", e)
            return None

    @staticmethod
    def _uid_file_path() -> str:
        return (
            getattr(Config, "LAST_SEEN_UID_PROPERTIES_PATH", None)
            or Config.LAST_SEEN_UID_PATH
        )

    def _get_last_seen_uid(self) -> int:
        """Read the persisted cursor. Missing file means 0; a corrupt one raises.

        Returning 0 on any error (the old behaviour) turned a corrupt cursor
        into a silent full-mailbox reprocess — issue #24.
        """
        uid_file = self._uid_file_path()
        legacy_uid_file = os.path.join(Config.BASE_DIR, ".last_seen_uid_properties")

        for path in (uid_file, legacy_uid_file):
            uid = read_uid_file(path)
            if uid is None:
                continue
            return uid
        return 0

    def _save_last_seen_uid(self, uid: int) -> bool:
        """Persist the cursor atomically. Returns False when the write failed.

        A failed write is safe: the cursor stays behind and the emails are
        re-fetched next run, where re-ingestion dedupes them.
        """
        try:
            write_uid_file(self._uid_file_path(), uid)
            return True
        except Exception as e:
            logger.error("Failed to save last UID: %s", e)
            return False

    def _advance_uid_cursor(self, email_data: Dict[str, Any]) -> None:
        """Mark one email's UID as done and persist the resulting watermark.

        Called only once the email's DB work has been committed (or the email
        needed no write at all), so the persisted cursor can never step over an
        email whose rows never landed.
        """
        cursor = self._uid_cursor
        if cursor is None:
            return
        if cursor.resolve(email_data.get("uid")) and cursor.watermark > 0:
            if self._save_last_seen_uid(cursor.watermark):
                self.last_seen_uid = cursor.watermark

    def _decode_header_value(self, value: str) -> str:
        return decode_header_value(value)

    def _extract_html_parts(self, msg) -> List[str]:
        html_parts: List[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            if msg.get_content_type() == "text/html":
                payload = msg.get_payload(decode=True)
                if payload:
                    html_parts.append(payload.decode("utf-8", errors="ignore"))
        return html_parts

    def _extract_text_parts(self, msg) -> str:
        text_parts: List[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text_parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            if msg.get_content_type() == "text/plain":
                payload = msg.get_payload(decode=True)
                if payload:
                    text_parts.append(payload.decode("utf-8", errors="ignore"))
        return "\n".join(text_parts)

    @classmethod
    def _is_listing_url(cls, url: Optional[str]) -> bool:
        """True only for a URL that identifies exactly one Idealista listing.

        A row created from anything else carries `idealista_property_id=None`,
        so it can never be deduped by id, and exact-URL matching is defeated by
        the per-email UTM parameters. Recommendation/digest/promo emails linking
        to the homepage or to a search page therefore inserted a fresh junk row
        on every full sync (issue #25). The legacy pipeline rejects those URLs
        (services/imap_service.py); this is the same gate, expressed as
        "the URL must yield a property id".
        """
        if not url:
            return False
        if "utm_link=logo" in url.lower():
            return False
        return cls._PROPERTY_ID_RE.search(url) is not None

    @staticmethod
    def _find_properties(
        idealista_id: Optional[int],
        url: Optional[str],
        profile_id: Optional[int],
        profile_agnostic_fallback: bool,
    ) -> List[Property]:
        """Look up the rows an incoming email refers to.

        The profile-scoped query stays the primary one, so a listing that
        legitimately belongs to two saved searches keeps one row per profile.
        But `SearchProfileService.resolve_profile()` auto-creates profiles from
        whatever saved-search name it can read out of the email, and price-change
        and "no longer listed" templates do not carry that name the way a listing
        email does. When the profile differs from the one the row was stored
        under, the scoped query misses and the update used to no-op silently.
        For those two email kinds we therefore fall back to a lookup by
        `idealista_property_id` alone, which is globally unique per listing
        (issue #25).
        """
        matches: List[Property] = []
        if idealista_id:
            matches = Property.query.filter_by(
                idealista_property_id=idealista_id, search_profile_id=profile_id
            ).all()
            if not matches and profile_agnostic_fallback:
                matches = Property.query.filter_by(
                    idealista_property_id=idealista_id
                ).all()
        if not matches and url:
            matches = Property.query.filter_by(
                url=url, search_profile_id=profile_id
            ).all()
            if not matches and profile_agnostic_fallback:
                matches = Property.query.filter_by(url=url).all()
        return matches

    @staticmethod
    def _infer_deal_type(subject: str, body: str, url: Optional[str]) -> str:
        text = f"{subject}\n{body}\n{url or ''}"
        return "rent" if PropertyIMAPService._RENTAL_RE.search(text or "") else "sale"

    # `_classify`, `_classify_with_rules` and `_classify_text_with_rules` went
    # with the last caller: each was a private re-implementation of the rule
    # loop or of a text order, and one of them is exactly how the raw title
    # (address and all) reached the rules. Classification lives in
    # PropertyClassificationService.classify_sources().

    @staticmethod
    def _excluded_categories() -> set[str]:
        try:
            cats = SettingsService.get_excluded_property_categories()
            return {str(c).strip().lower() for c in cats if str(c).strip()}
        except Exception:
            raw = getattr(Config, "EXCLUDED_PROPERTY_CATEGORIES", None)
            if isinstance(raw, set):
                return {str(c).strip().lower() for c in raw if str(c).strip()}
            return set()

    @staticmethod
    def _resolve_municipality_truncation(
        municipality: Optional[str],
    ) -> Optional[str]:
        """Resolve a municipality the alert email cut off ("Ovi...").

        Idealista truncates long location strings with an ellipsis and this
        pipeline stored them verbatim, so the /properties filter offered
        "Ovi..." and "Oviedo" as two municipalities and filtering by the full
        name missed the truncated rows (issue #298). When exactly one
        municipality already stored starts with the truncated stem, that is
        the name the email cut; anything else keeps the marker -- the value
        stays visibly non-canonical rather than guessed, and the filter
        options exclude it (routes/main_routes.py).
        """
        if not municipality or not is_truncated_municipality(municipality):
            return municipality

        if truncation_stem_ends_at_connective(municipality):
            # The wrong-pick shape: the stem ends at a generic connective
            # (de/del/la/...), where "exactly one stored name starts with it"
            # proves nothing -- the universe is only what ingestion has seen,
            # so "San Juan de..." for the never-stored San Juan de la Arena
            # would resolve to the stored San Juan de Alicante. Keep the
            # marker; the repair tool takes an explicit operator mapping.
            logger.warning(
                "Keeping truncated municipality %r verbatim: its stem ends at "
                "a generic connective, where a unique prefix match would pick "
                "whichever sibling ingestion happens to know",
                municipality,
            )
            return municipality

        known = [
            row[0]
            for row in db.session.query(Property.municipality).distinct().all()
            if row[0]
        ]
        resolved = resolve_truncated_municipality(municipality, known)
        if resolved:
            logger.info(
                "Resolved truncated municipality %r to stored full name %r",
                municipality,
                resolved,
            )
            return resolved
        logger.info(
            "Keeping truncated municipality %r verbatim: no unique stored "
            "full name starts with its stem",
            municipality,
        )
        return municipality

    def get_idealista_emails(
        self, max_results: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if not self.user or not self.password:
            logger.error("IMAP credentials not configured")
            return []

        email_data: List[Dict[str, Any]] = []
        self._uid_cursor = None
        # Reset here and not at the truncation site: this function returns
        # early on missing credentials and can raise before ever reaching the
        # search, and a stale value from an earlier run would report a backlog
        # that is not there.
        self._truncation = None
        self._fetch_error = None
        limit = max_results or self.max_emails
        excluded_categories = self._excluded_categories()
        sale_only = SettingsService.get_sale_only()

        skip_subjects = [
            "welcome to idealista",
            "bienvenido a idealista",
            "contactos que ha recibido",
            "you have received contacts",
            "weekly digest",
            "resumen semanal",
            "update your preferences",
            "actualiza tus preferencias",
            "respuesta de",
        ]
        price_change_subjects = [
            "price change",
            "price reduction",
            "price drop",
            "cambio de precio",
            "bajada de precio",
        ]
        no_longer_patterns = [
            "one of your favourites is no longer listed",
            "tu favorito ya no está disponible",
            "no longer listed",
            "ya no está disponible",
            "ya no está publicado",
            "is no longer available",
        ]

        try:
            with IMAPClient(
                self.host, port=self.port, ssl=self.ssl, timeout=self.timeout
            ) as client:
                client.login(self.user, self.password)
                logger.info("Connected to IMAP server as %s", self.user)

                if "gmail" in (self.host or "").lower():
                    try:
                        client.select_folder("[Gmail]/All Mail", readonly=True)
                    except Exception:
                        client.select_folder("INBOX", readonly=True)
                    gm_query = self._gmail_search_query()
                    try:
                        uids = client.search(["X-GM-RAW", gm_query])
                    except Exception:
                        uids = client.search(["ALL"])
                else:
                    client.select_folder(self.folder or "INBOX", readonly=True)
                    uids = client.search(["ALL"])

                if self.last_seen_uid > 0:
                    uids = [u for u in uids if u > self.last_seen_uid]

                # Oldest first, and that is not the defect it looks like.
                # `UidBatchCursor.watermark` advances only through *contiguous*
                # resolved UIDs from the start of the batch, so taking the
                # newest N would leave the older ones unresolved forever and
                # the cursor would never move at all. Ascending is the only
                # order that drains.
                #
                # What was wrong is that it said nothing. A capped run reported
                # the same shape as a run that had read the whole mailbox --
                # `total_emails_found` is the number *taken*, not the number
                # that matched -- so a backlog was invisible to the operator,
                # to the sync history and to the page that renders it. #98, in
                # the ingest: an absence of measurement drawn as a measurement.
                matched = sorted(uids)
                uids = matched[:limit]
                if len(matched) > len(uids):
                    self._truncation = {
                        "matched": len(matched),
                        "taken": len(uids),
                        "left": len(matched) - len(uids),
                        "oldest_left_uid": matched[len(uids)],
                        "newest_left_uid": matched[-1],
                    }
                    logger.warning(
                        "MAX_EMAILS_PER_RUN=%d capped this run: %d messages "
                        "matched, %d taken (oldest first, so the cursor can "
                        "advance), %d left for the next run — UIDs %d..%d",
                        limit,
                        self._truncation["matched"],
                        self._truncation["taken"],
                        self._truncation["left"],
                        self._truncation["oldest_left_uid"],
                        self._truncation["newest_left_uid"],
                    )
                if not uids:
                    return []

                fetch_data = client.fetch(uids, ["RFC822", "INTERNALDATE"])
                cursor = UidBatchCursor(uids, start=self.last_seen_uid)
                self._uid_cursor = cursor

                for uid in uids:
                    fetched = fetch_data.get(uid) or {}
                    raw_email = fetched.get(b"RFC822")
                    if raw_email is None:
                        # Fetch-level gap, not a parsing decision: leave the UID
                        # unresolved so the next run fetches it again instead of
                        # the cursor stepping over it.
                        logger.error("IMAP fetch returned no body for UID %s", uid)
                        continue

                    # Emails we hand back still need DB work, so they stay
                    # unresolved until run_ingestion() commits them. Emails we
                    # deliberately filtered out are done right here.
                    #
                    # An email that *raised* is neither: the failure may be
                    # transient (a parser bug, a bad decode, a momentary
                    # dependency error), and resolving its UID would drop a real
                    # listing exactly the way #24 dropped them. `failed` keeps
                    # the cursor behind such an email so the next run re-reads
                    # it. A permanently broken email therefore holds the cursor
                    # and says so in the log - the trade #24 already chose:
                    # visibly stuck beats silently lost.
                    emitted = False
                    failed = False
                    try:
                        msg = message_from_bytes(raw_email)

                        html_parts = self._extract_html_parts(msg)
                        body = "\n".join(html_parts) or self._extract_text_parts(msg)
                        if not body:
                            continue

                        subject = self._decode_header_value(msg.get("Subject", ""))
                        subject_low = subject.lower()
                        if any(s in subject_low for s in skip_subjects):
                            continue

                        internal_date = fetched.get(b"INTERNALDATE")
                        email_source_id = f"imap_{uid}"
                        email_sender = self._decode_header_value(msg.get("From", ""))

                        is_price_change = any(
                            p in subject_low for p in price_change_subjects
                        )
                        is_no_longer = any(
                            p in subject_low or p in body.lower()
                            for p in no_longer_patterns
                        )

                        url = extract_url(body) or extract_url(subject)
                        if not url:
                            # Not an idealista email. A portal alert names its
                            # listings and nothing else this loop needs: the
                            # email contributes the links (and, for
                            # yaencontre, the card fields, since that portal's
                            # pages cannot be read at all), the arrival date
                            # and the profile. Any network work happens in
                            # run_ingestion() -- never here, inside an open
                            # IMAP connection, where a batch of gated fetches
                            # would hold the mailbox session for minutes.
                            portal_entry = self._portal_email_entry(
                                uid=uid,
                                subject=subject,
                                body=body,
                                email_source_id=email_source_id,
                                internal_date=internal_date,
                                email_sender=email_sender,
                            )
                            if portal_entry is not None:
                                emitted = True
                                email_data.append(portal_entry)
                                continue
                            if any(
                                s.lower() in (email_sender or "").lower()
                                for s in self._all_portal_senders()
                            ):
                                # A portal sender whose links this parser
                                # cannot read is either a welcome/confirmation
                                # mail (fine to consume) or an alert template
                                # whose link shape is not the one this
                                # recognizes -- and that second case must be
                                # visible in the log, because the email is
                                # consumed either way and would otherwise
                                # vanish.
                                logger.warning(
                                    "UID %s from portal sender %r carries no "
                                    "recognizable listing; consuming it "
                                    "(subject: %r)",
                                    uid,
                                    email_sender,
                                    subject[:120],
                                )
                            continue
                        if not self._is_listing_url(url):
                            # Nothing downstream can dedupe such a row, so this
                            # is a deliberate skip, not a parser gap: the UID is
                            # resolved and the email never comes back.
                            logger.info(
                                "Skipping UID %s: %r is not a single-listing URL",
                                uid,
                                url[:120],
                            )
                            continue

                        deal_type = self._infer_deal_type(subject, body, url)
                        if deal_type == "rent" and sale_only:
                            continue
                        idealista_id = extract_idealista_property_id(url)
                        listing_title = extract_listing_title(
                            body, idealista_property_id=idealista_id
                        )

                        # Pre-classify using global rules so we can skip excluded categories
                        # without auto-creating SearchProfiles.
                        global_rules = (
                            SettingsService.get_property_classification_rules()
                        )
                        category, subtype = (
                            PropertyClassificationService.classify_sources(
                                listing_title, subject, body, global_rules
                            )
                        )

                        if category and category.strip().lower() in excluded_categories:
                            continue

                        profile = SearchProfileService.resolve_profile(subject, body)

                        if is_no_longer:
                            emitted = True
                            email_data.append(
                                {
                                    "type": "no_longer_listed",
                                    "uid": uid,
                                    "source_email_id": email_source_id,
                                    "email_received_at": internal_date,
                                    "url": url,
                                    "idealista_property_id": idealista_id,
                                    "search_profile_id": profile.id
                                    if profile
                                    else None,
                                    "deal_type": deal_type,
                                }
                            )
                            continue

                        # Re-classify using profile-specific rules (if any). This can override defaults.
                        rules = SearchProfileService.get_classification_rules(profile)
                        category_profile, subtype_profile = (
                            PropertyClassificationService.classify_sources(
                                listing_title, subject, body, rules
                            )
                        )
                        if category_profile:
                            category, subtype = category_profile, subtype_profile

                        if category and category.strip().lower() in excluded_categories:
                            continue
                        old_price_hint = None
                        new_price_hint = None
                        if is_price_change:
                            old_price_hint, new_price_hint = extract_price_change(body)

                        price = (
                            new_price_hint
                            or extract_price(subject)
                            or extract_price(body)
                        )
                        area = extract_area_m2(subject) or extract_area_m2(body)
                        attrs = extract_property_attributes(
                            body
                        ) or extract_property_attributes(subject)

                        description = (
                            self.email_parser._clean_description(body) if body else None
                        )
                        title = (
                            (listing_title or "").strip()
                            or (subject or "").strip()
                            or None
                        )
                        municipality = self._resolve_municipality_truncation(
                            extract_municipality_from_title(listing_title)
                            if listing_title
                            else None
                        )
                        if category == "land":
                            area_type = "plot"
                        elif area is not None:
                            area_type = "built"
                        else:
                            area_type = "unknown"

                        emitted = True
                        email_data.append(
                            {
                                "type": "price_change"
                                if is_price_change
                                else "listing",
                                "uid": uid,
                                "source_email_id": email_source_id,
                                "email_received_at": internal_date,
                                "email_subject": subject,
                                "email_sender": email_sender,
                                "title": title,
                                "url": url,
                                "deal_type": deal_type,
                                "price": price,
                                "previous_price_hint": old_price_hint,
                                "area": area,
                                "area_type": area_type,
                                "municipality": municipality,
                                "search_profile_id": profile.id if profile else None,
                                "property_category": category,
                                "property_subtype": subtype,
                                "description": description,
                                "attributes": attrs,
                                "idealista_property_id": idealista_id,
                            }
                        )
                    except Exception:
                        failed = True
                        logger.error(
                            "Failed to process UID %s - holding last_seen_uid "
                            "behind it so the email is re-read next run",
                            uid,
                            exc_info=True,
                        )
                    finally:
                        if not emitted and not failed:
                            self._advance_uid_cursor({"uid": uid})
        except Exception as e:
            # Swallowed on purpose -- whatever was parsed before the failure is
            # still worth ingesting, and the UID cursor only advances over rows
            # that committed. But it must not be swallowed *silently*: this
            # handler catches a login failure and a dead connection too, so a
            # run that never read the mailbox at all used to reach
            # run_ingestion()'s success branch and write `completed` with
            # `total_emails_found = 0` -- indistinguishable, in the one table
            # that records these runs, from "no new mail". #98, one layer above
            # the cap this ticket is about.
            logger.error("Failed to fetch via IMAP: %s", e)
            self._fetch_error = type(e).__name__

        return email_data

    def _enrich_new_property(self, prop: Property) -> None:
        """Everything a freshly committed row gets, whichever portal it is from.

        Extracted from the idealista branch of run_ingestion() verbatim when
        the fotocasa branch arrived, so the two sources cannot drift: a
        fotocasa row gets the same geocode-or-travel decision (its portal pin
        makes `ensure_coordinates` return early, so the geocode is a no-op
        there), the same sea distance, the same free pass and the same
        scoring flag. Every step commits on its own and swallows its own
        failure -- nothing in here may fail ingestion or hold the UID cursor.

        The paid step, and the cheap one that has to survive it.

        `AUTO_TRAVEL_ENRICHMENT` is off by default since 2026-08-17 (see
        config.py for what the old default cost). It used to be the only
        thing here, and it geocoded the row on its way to Google --
        `calculate_for_property` opens with `ensure_coordinates`. So
        switching it off would silently take the coordinate with it, and
        with the coordinate go the sea distance, the sea-view verdict, the
        OSM amenities and the quality-of-life block below: four free
        measurements lost to a flag about a paid one, and a row that reads
        "nothing nearby" when the truth is "nobody looked". That is #98's
        defect arriving through the back door of a cost control.

        Hence the `elif`, and not a second unconditional call: when travel
        runs it has already geocoded the row, and `ensure_coordinates`
        returns early on a row that has coordinates, so a second call would
        be free and pointless -- but the two branches state the intent, which
        is that exactly one geocode happens per new listing.
        """
        if getattr(Config, "AUTO_TRAVEL_ENRICHMENT", False):
            try:
                from services.property_travel_service import (
                    PropertyTravelService,
                )

                travel_service = PropertyTravelService()
                # `AUTO_TRAVEL_ENRICHMENT=true` is the one standing
                # decision that lets an unattended loop spend, and
                # it is off by default for what it cost. Naming it
                # in the authorization puts it in the ledger under
                # the flag that permitted it, so an invoice spike
                # from ingestion is attributable to a setting
                # rather than to a mystery.
                with authorized_spend(
                    f"ingest: AUTO_TRAVEL_ENRICHMENT for property {prop.id}",
                    actor="ingest:property_imap",
                    cap_units=CAP_INGEST_TRAVEL,
                ):
                    travel_service.calculate_for_property(prop, commit=True)
            except Exception as enrich_error:
                logger.warning(
                    "Property travel enrichment failed for %s: %s",
                    prop.id,
                    enrich_error,
                )
                db.session.rollback()
        elif getattr(Config, "AUTO_GEOCODING", True):
            try:
                from services.property_location_service import (
                    PropertyLocationService,
                )

                # The one paid call the free pass cannot do
                # without, at $0.005 a listing and about $1 a
                # month here. A standing owner decision, recorded
                # in `config.py` -- so it is authorized by name
                # and lands in the ledger, rather than being the
                # single billed call in the tree that answers to
                # nobody.
                with authorized_spend(
                    f"ingest: AUTO_GEOCODING for property {prop.id}",
                    actor="ingest:property_imap",
                    cap_units=CAP_INGEST_GEOCODE,
                ):
                    PropertyLocationService().ensure_coordinates(prop)
                # `ensure_coordinates` writes to the instance and
                # leaves the commit to its caller, exactly as
                # `utils/refresh_property_accuracy.py` does. It
                # commits either way: a refusal is recorded on the
                # row as a refusal (`enrichment["geocoding"]
                # ["refused"]`), and that record is what stops the
                # next run paying to re-ask the same question.
                db.session.commit()
            except Exception as geocode_error:
                logger.warning(
                    "Geocoding failed for %s: %s",
                    prop.id,
                    geocode_error,
                )
                db.session.rollback()

        # Measured before scoring so the fresh score already accounts
        # for it, and committed on its own: neither the travel flag
        # above nor the scoring flag below may decide whether this
        # result reaches the database.
        if getattr(Config, "SEA_DISTANCE_ENABLED", True):
            try:
                from services.sea_distance_service import (
                    SeaDistanceService,
                )

                SeaDistanceService().update_property(prop, commit=True)
            except Exception as sea_error:
                logger.warning(
                    "Sea distance measurement failed for %s: %s",
                    prop.id,
                    sea_error,
                )
                db.session.rollback()

        # The free pass: OSM amenities (#152), quality of life
        # (#275) and the sea-view verdict. Ingestion ran the paid
        # enrichers and skipped these, so every new row arrived
        # with no Extended Infrastructure card, no QoL block and
        # no sea-view verdict (#299). It runs after the travel
        # step because that is what geocodes the row, and after
        # the sea-distance step because that warms the per-cell
        # coastline cache the sea-view geometry reads. Each step
        # is paced by its transport's own gate, commits on its
        # own, and records a refusal as a refusal; nothing in it
        # may fail ingestion or hold the UID cursor back.
        #
        # `use_ai=False` is the load-bearing argument here. The
        # sea-view text signal can ask the owner's Claude
        # subscription what a mention of the sea means, and that
        # is a cold CLI run with a 600 s timeout (#201). This
        # loop runs unattended, twice a night, over however many
        # alert emails arrived; "vistas al mar" is ordinary
        # listing prose in Asturias and Galicia, so leaving the
        # default on would mean minutes of subscription work per
        # batch that nobody pressed a button for.
        # `utils/backfill_sea_view.py` has had `--no-ai` since it
        # was written, and an unattended ingest must not be
        # bolder than a backfill. The keyword path records that
        # it took it (`source: "keywords_only"`); the Enrich
        # button, where there is a press, still uses the AI.
        if getattr(Config, "FREE_ENRICHMENT_ENABLED", True):
            try:
                from services.property_enrichment_service import (
                    PropertyEnrichmentService,
                )

                PropertyEnrichmentService().enrich_free_sources(
                    prop, commit=True, use_ai=False
                )
            except Exception as free_error:
                logger.warning(
                    "Free enrichment failed for %s: %s",
                    prop.id,
                    free_error,
                )
                db.session.rollback()

        if getattr(Config, "AUTO_PROPERTY_SCORING", False):
            try:
                from services.property_scoring_service import (
                    PropertyScoringService,
                )

                scoring_service = PropertyScoringService()
                scoring_service.calculate_for_property(prop, commit=True)
            except Exception as scoring_error:
                logger.warning(
                    "Property scoring failed for %s: %s",
                    prop.id,
                    scoring_error,
                )
                db.session.rollback()

    def _ingest_fotocasa_email(
        self,
        email_data: Dict[str, Any],
        *,
        sale_only: bool,
        excluded_categories: set[str],
    ) -> tuple[int, bool]:
        """Create rows for the fotocasa listings one alert email names.

        Returns ``(created, all_done)``. ``all_done=False`` means at least one
        listing page could not be read for a reason that may pass -- fotocasa's
        "SENTIMOS LA INTERRUPCIÓN" block (a 200, `services/fotocasa_source.py`),
        a timeout, an HTTP error, or a payload this parser cannot read -- so
        the caller must hold the UID cursor behind this email and let the next
        run re-read it, exactly as #24 holds it for an idealista email that
        raised. Every row committed before the failure is safe to re-meet: the
        `fotocasa:<id>` dedup key makes the re-run skip it without a fetch.

        The email supplies only the links, the arrival date and the profile;
        every listing field comes off the listing page, through the same
        `preview_row`/`build_property` pair the paste-links import uses --
        one builder, so the two doors cannot disagree about the dedup key,
        the NULL `listing_status_source` or the portal pin.

        A page that answers "this URL no longer serves that listing"
        (`not_the_listing_page`: the advert was taken down and the server
        redirected) is a consumed skip, not a held one -- the server answered,
        and re-asking tomorrow gets the same answer, so holding the cursor on
        it would stall ingestion forever over a listing that is gone.
        """
        import requests

        from services import fotocasa_import

        http = requests.Session()
        created = 0
        all_done = True

        for url in email_data.get("urls") or []:
            listing_id = fotocasa_source.listing_id_from_url(url)
            if listing_id is None:
                continue

            # Before the fetch, so a listing already here through either door
            # (this ingester or the paste-links import) costs no request and
            # no gate wait. The key both doors write is `fotocasa:<id>`.
            if fotocasa_import.existing_by_listing_id(listing_id) is not None:
                continue

            if self._portal_fetching_stopped("fotocasa", email_data):
                all_done = False
                break

            listing = fotocasa_source.fetch_listing(url, session=http)
            if not listing.ok:
                if listing.refusal == fotocasa_source.REFUSAL_NOT_A_LISTING:
                    logger.info(
                        "Fotocasa listing %s from %s is no longer served at its "
                        "URL; skipping it for good",
                        listing_id,
                        email_data.get("source_email_id"),
                    )
                    continue
                self._count_portal_refusal(
                    "fotocasa", listing_id, listing.refusal, email_data
                )
                all_done = False
                continue

            self._portal_refusals["fotocasa"] = 0

            if self._create_portal_row(
                fotocasa_import.preview_row(listing),
                source="fotocasa",
                email_data=email_data,
                sale_only=sale_only,
                excluded_categories=excluded_categories,
            ):
                created += 1

        return created, all_done

    def _portal_fetching_stopped(self, portal: str, email_data: Dict[str, Any]) -> bool:
        """True when this run has already given up on the portal's host."""
        refusals = self._portal_refusals.get(portal, 0)
        if refusals < PORTAL_MAX_CONSECUTIVE_REFUSALS:
            return False
        logger.warning(
            "Skipping the remaining %s fetches this run after %s refusals in "
            "a row; UID %s is held and re-read next run",
            portal,
            refusals,
            email_data.get("uid"),
        )
        return True

    def _count_portal_refusal(
        self, portal: str, listing_id: Any, reason: Any, email_data: Dict[str, Any]
    ) -> None:
        self._portal_refusals[portal] = self._portal_refusals.get(portal, 0) + 1
        logger.warning(
            "%s refused listing %s (%s); holding UID %s for the next run",
            portal,
            listing_id,
            reason,
            email_data.get("uid"),
        )

    def _create_portal_row(
        self,
        row: Dict[str, Any],
        *,
        source: str,
        email_data: Dict[str, Any],
        sale_only: bool,
        excluded_categories: set[str],
        record_advertiser: bool = True,
    ) -> bool:
        """The shared tail of every portal ingest: filter, build, commit, enrich.

        Returns True when a row landed. The rent and excluded-category skips
        are deliberate filters (a consumed email, never a held one), and the
        IntegrityError branch is the ordinary duplicate outcome arriving a
        moment late from a concurrent writer -- the import page, another run.
        """
        from services import fotocasa_import

        profile_id = email_data.get("search_profile_id")
        email_date = self._parse_email_received_at(email_data.get("email_received_at"))

        deal_type = (row.get("deal_type") or "sale").strip().lower()
        if deal_type == "rent" and sale_only:
            return False

        classification = fotocasa_import.classify_row(row, profile_id)
        category = (classification[0] or "").strip().lower()
        if category and category in excluded_categories:
            return False

        prop = fotocasa_import.build_property(
            row,
            profile_id=profile_id,
            classification=classification,
            method="alert_email",
            email_date=email_date,
            email_subject=email_data.get("email_subject"),
            email_sender=email_data.get("email_sender"),
            source=source,
            record_advertiser=record_advertiser,
        )
        try:
            db.session.add(prop)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return False

        self._enrich_new_property(prop)
        return True

    def _ingest_yaencontre_email(
        self,
        email_data: Dict[str, Any],
        *,
        sale_only: bool,
        excluded_categories: set[str],
    ) -> tuple[int, bool]:
        """Create rows for the yaencontre cards one alert email carries.

        No network at all: the portal answers DataDome to these machines
        (measured 2026-08-30, module docstring of
        `services/yaencontre_source.py`), so the card fields ARE the row --
        no coordinates, no seller verdict (`record_advertiser=False`), and
        the geocoder fills the location from the title at ingest. With no
        fetches there are no refusals, so `all_done` is always True; an
        exception is #24's territory and holds the cursor through the
        caller's own except branch.
        """
        from services import fotocasa_import

        created = 0
        for card in email_data.get("cards") or []:
            listing_id = card.get("listing_id")
            if not listing_id:
                continue
            if (
                fotocasa_import.existing_by_listing_id(
                    int(listing_id), yaencontre_source.SOURCE_NAME
                )
                is not None
            ):
                continue
            if self._create_portal_row(
                dict(card),
                source=yaencontre_source.SOURCE_NAME,
                email_data=email_data,
                sale_only=sale_only,
                excluded_categories=excluded_categories,
                record_advertiser=False,
            ):
                created += 1
        return created, True

    def _ingest_milanuncios_email(
        self,
        email_data: Dict[str, Any],
        *,
        sale_only: bool,
        excluded_categories: set[str],
    ) -> tuple[int, bool]:
        """Create rows for the milanuncios cards one digest email names.

        The email carries no direct listing URLs -- every card anchor is an
        opaque SparkPost tracker -- so identity costs one redirect read per
        card (`resolve_tracker`, redirects OFF) before the dedup check, and
        the page fetch after it supplies every field, fotocasa-style. The
        refusal semantics are fotocasa's too: a tracker or page that did not
        answer holds the UID cursor; a page that answered "gone" (404, a
        redirect to a different ad) or "this is a demand ad, not a property
        for sale" is consumed, because tomorrow's answer is the same.
        """
        import requests

        from services import fotocasa_import

        http = requests.Session()
        created = 0
        all_done = True
        seen_ids: set = set()

        for tracker in email_data.get("tracker_urls") or []:
            if self._portal_fetching_stopped("milanuncios", email_data):
                all_done = False
                break

            target = milanuncios_source.resolve_tracker(tracker, session=http)
            if target is None:
                self._count_portal_refusal(
                    "milanuncios", "unresolved-tracker", "no_redirect", email_data
                )
                all_done = False
                continue

            listing_id = milanuncios_source.listing_id_from_url(target)
            if listing_id is None or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)
            if (
                fotocasa_import.existing_by_listing_id(
                    listing_id, milanuncios_source.SOURCE_NAME
                )
                is not None
            ):
                continue

            row = milanuncios_source.fetch_listing(target, session=http)
            if row.get("status") == "refused":
                reason = row.get("reason")
                if reason in (
                    milanuncios_source.REFUSAL_NOT_A_LISTING,
                    milanuncios_source.REFUSAL_NOT_SUPPLY,
                ):
                    logger.info(
                        "Milanuncios listing %s from %s is %s; skipping it for good",
                        listing_id,
                        email_data.get("source_email_id"),
                        reason,
                    )
                    continue
                self._count_portal_refusal(
                    "milanuncios", listing_id, reason, email_data
                )
                all_done = False
                continue

            self._portal_refusals["milanuncios"] = 0

            if self._create_portal_row(
                row,
                source=milanuncios_source.SOURCE_NAME,
                email_data=email_data,
                sale_only=sale_only,
                excluded_categories=excluded_categories,
            ):
                created += 1

        return created, all_done

    def run_ingestion(self, sync_type: str = "incremental") -> int:
        start_time = datetime.now(timezone.utc)
        sync_history = SyncHistory(
            sync_type=sync_type, backend="imap", started_at=start_time
        )
        db.session.add(sync_history)
        db.session.commit()

        processed_count = 0
        price_updated_count = 0
        expired_count = 0
        sale_only = SettingsService.get_sale_only()
        excluded_categories = self._excluded_categories()

        try:
            self._uid_cursor = None
            self._portal_refusals = {}
            # `_truncation` and `_fetch_error` are deliberately NOT reset here:
            # `get_idealista_emails` is their only writer and resets them at its
            # top, and a second reset makes each individually unfalsifiable --
            # measured, dropping either one alone left the suite green.
            emails = self.get_idealista_emails()
            sync_history.total_emails_found = len(emails)

            for email_data in emails:
                email_failed = False
                try:
                    profile_id = email_data.get("search_profile_id")
                    deal_type = (email_data.get("deal_type") or "sale").strip().lower()
                    if deal_type == "rent" and sale_only:
                        continue
                    if (
                        email_data.get("type") or ""
                    ).strip().lower() != "no_longer_listed":
                        category = (
                            (email_data.get("property_category") or "").strip().lower()
                        )
                        if category and category in excluded_categories:
                            continue
                    portal_handlers = {
                        "fotocasa_listings": self._ingest_fotocasa_email,
                        "yaencontre_listings": self._ingest_yaencontre_email,
                        "milanuncios_listings": self._ingest_milanuncios_email,
                    }
                    handler = portal_handlers.get(
                        (email_data.get("type") or "").strip().lower()
                    )
                    if handler is not None:
                        created_here, all_done = handler(
                            email_data,
                            sale_only=sale_only,
                            excluded_categories=excluded_categories,
                        )
                        processed_count += created_here
                        if not all_done:
                            # The finally below then holds the UID cursor, so
                            # the unread listings come back next run.
                            email_failed = True
                        continue
                    if email_data.get("type") == "no_longer_listed":
                        url = email_data.get("url")
                        idealista_id = email_data.get(
                            "idealista_property_id"
                        ) or extract_idealista_property_id(url)
                        # A delisting is a fact about the listing, not about the
                        # saved search it arrived through: match across profiles
                        # rather than let a mismatched profile leave a removed
                        # property sitting there as "active" (issue #25).
                        matches: List[Property] = self._find_properties(
                            idealista_id,
                            url,
                            profile_id,
                            profile_agnostic_fallback=True,
                        )
                        if not matches:
                            logger.info(
                                "No property matched a 'no longer listed' email "
                                "(idealista_property_id=%s, url=%s)",
                                idealista_id,
                                url,
                            )

                        updated = 0
                        for prop in matches:
                            if idealista_id and prop.idealista_property_id is None:
                                prop.idealista_property_id = idealista_id

                            if prop.listing_status != "active":
                                continue

                            prop.listing_status = "removed"
                            prop.listing_removed_date = datetime.now(timezone.utc)
                            # idealista said so itself, in mail addressed to the
                            # owner. With the scraper blocked this is the only
                            # source that observes anything, so the page has to
                            # be able to tell it apart from a hand-set status.
                            prop.listing_status_source = "email"
                            updated += 1
                            expired_count += 1

                        if updated:
                            db.session.commit()
                        continue

                    url = email_data.get("url")
                    idealista_id = email_data.get(
                        "idealista_property_id"
                    ) or extract_idealista_property_id(url)

                    is_price_change = (
                        email_data.get("type") or ""
                    ).strip().lower() == "price_change"

                    # Only price-change emails look across profiles: a plain
                    # listing email for a second saved search is supposed to get
                    # its own row (see test_property_profile_dedup.py), and the
                    # two cases are indistinguishable at this point.
                    existing: List[Property] = self._find_properties(
                        idealista_id,
                        url,
                        profile_id,
                        profile_agnostic_fallback=is_price_change,
                    )

                    raw_price = email_data.get("price")
                    new_price = None
                    if raw_price is not None:
                        try:
                            candidate_price = float(raw_price)
                        except (TypeError, ValueError):
                            candidate_price = None
                        # A parsed price of 0 (or lower) is never a real price signal —
                        # treat it the same as "no price" so re-ingestion can never
                        # zero out an existing stored price (regression guard,
                        # mirrors the legacy imap_service.py Land pipeline).
                        if candidate_price is not None and candidate_price > 0:
                            new_price = candidate_price

                    if existing and new_price is not None:
                        previous_price_hint = email_data.get("previous_price_hint")
                        email_date_obj = self._parse_email_received_at(
                            email_data.get("email_received_at")
                        )
                        any_updated = False
                        for prop in existing:
                            if idealista_id and prop.idealista_property_id is None:
                                prop.idealista_property_id = idealista_id

                            old_price = float(prop.price) if prop.price else None
                            if old_price is None:
                                # Backfill missing price (rare) or create a price history baseline
                                # using the hint from the email if available.
                                hint = (
                                    float(previous_price_hint)
                                    if previous_price_hint is not None
                                    else None
                                )
                                prop.previous_price = hint
                                prop.price = new_price
                                if hint is not None and hint > 0:
                                    price_change = new_price - hint
                                    prop.price_change_amount = price_change
                                    prop.price_change_percentage = (
                                        price_change / hint
                                    ) * 100
                                prop.price_changed_date = datetime.now(timezone.utc)
                                prop.email_date = email_date_obj
                                any_updated = True
                                continue

                            if new_price == old_price:
                                continue

                            price_change = new_price - old_price
                            price_change_percentage = (
                                (price_change / old_price) * 100 if old_price > 0 else 0
                            )

                            prop.previous_price = old_price
                            prop.price = new_price
                            prop.price_change_amount = price_change
                            prop.price_change_percentage = price_change_percentage
                            prop.price_changed_date = datetime.now(timezone.utc)
                            prop.email_date = email_date_obj
                            any_updated = True

                        if any_updated:
                            if getattr(Config, "AUTO_PROPERTY_SCORING", False):
                                try:
                                    from services.property_scoring_service import (
                                        PropertyScoringService,
                                    )

                                    scoring_service = PropertyScoringService()
                                    for prop in existing:
                                        scoring_service.calculate_for_property(
                                            prop, commit=False
                                        )
                                except Exception as scoring_error:
                                    logger.warning(
                                        "Property scoring failed after price update for %s: %s",
                                        idealista_id or url,
                                        scoring_error,
                                    )
                            db.session.commit()
                            price_updated_count += 1
                            continue

                    if existing:
                        continue

                    if is_price_change:
                        # Price-change alerts are not listing-specific: their
                        # body describes the change, not the property. Creating a
                        # row from one produces a thin duplicate of a listing we
                        # already track under another profile, and cross-ingests
                        # listings from unrelated saved searches. The legacy
                        # pipeline refuses this for the same reason
                        # (services/imap_service.py) - issue #25.
                        logger.info(
                            "Skipping price-change email for an untracked listing "
                            "(idealista_property_id=%s, url=%s)",
                            idealista_id,
                            url,
                        )
                        continue

                    if not idealista_id:
                        # No dedup key: such a row is re-inserted on every full
                        # sync because the URL carries per-email UTM parameters.
                        logger.info(
                            "Skipping email %s: no Idealista listing id in %s",
                            email_data.get("source_email_id"),
                            url,
                        )
                        continue

                    email_date = self._parse_email_received_at(
                        email_data.get("email_received_at")
                    )

                    prop = Property()
                    prop.source_email_id = email_data["source_email_id"]
                    prop.idealista_property_id = idealista_id
                    prop.email_subject = email_data.get("email_subject")
                    prop.email_sender = email_data.get("email_sender")
                    prop.title = email_data.get("title")
                    prop.url = email_data.get("url")
                    prop.deal_type = deal_type
                    prop.price = email_data.get("price")
                    prop.area = email_data.get("area")
                    prop.area_type = email_data.get("area_type") or "unknown"
                    prop.municipality = email_data.get("municipality")
                    prop.search_profile_id = email_data.get("search_profile_id")
                    prop.property_category = email_data.get("property_category")
                    prop.property_subtype = email_data.get("property_subtype")
                    prop.description = email_data.get("description")
                    prop.attributes = email_data.get("attributes")
                    prop.email_date = email_date

                    db.session.add(prop)
                    db.session.commit()
                    processed_count += 1

                    self._enrich_new_property(prop)
                except Exception as e:
                    email_failed = True
                    logger.error(
                        "Failed to process email %s: %s",
                        email_data.get("source_email_id"),
                        e,
                    )
                    db.session.rollback()
                    continue
                finally:
                    # The cursor may only pass an email whose DB work landed;
                    # a failed one holds it back so the next run re-fetches the
                    # unprocessed tail (issue #24).
                    if email_failed:
                        logger.error(
                            "Holding last_seen_uid at %s: email %s was not persisted",
                            self.last_seen_uid,
                            email_data.get("source_email_id"),
                        )
                    else:
                        self._advance_uid_cursor(email_data)

            sync_history.new_properties_added = processed_count
            sync_history.price_updated_count = price_updated_count
            sync_history.expired_count = expired_count
            # `partial` is this table's existing word for "did what it could",
            # and a capped run is exactly that: the mail it did not read is
            # still there and the next run takes it. Recorded rather than only
            # logged, because the log is not what anyone reads afterwards --
            # and `total_emails_found` above counts what was *taken*, so
            # without this the row is indistinguishable from a full sweep.
            if self._fetch_error:
                # The read did not finish, so whatever came back is not the
                # mailbox. This outranks the cap's disclosure: a capped run is
                # one that stopped where it was told to, and this one stopped
                # where it broke.
                sync_history.status = "failed"
                sync_history.error_message = (
                    f"The IMAP read did not complete ({self._fetch_error}); "
                    f"{len(emails)} message(s) were parsed before it stopped"
                )
            elif self._truncation:
                sync_history.status = "partial"
                sync_history.error_message = (
                    f"MAX_EMAILS_PER_RUN={self.max_emails} capped this run: "
                    f"{self._truncation['matched']} matched, "
                    f"{self._truncation['taken']} read, "
                    f"{self._truncation['left']} left for the next run "
                    f"(UIDs {self._truncation['oldest_left_uid']}.."
                    f"{self._truncation['newest_left_uid']})"
                )
            else:
                sync_history.status = "completed"
            sync_history.completed_at = datetime.now(timezone.utc)
            sync_history.sync_duration = int(
                (datetime.now(timezone.utc) - start_time).total_seconds()
            )
            db.session.commit()
            return processed_count

        except Exception as e:
            logger.error("Property IMAP ingestion failed: %s", e)
            sync_history.status = "failed"
            sync_history.error_message = "Property IMAP ingestion failed"
            sync_history.completed_at = datetime.now(timezone.utc)
            sync_history.sync_duration = int(
                (datetime.now(timezone.utc) - start_time).total_seconds()
            )
            db.session.commit()
            return 0

    def run_full_sync(self) -> int:
        """Run a full synchronization - reset last seen UID and process all emails."""
        logger.info("Starting full properties email synchronization")
        self.last_seen_uid = 0
        self._save_last_seen_uid(0)
        return self.run_ingestion(sync_type="full")
