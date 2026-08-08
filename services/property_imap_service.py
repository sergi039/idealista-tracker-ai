import logging
import os
import re
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from typing import Any, Dict, List, Optional, Tuple

from imapclient import IMAPClient

from app import db
from config import Config
from models import Property, SyncHistory
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
)

logger = logging.getLogger(__name__)


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
        try:
            decoded_parts = decode_header(value)
            result = []
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    if encoding:
                        result.append(part.decode(encoding, errors="ignore"))
                    else:
                        result.append(part.decode("utf-8", errors="ignore"))
                else:
                    result.append(part)
            return " ".join(result)
        except Exception:
            return value

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

    @staticmethod
    def _infer_deal_type(subject: str, body: str, url: Optional[str]) -> str:
        text = f"{subject}\n{body}\n{url or ''}"
        return "rent" if PropertyIMAPService._RENTAL_RE.search(text or "") else "sale"

    def _classify(self, subject: str, body: str) -> Tuple[Optional[str], Optional[str]]:
        text = f"{subject}\n{body}"
        # Rules can be overridden per SearchProfile; this method will be called with
        # the currently selected profile in get_idealista_emails().
        rules = SettingsService.get_property_classification_rules()
        for rule in rules:
            pattern = rule.get("pattern")
            if not pattern:
                continue
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return rule.get("category"), rule.get("subtype")
            except re.error:
                continue
        return None, None

    def _classify_with_rules(
        self, subject: str, body: str, rules: List[Dict[str, Any]]
    ) -> Tuple[Optional[str], Optional[str]]:
        return self._classify_text_with_rules(f"{subject}\n{body}", rules)

    def _classify_text_with_rules(
        self, text: str, rules: List[Dict[str, Any]]
    ) -> Tuple[Optional[str], Optional[str]]:
        return PropertyClassificationService.classify_text(text or "", rules)

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

    def get_idealista_emails(
        self, max_results: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if not self.user or not self.password:
            logger.error("IMAP credentials not configured")
            return []

        email_data: List[Dict[str, Any]] = []
        self._uid_cursor = None
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
                    gm_query = "from:noresponder@idealista.com"
                    label_part = self._gmail_label_query(self.folder)
                    if label_part:
                        gm_query = f"{gm_query} {label_part}"
                    try:
                        uids = client.search(["X-GM-RAW", gm_query])
                    except Exception:
                        uids = client.search(["ALL"])
                else:
                    client.select_folder(self.folder or "INBOX", readonly=True)
                    uids = client.search(["ALL"])

                if self.last_seen_uid > 0:
                    uids = [u for u in uids if u > self.last_seen_uid]

                uids = sorted(uids)[:limit]
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
                        email_sender = msg.get("From")

                        is_price_change = any(
                            p in subject_low for p in price_change_subjects
                        )
                        is_no_longer = any(
                            p in subject_low or p in body.lower()
                            for p in no_longer_patterns
                        )

                        url = extract_url(body) or extract_url(subject)
                        if not url:
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
                        category, subtype = self._classify_text_with_rules(
                            listing_title or "", rules=global_rules
                        )
                        if not category:
                            category, subtype = self._classify_text_with_rules(
                                subject or "", rules=global_rules
                            )
                        if not category:
                            category, subtype = self._classify_text_with_rules(
                                body or "", rules=global_rules
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
                            self._classify_text_with_rules(
                                listing_title or "", rules=rules
                            )
                        )
                        if not category_profile:
                            category_profile, subtype_profile = (
                                self._classify_text_with_rules(
                                    subject or "", rules=rules
                                )
                            )
                        if not category_profile:
                            category_profile, subtype_profile = (
                                self._classify_text_with_rules(body or "", rules=rules)
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
                        municipality = (
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
                    except Exception as e:
                        failed = True
                        logger.error(
                            "Failed to process UID %s: %s - holding last_seen_uid "
                            "behind it so the email is re-read next run",
                            uid,
                            e,
                        )
                    finally:
                        if not emitted and not failed:
                            self._advance_uid_cursor({"uid": uid})
        except Exception as e:
            logger.error("Failed to fetch via IMAP: %s", e)

        return email_data

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
                    if email_data.get("type") == "no_longer_listed":
                        url = email_data.get("url")
                        idealista_id = email_data.get(
                            "idealista_property_id"
                        ) or extract_idealista_property_id(url)
                        matches: List[Property] = []
                        if idealista_id:
                            matches = Property.query.filter_by(
                                idealista_property_id=idealista_id,
                                search_profile_id=profile_id,
                            ).all()
                        if not matches and url:
                            matches = Property.query.filter_by(
                                url=url, search_profile_id=profile_id
                            ).all()

                        updated = 0
                        for prop in matches:
                            if idealista_id and prop.idealista_property_id is None:
                                prop.idealista_property_id = idealista_id

                            if prop.listing_status != "active":
                                continue

                            prop.listing_status = "removed"
                            prop.listing_removed_date = datetime.now(timezone.utc)
                            updated += 1
                            expired_count += 1

                        if updated:
                            db.session.commit()
                        continue

                    url = email_data.get("url")
                    idealista_id = email_data.get(
                        "idealista_property_id"
                    ) or extract_idealista_property_id(url)

                    existing: List[Property] = []
                    if idealista_id:
                        existing = Property.query.filter_by(
                            idealista_property_id=idealista_id,
                            search_profile_id=profile_id,
                        ).all()
                    if not existing and url:
                        existing = Property.query.filter_by(
                            url=url, search_profile_id=profile_id
                        ).all()

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

                    # If the first email we ingest is a price change, keep a baseline.
                    if email_data.get("type") == "price_change":
                        hint = email_data.get("previous_price_hint")
                        if hint is not None and prop.price is not None:
                            try:
                                hint_f = float(hint)
                                new_f = float(prop.price)
                                prop.previous_price = hint_f
                                prop.price_change_amount = new_f - hint_f
                                prop.price_change_percentage = (
                                    (prop.price_change_amount / hint_f) * 100
                                    if hint_f > 0
                                    else None
                                )
                                prop.price_changed_date = datetime.now(timezone.utc)
                            except Exception:
                                pass

                    db.session.add(prop)
                    db.session.commit()
                    processed_count += 1

                    if getattr(Config, "AUTO_TRAVEL_ENRICHMENT", False):
                        try:
                            from services.property_travel_service import (
                                PropertyTravelService,
                            )

                            travel_service = PropertyTravelService()
                            travel_service.calculate_for_property(prop, commit=True)
                        except Exception as enrich_error:
                            logger.warning(
                                "Property travel enrichment failed for %s: %s",
                                prop.id,
                                enrich_error,
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
