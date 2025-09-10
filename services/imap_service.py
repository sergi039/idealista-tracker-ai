import os
import logging
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from imapclient import IMAPClient
from email import message_from_bytes
from email.header import decode_header
from utils.email_parser import EmailParser
from models import Land, SyncHistory
from app import db
from config import Config

logger = logging.getLogger(__name__)

class IMAPService:
    def __init__(self):
        self.host = Config.IMAP_HOST
        self.port = Config.IMAP_PORT
        self.ssl = Config.IMAP_SSL
        self.user = Config.IMAP_USER
        self.password = Config.IMAP_PASSWORD
        self.folder = Config.IMAP_FOLDER
        self.search_query = Config.IMAP_SEARCH_QUERY
        self.max_emails = Config.MAX_EMAILS_PER_RUN
        self.email_parser = EmailParser()
        self.last_seen_uid = self._get_last_seen_uid()
    
    def _get_last_seen_uid(self) -> int:
        """Get the last processed UID from database to avoid reprocessing"""
        try:
            # Check if we have a settings table or use a simple file
            uid_file = ".last_seen_uid"
            if os.path.exists(uid_file):
                with open(uid_file, 'r') as f:
                    return int(f.read().strip() or "0")
            return 0
        except Exception:
            return 0
    
    def _save_last_seen_uid(self, uid: int):
        """Save the last processed UID"""
        try:
            with open(".last_seen_uid", 'w') as f:
                f.write(str(uid))
        except Exception as e:
            logger.error(f"Failed to save last UID: {e}")
    
    def authenticate(self) -> bool:
        """Test IMAP connection and authentication"""
        try:
            if not self.user or not self.password:
                logger.error("IMAP credentials not configured")
                return False
            
            with IMAPClient(self.host, port=self.port, ssl=self.ssl) as client:
                client.login(self.user, self.password)
                logger.info(f"IMAP authentication successful for {self.user}")
                return True
        except Exception as e:
            logger.error(f"IMAP authentication failed: {str(e)}")
            return False
    
    def _decode_header_value(self, value: str) -> str:
        """Decode email header value"""
        try:
            decoded_parts = decode_header(value)
            result = []
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    if encoding:
                        result.append(part.decode(encoding, errors='ignore'))
                    else:
                        result.append(part.decode('utf-8', errors='ignore'))
                else:
                    result.append(part)
            return ' '.join(result)
        except Exception:
            return value
    
    def _extract_html_parts(self, msg) -> List[str]:
        """Extract HTML parts from email message"""
        html_parts = []
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_parts.append(payload.decode('utf-8', errors='ignore'))
        else:
            if msg.get_content_type() == "text/html":
                payload = msg.get_payload(decode=True)
                if payload:
                    html_parts.append(payload.decode('utf-8', errors='ignore'))
        
        return html_parts
    
    def _extract_text_parts(self, msg) -> str:
        """Extract plain text parts from email message"""
        text_parts = []
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text_parts.append(payload.decode('utf-8', errors='ignore'))
        else:
            if msg.get_content_type() == "text/plain":
                payload = msg.get_payload(decode=True)
                if payload:
                    text_parts.append(payload.decode('utf-8', errors='ignore'))
        
        return '\n'.join(text_parts)
    
    def get_idealista_emails(self, max_results: int = None) -> List[Dict]:
        """Fetch and parse Idealista emails via IMAP"""
        if not self.user or not self.password:
            logger.error("IMAP credentials not configured")
            return []
        email_data = []
        max_results = max_results or self.max_emails

        try:
            with IMAPClient(self.host, port=self.port, ssl=self.ssl) as client:
                client.login(self.user, self.password)
                logger.info(f"Connected to IMAP server as {self.user}")

                # Gmail: работаем из All Mail, ярлык — через X-GM-RAW
                if 'gmail' in self.host.lower():
                    try:
                        client.select_folder('[Gmail]/All Mail', readonly=True)
                        logger.info("Selected [Gmail]/All Mail")
                    except Exception:
                        client.select_folder('INBOX', readonly=True)
                        logger.info("Fallback to INBOX")
                    # Упрощенный поиск - только по отправителю
                    gm_query = 'from:noresponder@idealista.com'
                    try:
                        uids = client.search(['X-GM-RAW', gm_query])
                        logger.info(f"Gmail X-GM-RAW search found {len(uids)} emails")
                    except Exception as e:
                        logger.warning(f"X-GM-RAW not available: {e}, falling back to ALL")
                        uids = client.search(['ALL'])
                else:
                    client.select_folder(self.folder or "INBOX", readonly=True)
                    uids = client.search(['ALL'])

                logger.info(f"Total emails found: {len(uids)}")

                if self.last_seen_uid > 0:
                    uids = [u for u in uids if u > self.last_seen_uid]
                    logger.info(f"Filtering by last_seen_uid ({self.last_seen_uid}): {len(uids)} new emails")
                    
                # Ограничим первую обработку 5 письмами для теста
                uids = sorted(uids)[:5] if max_results is None else sorted(uids)[:max_results]
                if not uids:
                    logger.info("No new emails found")
                    return []

                logger.info(f"Processing {len(uids)} emails...")
                fetch_data = client.fetch(uids, ['RFC822', 'INTERNALDATE'])
                
                for uid in uids:
                    try:
                        raw_email = fetch_data[uid][b'RFC822']
                        msg = message_from_bytes(raw_email)

                        html_parts = self._extract_html_parts(msg)
                        body = '\n'.join(html_parts) or self._extract_text_parts(msg)
                        if not body:
                            logger.warning(f"No body found in email UID {uid}")
                            continue

                        subject = self._decode_header_value(msg.get('Subject', ''))
                        logger.info(f"Processing email UID {uid}: {subject[:50]}...")
                        
                        # Skip non-property emails (explicit blacklist)
                        skip_subjects = [
                            'One of your favourites is no longer listed',
                            'Tu favorito ya no está disponible',
                            'Welcome to Idealista',
                            'Bienvenido a Idealista',
                            'Contactos que ha recibido',
                            'You have received contacts',
                            'Weekly digest',
                            'Resumen semanal',
                            'Update your preferences',
                            'Actualiza tus preferencias',
                            'Respuesta de',  # Skip user responses/replies
                            'Price change',  # Skip price change notifications
                            'Cambio de precio',
                            'detached house',  # Skip house listings
                            'casa adosada',
                            'vivienda',
                            'chalet',
                            'piso',
                            'apartamento',
                            'ático',
                            'dúplex',
                            'Bilbao homes'
                        ]
                        
                        if any(skip_text in subject for skip_text in skip_subjects):
                            logger.info(f"Skipping non-property email: {subject[:50]}")
                            continue
                        
                        # Only process property listing emails (whitelist approach)
                        valid_subjects = [
                            'New plot of land in your search',
                            'Nuevo terreno en tu búsqueda',
                            'Price reduction in your search',
                            'Bajada de precio en tu búsqueda'
                        ]
                        
                        is_valid = any(valid_text in subject for valid_text in valid_subjects)
                        if not is_valid:
                            logger.warning(f"Unknown email type, skipping: {subject[:50]}")
                            continue
                        
                        # Parse email content and validate
                        email_content = {'subject': subject, 'body': body, 'message_id': f"imap_{uid}"}
                        parsed = self.email_parser.parse_idealista_email(email_content)
                        
                        if not parsed:
                            logger.warning(f"Could not parse property data from email UID {uid}")
                            continue
                            
                        # Validate URL quality - skip emails with homepage/UTM-only links
                        if parsed.get('url'):
                            url = parsed['url']
                            # Good URLs contain '/inmueble/' or '/venta-' or '/alquiler-'
                            is_property_url = any(path in url for path in ['/inmueble/', '/venta-', '/alquiler-'])
                            # Bad URLs are just homepage with UTM parameters
                            is_homepage_only = (
                                url.startswith('https://www.idealista.com/?') or
                                url.startswith('https://www.idealista.com/#') or
                                url.endswith('idealista.com/') or
                                'utm_link=logo' in url
                            )
                            
                            if is_homepage_only or not is_property_url:
                                logger.warning(f"Skipping email with invalid URL: {url[:100]}")
                                continue
                        if parsed:
                            parsed['source_email_id'] = f"imap_{uid}"
                            parsed['email_received_at'] = fetch_data[uid][b'INTERNALDATE']
                            email_data.append(parsed)
                            logger.info(f"Successfully parsed email UID {uid}")
                        else:
                            logger.warning(f"Could not parse Idealista data from email UID {uid}")
                    except Exception as e:
                        logger.error(f"Failed to process UID {uid}: {e}")
                        continue

                # Persist last seen
                if uids:
                    self.last_seen_uid = max(uids)
                    self._save_last_seen_uid(self.last_seen_uid)
                    logger.info(f"Saved last seen UID: {self.last_seen_uid}")

                logger.info(f"Successfully processed {len(email_data)} Idealista emails")

        except Exception as e:
            logger.error(f"Failed to fetch via IMAP: {e}")

        return email_data
    
    def run_ingestion(self, sync_type: str = "incremental") -> int:
        """Main method to run email ingestion via IMAP"""
        start_time = datetime.utcnow()
        
        # Create sync history record
        sync_history = SyncHistory(
            sync_type=sync_type,
            backend='imap',
            started_at=start_time
        )
        db.session.add(sync_history)
        db.session.commit()
        
        try:
            logger.info(f"Starting IMAP ingestion process ({sync_type})")
            
            # Fetch and parse emails
            emails = self.get_idealista_emails()
            sync_history.total_emails_found = len(emails)
            
            if not emails:
                logger.warning("No emails found for ingestion")
                sync_history.new_properties_added = 0
                sync_history.status = 'completed'
                sync_history.completed_at = datetime.utcnow()
                sync_history.sync_duration = int((datetime.utcnow() - start_time).total_seconds())
                db.session.commit()
                return 0
            
            # Import here to avoid circular imports
            from services.enrichment_service import EnrichmentService
            
            processed_count = 0
            for email_data in emails:
                try:
                    # Check if email already processed
                    existing_email = Land.query.filter_by(
                        source_email_id=email_data['source_email_id']
                    ).first()
                    
                    if existing_email:
                        logger.debug(f"Email {email_data['source_email_id']} already processed")
                        continue
                    
                    # Check if property already exists by URL (for price updates)
                    existing_property = None
                    if email_data.get('url'):
                        existing_property = Land.query.filter_by(
                            url=email_data['url']
                        ).first()
                    
                    # If property exists, update price if changed
                    if existing_property and email_data.get('price'):
                        new_price = float(email_data['price'])
                        old_price = float(existing_property.price) if existing_property.price else None
                        
                        if old_price and new_price != old_price:
                            # Calculate price change
                            price_change = new_price - old_price
                            price_change_percentage = (price_change / old_price) * 100 if old_price > 0 else 0
                            
                            # Update property with new price information
                            existing_property.previous_price = old_price
                            existing_property.price = new_price
                            existing_property.price_change_amount = price_change
                            existing_property.price_change_percentage = price_change_percentage
                            existing_property.price_changed_date = datetime.utcnow()
                            
                            # Parse email date if available
                            email_date_obj = None
                            if email_data.get('email_received_at'):
                                try:
                                    import email.utils
                                    # Parse IMAP INTERNALDATE format
                                    email_date_obj = email.utils.parsedate_to_datetime(
                                        email_data['email_received_at'].decode() 
                                        if isinstance(email_data['email_received_at'], bytes) 
                                        else email_data['email_received_at']
                                    )
                                except Exception as e:
                                    logger.warning(f"Failed to parse email date: {e}")
                            
                            existing_property.email_date = email_date_obj
                            
                            # Add this email ID to prevent reprocessing
                            existing_property.source_email_id = email_data['source_email_id']
                            
                            db.session.commit()
                            
                            if price_change < 0:
                                logger.info(f"Price REDUCED for {existing_property.title}: {old_price:.0f}€ → {new_price:.0f}€ ({price_change:.0f}€, {price_change_percentage:.1f}%)")
                            else:
                                logger.info(f"Price INCREASED for {existing_property.title}: {old_price:.0f}€ → {new_price:.0f}€ (+{price_change:.0f}€, +{price_change_percentage:.1f}%)")
                            
                            processed_count += 1
                            continue
                    
                    # Create new land record
                    # Parse email date if available
                    email_date = None
                    if email_data.get('email_received_at'):
                        try:
                            import email.utils
                            # Parse IMAP INTERNALDATE format
                            email_date = email.utils.parsedate_to_datetime(email_data['email_received_at'].decode() if isinstance(email_data['email_received_at'], bytes) else email_data['email_received_at'])
                        except Exception as e:
                            logger.warning(f"Failed to parse email date: {e}")
                    
                    land = Land()
                    land.source_email_id = email_data['source_email_id']
                    land.title = email_data.get('title')
                    land.url = email_data.get('url')
                    land.price = email_data.get('price')
                    land.area = email_data.get('area')
                    land.municipality = email_data.get('municipality')
                    land.land_type = email_data.get('land_type')
                    land.description = email_data.get('description')
                    land.legal_status = email_data.get('legal_status')
                    land.email_date = email_date
                    
                    db.session.add(land)
                    db.session.commit()
                    
                    # Try enrichment but continue if it fails
                    try:
                        enrichment_service = EnrichmentService()
                        enriched = enrichment_service.enrich_land(land.id)
                        if enriched:
                            logger.info(f"Successfully enriched land {land.id}")
                        else:
                            logger.warning(f"Failed to enrich land {land.id}, continuing without enrichment")
                    except Exception as enrich_error:
                        logger.warning(f"Enrichment failed for land {land.id}: {str(enrich_error)}, continuing")
                    
                    processed_count += 1
                    logger.info(f"Processed new land: {land.title}")
                    
                except Exception as e:
                    logger.error(f"Failed to process email {email_data.get('source_email_id')}: {str(e)}")
                    db.session.rollback()
                    continue
            
            # Update sync history
            sync_history.new_properties_added = processed_count
            sync_history.status = 'completed'
            sync_history.completed_at = datetime.utcnow()
            sync_history.sync_duration = int((datetime.utcnow() - start_time).total_seconds())
            db.session.commit()
            
            logger.info(f"IMAP ingestion completed. Processed {processed_count} new properties")
            return processed_count
            
        except Exception as e:
            logger.error(f"IMAP ingestion failed: {str(e)}")
            
            # Update sync history with error
            sync_history.status = 'failed'
            sync_history.error_message = str(e)
            sync_history.completed_at = datetime.utcnow()
            sync_history.sync_duration = int((datetime.utcnow() - start_time).total_seconds())
            db.session.commit()
            
            return 0
    
    def run_full_sync(self) -> int:
        """Run a full synchronization - reset last seen UID and process all emails"""
        logger.info("Starting full email synchronization")
        
        # Temporarily reset last seen UID for full sync
        original_uid = self.last_seen_uid
        self.last_seen_uid = 0
        
        try:
            # Run ingestion with full sync type
            result = self.run_ingestion(sync_type="full")
            return result
            
        finally:
            # Restore original UID only if full sync failed
            # If successful, the new UID will be saved automatically
            pass