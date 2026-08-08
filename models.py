from datetime import datetime, timezone
import json

from app import db
from sqlalchemy import CheckConstraint
from sqlalchemy.types import JSON


def utcnow():
    return datetime.now(timezone.utc)


class SearchProfile(db.Model):
    """Represents a saved search / client profile.

    Each profile can have its own classification rules, travel targets, and UI/scoring config.
    """

    __tablename__ = "search_profiles"
    __table_args__ = (
        db.Index("ix_search_profiles_name", "name"),
        db.Index("ix_search_profiles_is_active", "is_active"),
        db.Index("ix_search_profiles_is_default", "is_default"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    is_default = db.Column(db.Boolean, default=False)

    # Email routing for ingestion: list of regex patterns (configurable).
    email_matchers = db.Column(JSON)  # [{"pattern": "...", "priority": 10}, ...]

    # Per-profile configuration (all optional; fall back to global defaults).
    classification_rules = db.Column(
        JSON
    )  # [{"category": "...", "subtype": "...", "pattern": "...", "priority": 50}, ...]
    travel_targets = db.Column(
        JSON
    )  # list of targets (schema in SettingsService/SearchProfileService)
    ui_config = db.Column(JSON)  # which columns/sections to show
    scoring_config = db.Column(JSON)  # future: per-profile scoring weights, etc.
    ai_config = db.Column(
        JSON
    )  # optional AI prompt/context overrides (e.g., market_context)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self):
        return f"<SearchProfile {self.id} {self.name}>"


class Property(db.Model):
    """Universal property model (venta-first, Spain-wide).

    Introduced alongside the legacy Land model to enable an incremental migration.
    """

    __tablename__ = "properties"
    __table_args__ = (
        db.Index("ix_properties_idealista_property_id", "idealista_property_id"),
        db.Index("ix_properties_search_profile_id", "search_profile_id"),
        db.Index("ix_properties_property_category", "property_category"),
        db.Index("ix_properties_property_subtype", "property_subtype"),
        db.Index("ix_properties_municipality", "municipality"),
        db.Index("ix_properties_listing_status", "listing_status"),
        db.Index("ix_properties_is_favorite", "is_favorite"),
        db.Index("ix_properties_created_at", "created_at"),
        db.Index("ix_properties_score_total", "score_total"),
        db.Index("ix_properties_score_investment", "score_investment"),
        db.Index("ix_properties_score_lifestyle", "score_lifestyle"),
        # Data integrity constraints
        CheckConstraint(
            "price IS NULL OR price >= 0", name="ck_properties_price_non_negative"
        ),
        CheckConstraint(
            "area IS NULL OR area >= 0", name="ck_properties_area_non_negative"
        ),
        CheckConstraint(
            "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
            name="ck_properties_lat_range",
        ),
        CheckConstraint(
            "location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180)",
            name="ck_properties_lon_range",
        ),
        CheckConstraint(
            "score_total IS NULL OR (score_total >= 0 AND score_total <= 100)",
            name="ck_properties_score_total_range",
        ),
        CheckConstraint(
            "score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100)",
            name="ck_properties_score_investment_range",
        ),
        CheckConstraint(
            "score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100)",
            name="ck_properties_score_lifestyle_range",
        ),
        CheckConstraint(
            "listing_status IN ('active', 'removed', 'sold', 'unknown')",
            name="ck_properties_listing_status_enum",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    # Stable Idealista listing id extracted from URL (/inmueble/<id>/). Used for dedup/updates.
    idealista_property_id = db.Column(db.BigInteger, nullable=True)
    email_subject = db.Column(db.Text)
    email_sender = db.Column(db.String(255))

    search_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("search_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    search_profile = db.relationship(
        "SearchProfile", backref=db.backref("properties", lazy="dynamic")
    )

    title = db.Column(db.Text)
    url = db.Column(db.Text)

    # Deal & type classification (config-driven; no hard constraints here by design).
    deal_type = db.Column(
        db.String(16), default="sale"
    )  # 'sale' | 'rent' (rent not used initially)
    property_category = db.Column(
        db.String(32)
    )  # e.g. 'housing', 'land', 'garage', 'commercial', 'building'
    property_subtype = db.Column(
        db.String(64)
    )  # e.g. 'apartment', 'house', 'penthouse', 'plot', ...

    price = db.Column(db.Numeric(10, 2))
    currency = db.Column(db.String(3), default="EUR")
    area = db.Column(db.Numeric(10, 2))
    area_type = db.Column(
        db.String(16), default="unknown"
    )  # 'built' | 'plot' | 'unknown'

    municipality = db.Column(db.String(255))
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    location_accuracy = db.Column(
        db.String(20), default="unknown"
    )  # 'precise', 'approximate', 'unknown'

    description = db.Column(db.Text)

    # Flexible JSON fields for extracted + enriched data.
    attributes = db.Column(JSON)  # rooms, bathrooms, floor, etc.
    property_details = db.Column(
        JSON
    )  # Raw/detail blocks (kept for compatibility patterns)
    enrichment = db.Column(JSON)  # Google/OSM derived info
    travel = db.Column(JSON)  # Dynamic travel times/distances per configured targets
    scoring = db.Column(JSON)  # Full scoring breakdown (per profile)
    ai_analysis = db.Column(JSON)
    enhanced_description = db.Column(JSON)

    score_total = db.Column(db.Numeric(5, 2))
    score_investment = db.Column(db.Numeric(5, 2))
    score_lifestyle = db.Column(db.Numeric(5, 2))

    # Price history (generic)
    previous_price = db.Column(db.Numeric(10, 2))
    price_change_amount = db.Column(db.Numeric(10, 2))
    price_change_percentage = db.Column(db.Numeric(5, 2))
    price_changed_date = db.Column(db.DateTime)

    is_favorite = db.Column(db.Boolean, default=False)

    listing_status = db.Column(
        db.String(20), default="active"
    )  # 'active', 'removed', 'sold', 'unknown'
    listing_removed_date = db.Column(db.DateTime)
    listing_last_checked = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=utcnow)
    email_date = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        title = (self.title or "").strip()
        snippet = (title[:50] + "...") if len(title) > 50 else title
        return f"<Property {self.id}: {snippet}>"

    def to_dict(self):
        return {
            "id": self.id,
            "source_email_id": self.source_email_id,
            "idealista_property_id": self.idealista_property_id,
            "email_subject": self.email_subject,
            "email_sender": self.email_sender,
            "search_profile_id": self.search_profile_id,
            "title": self.title,
            "url": self.url,
            "deal_type": self.deal_type,
            "property_category": self.property_category,
            "property_subtype": self.property_subtype,
            "price": float(self.price) if self.price else None,
            "currency": self.currency,
            "area": float(self.area) if self.area else None,
            "area_type": self.area_type,
            "municipality": self.municipality,
            "location_lat": float(self.location_lat) if self.location_lat else None,
            "location_lon": float(self.location_lon) if self.location_lon else None,
            "location_accuracy": self.location_accuracy,
            "description": self.description,
            "attributes": self.attributes or {},
            "property_details": self.property_details or {},
            "enrichment": self.enrichment or {},
            "travel": self.travel or {},
            "scoring": self.scoring or {},
            "ai_analysis": self.ai_analysis or {},
            "enhanced_description": self.enhanced_description or {},
            "score_total": float(self.score_total) if self.score_total else None,
            "score_investment": float(self.score_investment)
            if self.score_investment
            else None,
            "score_lifestyle": float(self.score_lifestyle)
            if self.score_lifestyle
            else None,
            "previous_price": float(self.previous_price)
            if self.previous_price
            else None,
            "price_change_amount": float(self.price_change_amount)
            if self.price_change_amount
            else None,
            "price_change_percentage": float(self.price_change_percentage)
            if self.price_change_percentage
            else None,
            "price_changed_date": self.price_changed_date.isoformat()
            if self.price_changed_date
            else None,
            "is_favorite": bool(self.is_favorite),
            "listing_status": self.listing_status or "active",
            "listing_removed_date": self.listing_removed_date.isoformat()
            if self.listing_removed_date
            else None,
            "listing_last_checked": self.listing_last_checked.isoformat()
            if self.listing_last_checked
            else None,
            "email_date": self.email_date.isoformat() if self.email_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def _get_enrichment_dict(self):
        return self.enrichment if isinstance(self.enrichment, dict) else {}

    def _get_legacy_land(self):
        enrichment = self._get_enrichment_dict()
        legacy = enrichment.get("legacy_land")
        return legacy if isinstance(legacy, dict) else {}

    def _get_enrichment_section(self, key):
        enrichment = self._get_enrichment_dict()
        section = enrichment.get(key)
        if isinstance(section, dict):
            return section
        legacy = self._get_legacy_land()
        section = legacy.get(key)
        return section if isinstance(section, dict) else {}

    def _get_enrichment_value(self, key):
        enrichment = self._get_enrichment_dict()
        if key in enrichment and enrichment.get(key) is not None:
            return enrichment.get(key)
        legacy = self._get_legacy_land()
        if key in legacy and legacy.get(key) is not None:
            return legacy.get(key)
        return None

    def _get_travel_targets(self):
        travel = self.travel if isinstance(self.travel, dict) else {}
        targets = travel.get("targets")
        return targets if isinstance(targets, dict) else {}

    def _travel_target_duration(self, key):
        target = self._get_travel_targets().get(key)
        if not isinstance(target, dict):
            return None
        duration_min = target.get("duration_min")
        if duration_min is not None:
            return duration_min
        duration_s = target.get("duration_s")
        if duration_s is None:
            return None
        return int(round(duration_s / 60.0))

    def _travel_target_distance_km(self, key):
        target = self._get_travel_targets().get(key)
        if not isinstance(target, dict):
            return None
        distance_km = target.get("distance_km")
        if distance_km is None:
            distance_m = target.get("distance_m")
            if distance_m is None:
                return None
            distance_km = float(distance_m) / 1000.0
        return round(float(distance_km), 1)

    @property
    def infrastructure_basic(self):
        return self._get_enrichment_section("infrastructure_basic")

    @property
    def infrastructure_extended(self):
        return self._get_enrichment_section("infrastructure_extended")

    @property
    def transport(self):
        return self._get_enrichment_section("transport")

    @property
    def environment(self):
        return self._get_enrichment_section("environment")

    @property
    def services_quality(self):
        return self._get_enrichment_section("services_quality")

    @property
    def travel_time_airport(self):
        value = self._get_enrichment_value("travel_time_airport")
        if value is not None:
            return value
        return self._travel_target_duration("airport")

    @property
    def travel_time_train_station(self):
        value = self._get_enrichment_value("travel_time_train_station")
        if value is not None:
            return value
        return self._travel_target_duration("train_station")

    @property
    def travel_time_hospital(self):
        value = self._get_enrichment_value("travel_time_hospital")
        if value is not None:
            return value
        return self._travel_target_duration("hospital")

    @property
    def travel_time_police(self):
        value = self._get_enrichment_value("travel_time_police")
        if value is not None:
            return value
        return self._travel_target_duration("police")

    @property
    def distance_airport(self):
        value = self._get_enrichment_value("distance_airport")
        if value is not None:
            return value
        return self._travel_target_distance_km("airport")

    @property
    def distance_train_station(self):
        value = self._get_enrichment_value("distance_train_station")
        if value is not None:
            return value
        return self._travel_target_distance_km("train_station")

    @property
    def distance_hospital(self):
        value = self._get_enrichment_value("distance_hospital")
        if value is not None:
            return value
        return self._travel_target_distance_km("hospital")

    @property
    def distance_police(self):
        value = self._get_enrichment_value("distance_police")
        if value is not None:
            return value
        return self._travel_target_distance_km("police")

    def _ai_analysis_dict(self):
        """Return ai_analysis as a dict regardless of storage type."""
        data = self.ai_analysis
        if not data:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _humanize_rating_text(value):
        text = str(value or "").strip()
        if not text:
            return None

        text = text.replace("_", " ")
        text = " ".join(text.split())

        head, sep, tail = text.partition("-")
        head = head.strip()
        tail = tail.strip() if sep else ""

        if head and head.upper() == head:
            head = head.title()

        if not sep:
            return head

        if not tail:
            return head

        return f"{head} - {tail}"

    @property
    def investment_metrics_rating_full(self):
        """Full investment rating text from structured rental market analysis (if present)."""
        analysis = self._ai_analysis_dict()
        rental = (
            analysis.get("rental_market_analysis")
            if isinstance(analysis, dict)
            else None
        )
        if not isinstance(rental, dict):
            return None
        rating = rental.get("investment_rating")
        if not rating:
            return None
        return self._humanize_rating_text(rating)

    @property
    def investment_metrics_rating(self):
        """Short investment rating label (e.g., GOOD/MODERATE/EXCELLENT)."""
        full = self.investment_metrics_rating_full
        if not full:
            return None
        short = full.split("-", 1)[0].strip()
        return short if short else None


class Land(db.Model):
    __tablename__ = "lands"
    __table_args__ = (
        db.Index("ix_lands_idealista_property_id", "idealista_property_id"),
        db.Index("ix_lands_land_type", "land_type"),
        db.Index("ix_lands_municipality", "municipality"),
        db.Index("ix_lands_listing_status", "listing_status"),
        db.Index("ix_lands_is_favorite", "is_favorite"),
        db.Index("ix_lands_created_at", "created_at"),
        db.Index("ix_lands_score_total", "score_total"),
        db.Index("ix_lands_score_investment", "score_investment"),
        db.Index("ix_lands_score_lifestyle", "score_lifestyle"),
        # Data integrity constraints (matching Legacy)
        CheckConstraint(
            "price IS NULL OR price >= 0", name="ck_lands_price_non_negative"
        ),
        CheckConstraint("area IS NULL OR area >= 0", name="ck_lands_area_non_negative"),
        CheckConstraint(
            "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
            name="ck_lands_lat_range",
        ),
        CheckConstraint(
            "location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180)",
            name="ck_lands_lon_range",
        ),
        CheckConstraint(
            "score_total IS NULL OR (score_total >= 0 AND score_total <= 100)",
            name="ck_lands_score_total_range",
        ),
        CheckConstraint(
            "score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100)",
            name="ck_lands_score_investment_range",
        ),
        CheckConstraint(
            "score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100)",
            name="ck_lands_score_lifestyle_range",
        ),
        CheckConstraint(
            "travel_time_oviedo IS NULL OR travel_time_oviedo >= 0",
            name="ck_lands_tt_oviedo",
        ),
        CheckConstraint(
            "travel_time_gijon IS NULL OR travel_time_gijon >= 0",
            name="ck_lands_tt_gijon",
        ),
        CheckConstraint(
            "travel_time_nearest_beach IS NULL OR travel_time_nearest_beach >= 0",
            name="ck_lands_tt_beach",
        ),
        CheckConstraint(
            "travel_time_airport IS NULL OR travel_time_airport >= 0",
            name="ck_lands_tt_airport",
        ),
        CheckConstraint(
            "travel_time_train_station IS NULL OR travel_time_train_station >= 0",
            name="ck_lands_tt_train",
        ),
        CheckConstraint(
            "travel_time_hospital IS NULL OR travel_time_hospital >= 0",
            name="ck_lands_tt_hospital",
        ),
        CheckConstraint(
            "travel_time_police IS NULL OR travel_time_police >= 0",
            name="ck_lands_tt_police",
        ),
        CheckConstraint(
            "listing_status IN ('active', 'removed', 'sold', 'unknown')",
            name="ck_lands_listing_status_enum",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    # Stable Idealista listing id extracted from URL (/inmueble/<id>/). Used for dedup/updates.
    idealista_property_id = db.Column(db.BigInteger, nullable=True)
    email_subject = db.Column(db.Text)  # Original email subject line
    email_sender = db.Column(db.String(255))  # Email sender
    title = db.Column(db.Text)
    url = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2))
    area = db.Column(db.Numeric(10, 2))
    municipality = db.Column(db.String(255))
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    location_accuracy = db.Column(
        db.String(20), default="unknown"
    )  # 'precise', 'approximate', 'unknown'
    land_type = db.Column(
        db.String(20), CheckConstraint("land_type IN ('developed', 'buildable')")
    )
    description = db.Column(db.Text)

    # JSON fields for complex data (works with both PostgreSQL and SQLite)
    infrastructure_basic = db.Column(JSON)  # electricity, water, internet, gas
    infrastructure_extended = db.Column(
        JSON
    )  # supermarket, school, restaurants, hospital
    transport = db.Column(JSON)  # train, airport, highway, bus
    environment = db.Column(JSON)  # sea_view, mountain_view, forest, orientation
    neighborhood = db.Column(JSON)  # new_houses, area_price_level, noise
    services_quality = db.Column(
        JSON
    )  # schools rating, restaurants rating, cafes rating

    legal_status = db.Column(db.String(50))
    property_details = db.Column(
        JSON
    )  # AI analysis and property details in JSON format
    ai_analysis = db.Column(JSON)  # Structured AI analysis with 5 blocks
    enhanced_description = db.Column(JSON)  # AI-enhanced professional description data
    score_total = db.Column(db.Numeric(5, 2))
    score_investment = db.Column(db.Numeric(5, 2))  # Investment-focused score (0-100)
    score_lifestyle = db.Column(db.Numeric(5, 2))  # Lifestyle-focused score (0-100)

    # Travel times by car (in minutes)
    # Reference city A/B are configured via SettingsService.get_reference_cities().
    travel_time_oviedo = db.Column(db.Integer)  # Travel time to reference city A
    travel_time_gijon = db.Column(db.Integer)  # Travel time to reference city B
    travel_time_nearest_beach = db.Column(
        db.Integer
    )  # Time to nearest beach in minutes
    nearest_beach_name = db.Column(db.String(255))  # Name of nearest beach

    # Priority infrastructure travel times (in minutes)
    travel_time_airport = db.Column(db.Integer)  # Time to nearest airport
    travel_time_train_station = db.Column(db.Integer)  # Time to nearest train station
    travel_time_hospital = db.Column(db.Integer)  # Time to nearest hospital
    travel_time_police = db.Column(db.Integer)  # Time to nearest police station

    # Priority infrastructure distances (in kilometers)
    distance_airport = db.Column(db.Integer)  # Distance to nearest airport in km
    distance_train_station = db.Column(
        db.Integer
    )  # Distance to nearest train station in km
    distance_hospital = db.Column(db.Integer)  # Distance to nearest hospital in km
    distance_police = db.Column(db.Integer)  # Distance to nearest police station in km

    # Price history tracking
    previous_price = db.Column(db.Numeric(10, 2))  # Previous price before update
    price_change_amount = db.Column(
        db.Numeric(10, 2)
    )  # Amount of price change (negative for decrease)
    price_change_percentage = db.Column(db.Numeric(5, 2))  # Percentage change
    price_changed_date = db.Column(db.DateTime)  # When price was last changed

    # Favorites
    is_favorite = db.Column(db.Boolean, default=False)  # Mark property as favorite

    # Listing status tracking
    listing_status = db.Column(
        db.String(20), default="active"
    )  # 'active', 'removed', 'sold', 'unknown'
    listing_removed_date = db.Column(
        db.DateTime
    )  # When listing was removed from Idealista
    listing_last_checked = db.Column(
        db.DateTime
    )  # Last time we checked the listing status

    created_at = db.Column(db.DateTime, default=utcnow)
    email_date = db.Column(db.DateTime)  # Date when the email was received
    updated_at = db.Column(
        db.DateTime, default=utcnow, onupdate=utcnow
    )  # Last update time

    def __repr__(self):
        return f"<Land {self.id}: {self.title[:50]}...>"

    def to_dict(self):
        """Convert land to dictionary for API responses"""
        return {
            "id": self.id,
            "source_email_id": self.source_email_id,
            "idealista_property_id": self.idealista_property_id,
            "title": self.title,
            "url": self.url,
            "price": float(self.price) if self.price else None,
            "area": float(self.area) if self.area else None,
            "municipality": self.municipality,
            "location_lat": float(self.location_lat) if self.location_lat else None,
            "location_lon": float(self.location_lon) if self.location_lon else None,
            "location_accuracy": self.location_accuracy,
            "land_type": self.land_type,
            "description": self.description,
            "infrastructure_basic": self.infrastructure_basic or {},
            "infrastructure_extended": self.infrastructure_extended or {},
            "transport": self.transport or {},
            "environment": self.environment or {},
            "neighborhood": self.neighborhood or {},
            "services_quality": self.services_quality or {},
            "legal_status": self.legal_status,
            "score_total": float(self.score_total) if self.score_total else None,
            "score_investment": float(self.score_investment)
            if self.score_investment
            else None,
            "score_lifestyle": float(self.score_lifestyle)
            if self.score_lifestyle
            else None,
            "travel_time_oviedo": self.travel_time_oviedo,
            "travel_time_gijon": self.travel_time_gijon,
            "travel_time_nearest_beach": self.travel_time_nearest_beach,
            "nearest_beach_name": self.nearest_beach_name,
            "is_favorite": self.is_favorite or False,
            "listing_status": self.listing_status or "active",
            "listing_removed_date": self.listing_removed_date.isoformat()
            if self.listing_removed_date
            else None,
            "listing_last_checked": self.listing_last_checked.isoformat()
            if self.listing_last_checked
            else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def _ai_analysis_dict(self):
        """Return ai_analysis as a dict regardless of storage type."""
        data = self.ai_analysis
        if not data:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @property
    def investment_metrics_rating_full(self):
        """Full investment rating text from structured rental market analysis (if present)."""
        analysis = self._ai_analysis_dict()
        rental = (
            analysis.get("rental_market_analysis")
            if isinstance(analysis, dict)
            else None
        )
        if not isinstance(rental, dict):
            return None
        rating = rental.get("investment_rating")
        if not rating:
            return None
        return self._humanize_rating_text(rating)

    @property
    def investment_metrics_rating(self):
        """Short investment rating label (e.g., GOOD/MODERATE/EXCELLENT)."""
        full = self.investment_metrics_rating_full
        if not full:
            return None
        short = full.split("-", 1)[0].strip()
        return short if short else None

    @staticmethod
    def _humanize_rating_text(value):
        text = str(value or "").strip()
        if not text:
            return None

        text = text.replace("_", " ")
        text = " ".join(text.split())

        head, sep, tail = text.partition("-")
        head = head.strip()
        tail = tail.strip() if sep else ""

        if head and head.upper() == head:
            head = head.title()

        if not sep:
            return head

        if not tail:
            return head

        return f"{head} - {tail}"

    @property
    def investment_metrics_badge_class(self):
        """Bootstrap badge class for investment rating."""
        full = (
            self.investment_metrics_rating_full or self.investment_metrics_rating or ""
        ).upper()
        if not full:
            return None
        if "EXCELLENT" in full or full == "HIGH":
            return "bg-success"
        if "GOOD" in full:
            return "bg-primary"
        if "MODERATE" in full or "MEDIUM" in full:
            return "bg-warning text-dark"
        if "BELOW" in full or "POOR" in full or "LOW" in full:
            return "bg-danger"
        return "bg-secondary"


class ScoringCriteria(db.Model):
    __tablename__ = "scoring_criteria"

    id = db.Column(db.Integer, primary_key=True)
    criteria_name = db.Column(db.String(100), nullable=False)
    profile = db.Column(
        db.String(20), nullable=True, default="combined"
    )  # 'investment', 'lifestyle', 'combined'
    weight = db.Column(db.Numeric(3, 2), default=1.0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    # Unique constraint for criteria_name + profile combination
    __table_args__ = (
        db.UniqueConstraint("criteria_name", "profile", name="uq_criteria_profile"),
    )

    def __repr__(self):
        return f"<ScoringCriteria {self.criteria_name}[{self.profile}]: {self.weight}>"


class LandHistory(db.Model):
    """Tracks changes to favorite properties over time"""

    __tablename__ = "land_history"

    id = db.Column(db.Integer, primary_key=True)
    land_id = db.Column(
        db.Integer, db.ForeignKey("lands.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date = db.Column(db.DateTime, default=utcnow, nullable=False)

    # Tracked fields
    price = db.Column(db.Numeric(10, 2))
    title = db.Column(db.Text)
    description = db.Column(db.Text)
    area = db.Column(db.Numeric(10, 2))
    land_type = db.Column(db.String(20))
    url = db.Column(db.Text)

    # Change metadata
    change_type = db.Column(db.String(50), nullable=False)
    # Types: 'added_to_favorites', 'price_change', 'description_change', 'title_change', 'removed_from_listing', 'periodic_snapshot'

    # Price change details (for price_change type)
    price_previous = db.Column(db.Numeric(10, 2))
    price_change_amount = db.Column(db.Numeric(10, 2))
    price_change_percentage = db.Column(db.Numeric(5, 2))

    # Relationship
    land = db.relationship(
        "Land",
        backref=db.backref(
            "history", lazy="dynamic", order_by="LandHistory.snapshot_date.desc()"
        ),
    )

    def __repr__(self):
        return (
            f"<LandHistory {self.land_id} - {self.change_type} @ {self.snapshot_date}>"
        )

    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "land_id": self.land_id,
            "snapshot_date": self.snapshot_date.isoformat()
            if self.snapshot_date
            else None,
            "price": float(self.price) if self.price else None,
            "title": self.title,
            "description": self.description,
            "area": float(self.area) if self.area else None,
            "land_type": self.land_type,
            "url": self.url,
            "change_type": self.change_type,
            "price_previous": float(self.price_previous)
            if self.price_previous
            else None,
            "price_change_amount": float(self.price_change_amount)
            if self.price_change_amount
            else None,
            "price_change_percentage": float(self.price_change_percentage)
            if self.price_change_percentage
            else None,
        }

    @classmethod
    def create_snapshot(cls, land, change_type, price_previous=None):
        """Create a new history snapshot for a land"""
        snapshot = cls(
            land_id=land.id,
            price=land.price,
            title=land.title,
            description=land.description,
            area=land.area,
            land_type=land.land_type,
            url=land.url,
            change_type=change_type,
            price_previous=price_previous,
        )

        # Calculate price change if previous price provided
        if price_previous and land.price:
            snapshot.price_change_amount = float(land.price) - float(price_previous)
            if float(price_previous) > 0:
                snapshot.price_change_percentage = (
                    snapshot.price_change_amount / float(price_previous)
                ) * 100

        return snapshot


class SyncHistory(db.Model):
    __tablename__ = "sync_history"

    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(20), nullable=False)  # 'full', 'incremental'
    backend = db.Column(db.String(20), nullable=False)  # 'imap', 'gmail'
    total_emails_found = db.Column(db.Integer, default=0)
    new_properties_added = db.Column(db.Integer, default=0)
    price_updated_count = db.Column(
        db.Integer, default=0
    )  # Properties with price changes
    expired_count = db.Column(
        db.Integer, default=0
    )  # Properties marked as expired/removed
    sync_duration = db.Column(db.Integer)  # Duration in seconds
    status = db.Column(
        db.String(20), default="completed"
    )  # 'completed', 'failed', 'partial'
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=utcnow)
    completed_at = db.Column(db.DateTime)

    def __repr__(self):
        return (
            f"<SyncHistory {self.sync_type} - {self.new_properties_added} properties>"
        )


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    value = db.Column(JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self):
        return f"<AppSetting {self.key}>"


class MarketSettings(db.Model):
    """Configurable market analysis parameters for AI enrichment"""

    __tablename__ = "market_settings"

    id = db.Column(db.Integer, primary_key=True)

    # Construction costs (€/m²)
    construction_basic_min = db.Column(db.Integer, default=1100)
    construction_basic_avg = db.Column(db.Integer, default=1300)
    construction_basic_max = db.Column(db.Integer, default=1500)
    construction_premium_min = db.Column(db.Integer, default=1500)
    construction_premium_avg = db.Column(db.Integer, default=1800)
    construction_premium_max = db.Column(db.Integer, default=2200)

    # Purchase costs ratio (transfer tax/VAT + notary/registry/legal); varies by region and deal.
    purchase_costs_ratio = db.Column(db.Numeric(4, 3), default=0.10)

    # Rental adjustments - Urban
    urban_vacancy_rate = db.Column(db.Numeric(4, 3), default=0.05)
    urban_operating_expenses = db.Column(db.Numeric(4, 3), default=0.15)
    urban_management_fee = db.Column(db.Numeric(4, 3), default=0.00)

    # Rental adjustments - Suburban
    suburban_vacancy_rate = db.Column(db.Numeric(4, 3), default=0.08)
    suburban_operating_expenses = db.Column(db.Numeric(4, 3), default=0.15)
    suburban_management_fee = db.Column(db.Numeric(4, 3), default=0.00)

    # Rental adjustments - Rural
    rural_vacancy_rate = db.Column(db.Numeric(4, 3), default=0.20)
    rural_operating_expenses = db.Column(db.Numeric(4, 3), default=0.18)
    rural_management_fee = db.Column(db.Numeric(4, 3), default=0.10)

    # Rental prices per m²/month (configurable)
    urban_rental_min = db.Column(db.Integer, default=9)
    urban_rental_avg = db.Column(db.Integer, default=11)
    urban_rental_max = db.Column(db.Integer, default=13)
    suburban_rental_min = db.Column(db.Integer, default=7)
    suburban_rental_avg = db.Column(db.Integer, default=9)
    suburban_rental_max = db.Column(db.Integer, default=11)
    rural_rental_min = db.Column(db.Integer, default=5)
    rural_rental_avg = db.Column(db.Integer, default=7)
    rural_rental_max = db.Column(db.Integer, default=9)

    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<MarketSettings id={self.id}>"

    def to_dict(self):
        """Convert to dictionary for API/template use"""
        return {
            "construction_costs": {
                "basic": {
                    "min": self.construction_basic_min,
                    "avg": self.construction_basic_avg,
                    "max": self.construction_basic_max,
                },
                "premium": {
                    "min": self.construction_premium_min,
                    "avg": self.construction_premium_avg,
                    "max": self.construction_premium_max,
                },
            },
            "purchase_costs_ratio": float(self.purchase_costs_ratio)
            if self.purchase_costs_ratio
            else 0.11,
            "rental_adjustments": {
                "urban": {
                    "vacancy_rate": float(self.urban_vacancy_rate)
                    if self.urban_vacancy_rate
                    else 0.05,
                    "operating_expenses": float(self.urban_operating_expenses)
                    if self.urban_operating_expenses
                    else 0.15,
                    "management_fee": float(self.urban_management_fee)
                    if self.urban_management_fee
                    else 0.00,
                },
                "suburban": {
                    "vacancy_rate": float(self.suburban_vacancy_rate)
                    if self.suburban_vacancy_rate
                    else 0.08,
                    "operating_expenses": float(self.suburban_operating_expenses)
                    if self.suburban_operating_expenses
                    else 0.15,
                    "management_fee": float(self.suburban_management_fee)
                    if self.suburban_management_fee
                    else 0.00,
                },
                "rural": {
                    "vacancy_rate": float(self.rural_vacancy_rate)
                    if self.rural_vacancy_rate
                    else 0.20,
                    "operating_expenses": float(self.rural_operating_expenses)
                    if self.rural_operating_expenses
                    else 0.18,
                    "management_fee": float(self.rural_management_fee)
                    if self.rural_management_fee
                    else 0.10,
                },
            },
            "rental_prices": {
                "urban": {
                    "min": self.urban_rental_min,
                    "avg": self.urban_rental_avg,
                    "max": self.urban_rental_max,
                },
                "suburban": {
                    "min": self.suburban_rental_min,
                    "avg": self.suburban_rental_avg,
                    "max": self.suburban_rental_max,
                },
                "rural": {
                    "min": self.rural_rental_min,
                    "avg": self.rural_rental_avg,
                    "max": self.rural_rental_max,
                },
            },
        }

    @classmethod
    def get_settings(cls):
        """Get current settings or create default if none exist"""
        settings = cls.query.first()
        if not settings:
            from app import db

            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings


class AiAnalysisVariant(db.Model):
    """Stores alternative AI analyses per land (e.g., Claude vs ChatGPT)."""

    __tablename__ = "ai_analysis_variants"

    id = db.Column(db.Integer, primary_key=True)
    land_id = db.Column(
        db.Integer,
        db.ForeignKey("lands.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(
        db.String(32), nullable=False, index=True
    )  # 'claude', 'openai'
    model = db.Column(db.String(128), nullable=True)
    analysis = db.Column(JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    land = db.relationship(
        "Land", backref=db.backref("ai_analysis_variants", lazy="dynamic")
    )

    __table_args__ = (
        db.Index("ix_ai_analysis_variants_land_provider", "land_id", "provider"),
    )

    def __repr__(self):
        return f"<AiAnalysisVariant land={self.land_id} provider={self.provider}>"


class PropertyAiAnalysisVariant(db.Model):
    """Stores alternative AI analyses per Property (e.g., Claude vs ChatGPT)."""

    __tablename__ = "property_ai_analysis_variants"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(
        db.Integer,
        db.ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(
        db.String(32), nullable=False, index=True
    )  # 'claude', 'openai'
    model = db.Column(db.String(128), nullable=True)
    analysis = db.Column(JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    property = db.relationship(
        "Property", backref=db.backref("ai_analysis_variants", lazy="dynamic")
    )

    __table_args__ = (
        db.Index(
            "ix_property_ai_analysis_variants_property_provider",
            "property_id",
            "provider",
        ),
    )

    def __repr__(self):
        return f"<PropertyAiAnalysisVariant property={self.property_id} provider={self.provider}>"
