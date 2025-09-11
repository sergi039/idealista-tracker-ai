import os

class Config:
    # Email backend selection
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "imap").lower()  # 'imap' or 'gmail'
    
    # AI Integration - Anthropic Claude
    # Using claude_key from secrets for authentication
    ANTHROPIC_API_KEY = os.environ.get('claude_key')
    ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"  # Latest Claude model
    
    # IMAP settings (for Gmail with App Password)
    IMAP_HOST = os.environ.get("IMAP_HOST") or "imap.gmail.com"
    IMAP_PORT = int(os.environ.get("IMAP_PORT") or "993")
    IMAP_SSL = (os.environ.get("IMAP_SSL") or "true").lower() == "true"
    IMAP_USER = os.environ.get("IMAP_USER")  # Required when using IMAP
    IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")  # Required when using IMAP
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER") or "Idealista"
    IMAP_SEARCH_QUERY = os.environ.get("IMAP_SEARCH_QUERY") or "ALL"
    MAX_EMAILS_PER_RUN = int(os.environ.get("MAX_EMAILS_PER_RUN") or "200")
    
    # Gmail API (legacy, kept for compatibility)
    GMAIL_API_KEY = os.environ.get("GMAIL_API_KEY")
    GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID") 
    GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
    GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
    GMAIL_LABEL = "Idealista"
    
    # Google APIs - Required for production
    GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
    GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
    
    # Database - Required
    DATABASE_URL = os.environ.get("DATABASE_URL")
    
    # App settings - Required
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SESSION_SECRET = os.environ.get("SESSION_SECRET")
    
    # Scheduler settings
    SCHEDULER_TIMEZONE = 'Europe/Madrid'  # CET timezone
    INGESTION_TIMES = ['07:00', '19:00']  # 7 AM and 7 PM CET
    
    # OSM Overpass API
    OSM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    
    # Professional scoring weights based on Spanish/European standards
    # Total must equal 1.0 (100%) - Updated to include Investment Yield
    DEFAULT_SCORING_WEIGHTS = {
        # Investment & Financial Returns (20%) - NEW CRITERION
        'investment_yield': 0.20,          # Rental yield, cap rate, investment metrics
        
        # Location & Accessibility (28%)
        'location_quality': 0.16,          # Proximity to urban centers, neighborhood
        'transport': 0.12,                 # Public transport, road access
        
        # Infrastructure & Utilities (24%)
        'infrastructure_basic': 0.16,      # Water, electricity, sewerage, internet
        'infrastructure_extended': 0.08,   # Gas, telecommunications, public services
        
        # Physical & Environmental (12%)
        'environment': 0.08,               # Environmental quality, natural features
        'physical_characteristics': 0.04,  # Topography, size, shape
        
        # Services & Amenities (8%)
        'services_quality': 0.08,          # Schools, hospitals, shopping
        
        # Legal & Development (8%)
        'legal_status': 0.04,              # Zoning status, building permissions
        'development_potential': 0.04      # Future development possibilities
    }
    
    # Dual Scoring System - MCDM Profiles for Investment vs Lifestyle analysis
    SCORING_PROFILES = {
        # Investment Profile - Focus on rental yield, location value, and development potential
        'investment': {
            'investment_yield': 0.35,          # Primary factor for investment returns
            'location_quality': 0.20,          # Location drives property values
            'legal_status': 0.10,              # Legal clarity essential for investment
            'transport': 0.10,                 # Accessibility affects rental demand
            'infrastructure_basic': 0.10,      # Basic utilities needed for rentals
            'development_potential': 0.08,     # Future value appreciation
            'physical_characteristics': 0.05,  # Size/shape for development
            'infrastructure_extended': 0.02,   # Nice-to-have for investments
            'services_quality': 0.00,          # Minimal impact on investment returns
            'environment': 0.00                # Minimal impact on investment returns
        },
        
        # Lifestyle Profile - Focus on quality of life, environment, and daily amenities
        'lifestyle': {
            'environment': 0.22,               # Views, nature, air quality for living
            'services_quality': 0.18,          # Schools, healthcare, shopping for family
            'location_quality': 0.20,          # Neighborhood quality for living
            'transport': 0.12,                 # Daily commute and accessibility
            'infrastructure_extended': 0.10,   # Gas, telecommunications for comfort
            'infrastructure_basic': 0.08,      # Essential utilities
            'physical_characteristics': 0.05,  # Land shape/size for personal use
            'legal_status': 0.03,              # Less critical for personal residence
            'development_potential': 0.02,     # Future changes can be disruptive
            'investment_yield': 0.00           # Not relevant for personal residence
        }
    }
    
    # Combined Score Mix - Weighted combination of Investment and Lifestyle scores
    # Based on user preferences: Investment (32%) + Lifestyle (68%)
    COMBINED_MIX = {
        'investment': 0.32,  # Weight for investment score in combined calculation
        'lifestyle': 0.68    # Weight for lifestyle score in combined calculation
    }
