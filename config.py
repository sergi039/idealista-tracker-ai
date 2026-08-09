import os


def _first_env(*names, default=None):
    """Return the first non-empty environment variable among names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _compose_database_url():
    """Compose DATABASE_URL from DB_* parts when provided."""
    user = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASSWORD")
    name = os.environ.get("DB_NAME")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    if user and password and name:
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return None


class Config:
    DEV_MODE = os.environ.get("DEV_MODE", "").lower() == "true"

    # Browser/session isolation (important when running legacy + universal on localhost).
    # Cookies do not isolate by port, so use a different cookie name than the legacy app.
    SESSION_COOKIE_NAME = (
        os.environ.get("SESSION_COOKIE_NAME") or "idealista_universal_session"
    )

    # Email backend selection
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "imap").lower()  # 'imap' or 'gmail'

    # AI transport: the owner's Claude and ChatGPT *subscriptions*, reached
    # through the host-side bridge (tools/ai_bridge.py) which shells out to the
    # authenticated Claude Code and Codex CLIs. No ANTHROPIC_API_KEY /
    # OPENAI_API_KEY anywhere: a key would bill per token instead of using the
    # subscription, so this path is deliberately the only one.
    AI_BRIDGE_URL = (
        os.environ.get("AI_BRIDGE_URL") or "http://host.docker.internal:5061"
    )
    AI_BRIDGE_TOKEN = os.environ.get("AI_BRIDGE_TOKEN")

    # Claude (via the claude CLI)
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6"

    # OpenAI (via the codex CLI)
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-5.6-sol"

    # IMAP settings (for Gmail with App Password)
    IMAP_HOST = os.environ.get("IMAP_HOST") or "imap.gmail.com"
    IMAP_PORT = int(os.environ.get("IMAP_PORT") or "993")
    IMAP_SSL = (os.environ.get("IMAP_SSL") or "true").lower() == "true"
    IMAP_USER = os.environ.get("IMAP_USER")  # Required when using IMAP
    IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")  # Required when using IMAP
    # Default to a dedicated Gmail label to avoid clobbering the legacy build.
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER") or "IdealistaProperties"
    # Socket timeout for every IMAP connection (issue #15): a hung socket must
    # fail that run loudly instead of stalling all future ingestions.
    IMAP_TIMEOUT_SECONDS = float(os.environ.get("IMAP_TIMEOUT_SECONDS") or "30")
    IMAP_SEARCH_QUERY = os.environ.get("IMAP_SEARCH_QUERY") or "ALL"
    MAX_EMAILS_PER_RUN = int(os.environ.get("MAX_EMAILS_PER_RUN") or "200")

    # Gmail API (legacy, kept for compatibility)
    GMAIL_API_KEY = os.environ.get("GMAIL_API_KEY")
    GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
    GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
    GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
    GMAIL_LABEL = os.environ.get("GMAIL_LABEL") or "IdealistaProperties"

    # Google APIs - Required for production
    GOOGLE_MAPS_API_KEY = _first_env(
        "GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_API", "Google_api"
    )
    GOOGLE_PLACES_API_KEY = _first_env(
        "GOOGLE_PLACES_API_KEY", "GOOGLE_PLACES_API", "Google_api"
    )

    # Database - Required
    DATABASE_URL = os.environ.get("DATABASE_URL") or _compose_database_url()

    # App settings - Required
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SESSION_SECRET = os.environ.get("SESSION_SECRET")

    # Feature flags
    AUTO_START_SCHEDULER = (
        os.environ.get("AUTO_START_SCHEDULER", "true" if DEV_MODE else "false").lower()
        == "true"
    )
    AUTO_TRAVEL_ENRICHMENT = (
        os.environ.get("AUTO_TRAVEL_ENRICHMENT", "true").lower() == "true"
    )
    AUTO_PROPERTY_SCORING = (
        os.environ.get("AUTO_PROPERTY_SCORING", "true").lower() == "true"
    )
    AUTO_PROFILE_ASSIGNMENT = (
        os.environ.get("AUTO_PROFILE_ASSIGNMENT", "true").lower() == "true"
    )

    # Universal build is sale-first; rentals are excluded unless explicitly enabled.
    SALE_ONLY = os.environ.get("SALE_ONLY", "true").lower() == "true"

    # Optional: skip categories entirely during ingestion (comma-separated).
    # Useful to keep legacy land tracker and universal properties tracker isolated without Gmail labels.
    EXCLUDED_PROPERTY_CATEGORIES = {
        part.strip().lower()
        for part in (os.environ.get("EXCLUDED_PROPERTY_CATEGORIES") or "").split(",")
        if part.strip()
    }

    # Scheduler settings
    SCHEDULER_TIMEZONE = "Europe/Madrid"  # CET timezone
    INGESTION_TIMES = ["07:00", "19:00"]  # 7 AM and 7 PM CET
    LISTING_STATUS_CHECK_TIME = os.environ.get("LISTING_STATUS_CHECK_TIME", "10:00")

    # OSM Overpass API
    OSM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

    # Sea-view estimation. Both sources are free and keyless -- Google billing
    # is off (#98) and is not needed here: the coastline comes from
    # OpenStreetMap and the terrain from Copernicus EU-DEM (25 m).
    SEA_VIEW_ELEVATION_URL = os.environ.get(
        "SEA_VIEW_ELEVATION_URL", "https://api.opentopodata.org/v1/eudem25m"
    )
    # OpenTopoData's public instance asks for one call per second and caps a
    # request at 100 locations. Staying under that is why a backfill is slow
    # rather than parallel.
    SEA_VIEW_ELEVATION_MIN_INTERVAL_S = float(
        os.environ.get("SEA_VIEW_ELEVATION_MIN_INTERVAL_S", "1.1")
    )
    SEA_VIEW_ELEVATION_MAX_LOCATIONS = 100

    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
    LAST_SEEN_UID_PATH = os.environ.get(
        "LAST_SEEN_UID_PATH", os.path.join(DATA_DIR, ".last_seen_uid")
    )
    LAST_SEEN_UID_PROPERTIES_PATH = os.environ.get(
        "LAST_SEEN_UID_PROPERTIES_PATH",
        os.path.join(DATA_DIR, ".last_seen_uid_properties"),
    )

    # Ingestion target selector (legacy lands vs new universal properties)
    INGESTION_TARGET = os.environ.get("INGESTION_TARGET", "properties").lower()

    # Professional scoring weights based on Spanish/European standards
    # Total must equal 1.0 (100%) - Updated to include Investment Yield
    DEFAULT_SCORING_WEIGHTS = {
        # Investment & Financial Returns (20%) - NEW CRITERION
        "investment_yield": 0.20,  # Rental yield, cap rate, investment metrics
        # Location & Accessibility (28%)
        "location_quality": 0.16,  # Proximity to urban centers, neighborhood
        "transport": 0.12,  # Public transport, road access
        # Infrastructure & Utilities (24%)
        "infrastructure_basic": 0.16,  # Water, electricity, sewerage, internet
        "infrastructure_extended": 0.08,  # Gas, telecommunications, public services
        # Physical & Environmental (12%)
        "environment": 0.08,  # Environmental quality, natural features
        "physical_characteristics": 0.04,  # Topography, size, shape
        # Services & Amenities (8%)
        "services_quality": 0.08,  # Schools, hospitals, shopping
        # Legal & Development (8%)
        "legal_status": 0.04,  # Zoning status, building permissions
        "development_potential": 0.04,  # Future development possibilities
    }

    # Dual Scoring System - MCDM Profiles for Investment vs Lifestyle analysis
    SCORING_PROFILES = {
        # Investment Profile - Focus on rental yield, location value, and development potential
        "investment": {
            "investment_yield": 0.35,  # Primary factor for investment returns
            "location_quality": 0.20,  # Location drives property values
            "legal_status": 0.10,  # Legal clarity essential for investment
            "transport": 0.10,  # Accessibility affects rental demand
            "infrastructure_basic": 0.10,  # Basic utilities needed for rentals
            "development_potential": 0.08,  # Future value appreciation
            "physical_characteristics": 0.05,  # Size/shape for development
            "infrastructure_extended": 0.02,  # Nice-to-have for investments
            "services_quality": 0.00,  # Minimal impact on investment returns
            "environment": 0.00,  # Minimal impact on investment returns
        },
        # Lifestyle Profile - Focus on quality of life, environment, and daily amenities
        "lifestyle": {
            "environment": 0.22,  # Views, nature, air quality for living
            "services_quality": 0.18,  # Schools, healthcare, shopping for family
            "location_quality": 0.20,  # Neighborhood quality for living
            "transport": 0.12,  # Daily commute and accessibility
            "infrastructure_extended": 0.10,  # Gas, telecommunications for comfort
            "infrastructure_basic": 0.08,  # Essential utilities
            "physical_characteristics": 0.05,  # Land shape/size for personal use
            "legal_status": 0.03,  # Less critical for personal residence
            "development_potential": 0.02,  # Future changes can be disruptive
            "investment_yield": 0.00,  # Not relevant for personal residence
        },
    }

    # Combined Score Mix - Weighted combination of Investment and Lifestyle scores
    # Based on user preferences: Investment (32%) + Lifestyle (68%)
    COMBINED_MIX = {
        "investment": 0.32,  # Weight for investment score in combined calculation
        "lifestyle": 0.68,  # Weight for lifestyle score in combined calculation
    }
