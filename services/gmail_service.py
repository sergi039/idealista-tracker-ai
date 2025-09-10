import os
import logging
import base64
import re
from email.mime.text import MIMEText
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from utils.email_parser import EmailParser

logger = logging.getLogger(__name__)

class GmailService:
    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
    
    def __init__(self):
        self.service = None
        self.email_parser = EmailParser()
        
    def authenticate(self):
        """Authenticate with Gmail API using service account or OAuth"""
        try:
            # For production, use service account credentials
            # For development, this would need OAuth flow
            api_key = os.environ.get("GMAIL_API_KEY")
            if not api_key:
                logger.error("GMAIL_API_KEY not found in environment variables")
                return False
                
            self.service = build('gmail', 'v1', developerKey=api_key)
            logger.info("Gmail service authenticated successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to authenticate Gmail service: {str(e)}")
            return False
    
    def get_idealista_emails(self, max_results=50):
        """Fetch emails with Idealista label"""
        if not self.service:
            if not self.authenticate():
                return []
        
        try:
            # Search for emails with Idealista label
            query = 'label:Idealista'
            results = self.service.users().messages().list(
                userId='me', 
                q=query, 
                maxResults=max_results
            ).execute()
            
            messages = results.get('messages', [])
            logger.info(f"Found {len(messages)} Idealista emails")
            
            email_data = []
            for message in messages:
                email_content = self.get_email_content(message['id'])
                if email_content:
                    # Skip non-property emails (explicit blacklist)
                    subject = email_content.get('subject', '')
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
                    
                    parsed_data = self.email_parser.parse_idealista_email(email_content)
                    if parsed_data:
                        parsed_data['source_email_id'] = message['id']
                        parsed_data['email_subject'] = email_content.get('subject', '')
                        parsed_data['email_sender'] = email_content.get('sender', '')
                        parsed_data['email_date'] = email_content.get('date', '')
                        email_data.append(parsed_data)
            
            logger.info(f"Successfully parsed {len(email_data)} emails")
            return email_data
            
        except Exception as e:
            logger.error(f"Failed to fetch Idealista emails: {str(e)}")
            return []
    
    def get_email_content(self, message_id):
        """Get the content of a specific email"""
        try:
            message = self.service.users().messages().get(
                userId='me', 
                id=message_id, 
                format='full'
            ).execute()
            
            # Extract email content
            payload = message['payload']
            body = ""
            subject = ""
            
            # Get subject, date, and sender
            headers = payload.get('headers', [])
            date_str = ""
            sender = ""
            for header in headers:
                if header['name'] == 'Subject':
                    subject = header['value']
                elif header['name'] == 'Date':
                    date_str = header['value']
                elif header['name'] == 'From':
                    sender = header['value']
            
            # Get body content
            if 'parts' in payload:
                for part in payload['parts']:
                    if part['mimeType'] == 'text/html' or part['mimeType'] == 'text/plain':
                        if 'data' in part['body']:
                            body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                            break
            else:
                if payload['body'].get('data'):
                    body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
            
            return {
                'subject': subject,
                'body': body,
                'message_id': message_id,
                'date': date_str,
                'sender': sender
            }
            
        except Exception as e:
            logger.error(f"Failed to get email content for {message_id}: {str(e)}")
            return None
    
    def run_ingestion(self):
        """Main method to run email ingestion"""
        try:
            logger.info("Starting Gmail ingestion process")
            emails = self.get_idealista_emails()
            
            if not emails:
                logger.warning("No emails found for ingestion")
                return 0
            
            # Import here to avoid circular imports
            from models import Land
            from app import db
            
            processed_count = 0
            for email_data in emails:
                try:
                    # Check if already exists
                    existing = Land.query.filter_by(
                        source_email_id=email_data['source_email_id']
                    ).first()
                    
                    if existing:
                        logger.debug(f"Email {email_data['source_email_id']} already processed")
                        continue
                    
                    # Parse email date to datetime
                    email_date_obj = None
                    if email_data.get('email_date'):
                        try:
                            from email.utils import parsedate_to_datetime
                            email_date_obj = parsedate_to_datetime(email_data['email_date'])
                        except Exception as e:
                            logger.warning(f"Failed to parse email date: {str(e)}")
                    
                    # Create new land record
                    land = Land(
                        source_email_id=email_data['source_email_id'],
                        email_subject=email_data.get('email_subject'),
                        email_sender=email_data.get('email_sender'),
                        email_date=email_date_obj,
                        title=email_data.get('title'),
                        url=email_data.get('url'),
                        price=email_data.get('price'),
                        area=email_data.get('area'),
                        municipality=email_data.get('municipality'),
                        land_type=email_data.get('land_type'),
                        description=email_data.get('description'),
                        legal_status=email_data.get('legal_status')
                    )
                    
                    db.session.add(land)
                    db.session.commit()
                    
                    # Enrich the land data
                    from services.enrichment_service import EnrichmentService
                    enrichment_service = EnrichmentService()
                    enrichment_service.enrich_land(land.id)
                    
                    processed_count += 1
                    logger.info(f"Processed new land: {land.title}")
                    
                except Exception as e:
                    logger.error(f"Failed to process email {email_data.get('source_email_id')}: {str(e)}")
                    db.session.rollback()
                    continue
            
            logger.info(f"Gmail ingestion completed. Processed {processed_count} new properties")
            return processed_count
            
        except Exception as e:
            logger.error(f"Gmail ingestion failed: {str(e)}")
            return 0
