import os

class Config:
    # Email backend selection
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "imap").lower()  # 'imap' or 'gmail'
    
    # AI Integration - Anthropic Claude
    # Using claude_key from secrets for authentication
    ANTHROPIC_API_KEY = os.environ.get('claude_key')
    ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"  # Latest Claude model
    
    # IMAP settings (for Gmail with App Password)
    IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
    IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
    IMAP_SSL = os.environ.get("IMAP_SSL", "true").lower() == "true"
    IMAP_USER = os.environ.get("IMAP_USER", "")
    IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "Idealista")  # Gmail label mapped as folder
    IMAP_SEARCH_QUERY = os.environ.get("IMAP_SEARCH_QUERY", "ALL")  # e.g. 'UNSEEN' or date filters
    MAX_EMAILS_PER_RUN = int(os.environ.get("MAX_EMAILS_PER_RUN", "200"))
    
    # Gmail API (legacy, kept for compatibility)
    GMAIL_API_KEY = os.environ.get("GMAIL_API_KEY", "")
    GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
    GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
    GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    GMAIL_LABEL = "Idealista"
    
    # Google APIs
    GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    
    # Database
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    
    # App settings
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
    
    # Scheduler settings
    SCHEDULER_TIMEZONE = 'Europe/Madrid'  # CET timezone
    INGESTION_TIMES = ['07:00', '19:00']  # 7 AM and 7 PM CET
    
    # OSM Overpass API
    OSM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    
    # Professional scoring weights based on Spanish/European standards
    # Total must equal 1.0 (100%)
    DEFAULT_SCORING_WEIGHTS = {
        # Location & Accessibility (35%)
        'location_quality': 0.20,          # Proximity to urban centers, neighborhood
        'transport': 0.15,                 # Public transport, road access
        
        # Infrastructure & Utilities (30%)
        'infrastructure_basic': 0.20,      # Water, electricity, sewerage, internet
        'infrastructure_extended': 0.10,   # Gas, telecommunications, public services
        
        # Physical & Environmental (15%)
        'environment': 0.10,               # Environmental quality, natural features
        'physical_characteristics': 0.05,  # Topography, size, shape
        
        # Services & Amenities (10%)
        'services_quality': 0.10,          # Schools, hospitals, shopping
        
        # Legal & Development (10%)
        'legal_status': 0.05,              # Zoning status, building permissions
        'development_potential': 0.05      # Future development possibilities
    }
