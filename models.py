from datetime import datetime
from app import db
from sqlalchemy import CheckConstraint
from sqlalchemy.types import JSON

class Land(db.Model):
    __tablename__ = 'lands'
    __table_args__ = (
        db.Index('ix_lands_land_type', 'land_type'),
        db.Index('ix_lands_municipality', 'municipality'),
        db.Index('ix_lands_listing_status', 'listing_status'),
        db.Index('ix_lands_is_favorite', 'is_favorite'),
        db.Index('ix_lands_created_at', 'created_at'),
        db.Index('ix_lands_score_total', 'score_total'),
        db.Index('ix_lands_score_investment', 'score_investment'),
        db.Index('ix_lands_score_lifestyle', 'score_lifestyle'),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    email_subject = db.Column(db.Text)  # Original email subject line
    email_sender = db.Column(db.String(255))  # Email sender
    title = db.Column(db.Text)
    url = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2))
    area = db.Column(db.Numeric(10, 2))
    municipality = db.Column(db.String(255))
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    location_accuracy = db.Column(db.String(20), default='unknown')  # 'precise', 'approximate', 'unknown'
    land_type = db.Column(db.String(20), CheckConstraint("land_type IN ('developed', 'buildable')"))
    description = db.Column(db.Text)
    
    # JSON fields for complex data (works with both PostgreSQL and SQLite)
    infrastructure_basic = db.Column(JSON)  # electricity, water, internet, gas
    infrastructure_extended = db.Column(JSON)  # supermarket, school, restaurants, hospital
    transport = db.Column(JSON)  # train, airport, highway, bus
    environment = db.Column(JSON)  # sea_view, mountain_view, forest, orientation
    neighborhood = db.Column(JSON)  # new_houses, area_price_level, noise
    services_quality = db.Column(JSON)  # schools rating, restaurants rating, cafes rating
    
    legal_status = db.Column(db.String(50))
    property_details = db.Column(JSON)  # AI analysis and property details in JSON format
    ai_analysis = db.Column(JSON)  # Structured AI analysis with 5 blocks
    enhanced_description = db.Column(JSON)  # AI-enhanced professional description data
    score_total = db.Column(db.Numeric(5, 2))
    score_investment = db.Column(db.Numeric(5, 2))  # Investment-focused score (0-100)
    score_lifestyle = db.Column(db.Numeric(5, 2))   # Lifestyle-focused score (0-100)
    
    # Travel times by car (in minutes)
    travel_time_oviedo = db.Column(db.Integer)  # Time to Oviedo in minutes
    travel_time_gijon = db.Column(db.Integer)   # Time to Gijon in minutes
    travel_time_nearest_beach = db.Column(db.Integer)  # Time to nearest beach in minutes
    nearest_beach_name = db.Column(db.String(255))     # Name of nearest beach
    
    # Priority infrastructure travel times (in minutes)
    travel_time_airport = db.Column(db.Integer)     # Time to nearest airport
    travel_time_train_station = db.Column(db.Integer)  # Time to nearest train station
    travel_time_hospital = db.Column(db.Integer)    # Time to nearest hospital
    travel_time_police = db.Column(db.Integer)      # Time to nearest police station
    
    # Priority infrastructure distances (in kilometers)
    distance_airport = db.Column(db.Integer)        # Distance to nearest airport in km
    distance_train_station = db.Column(db.Integer)  # Distance to nearest train station in km
    distance_hospital = db.Column(db.Integer)       # Distance to nearest hospital in km
    distance_police = db.Column(db.Integer)         # Distance to nearest police station in km
    
    # Price history tracking
    previous_price = db.Column(db.Numeric(10, 2))  # Previous price before update
    price_change_amount = db.Column(db.Numeric(10, 2))  # Amount of price change (negative for decrease)
    price_change_percentage = db.Column(db.Numeric(5, 2))  # Percentage change
    price_changed_date = db.Column(db.DateTime)  # When price was last changed
    
    # Favorites
    is_favorite = db.Column(db.Boolean, default=False)  # Mark property as favorite

    # Listing status tracking
    listing_status = db.Column(db.String(20), default='active')  # 'active', 'removed', 'sold', 'unknown'
    listing_removed_date = db.Column(db.DateTime)  # When listing was removed from Idealista
    listing_last_checked = db.Column(db.DateTime)  # Last time we checked the listing status

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    email_date = db.Column(db.DateTime)  # Date when the email was received
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # Last update time
    
    def __repr__(self):
        return f'<Land {self.id}: {self.title[:50]}...>'
    
    def to_dict(self):
        """Convert land to dictionary for API responses"""
        return {
            'id': self.id,
            'source_email_id': self.source_email_id,
            'title': self.title,
            'url': self.url,
            'price': float(self.price) if self.price else None,
            'area': float(self.area) if self.area else None,
            'municipality': self.municipality,
            'location_lat': float(self.location_lat) if self.location_lat else None,
            'location_lon': float(self.location_lon) if self.location_lon else None,
            'location_accuracy': self.location_accuracy,
            'land_type': self.land_type,
            'description': self.description,
            'infrastructure_basic': self.infrastructure_basic or {},
            'infrastructure_extended': self.infrastructure_extended or {},
            'transport': self.transport or {},
            'environment': self.environment or {},
            'neighborhood': self.neighborhood or {},
            'services_quality': self.services_quality or {},
            'legal_status': self.legal_status,
            'score_total': float(self.score_total) if self.score_total else None,
            'score_investment': float(self.score_investment) if self.score_investment else None,
            'score_lifestyle': float(self.score_lifestyle) if self.score_lifestyle else None,
            'travel_time_oviedo': self.travel_time_oviedo,
            'travel_time_gijon': self.travel_time_gijon,
            'travel_time_nearest_beach': self.travel_time_nearest_beach,
            'nearest_beach_name': self.nearest_beach_name,
            'is_favorite': self.is_favorite or False,
            'listing_status': self.listing_status or 'active',
            'listing_removed_date': self.listing_removed_date.isoformat() if self.listing_removed_date else None,
            'listing_last_checked': self.listing_last_checked.isoformat() if self.listing_last_checked else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class ScoringCriteria(db.Model):
    __tablename__ = 'scoring_criteria'
    
    id = db.Column(db.Integer, primary_key=True)
    criteria_name = db.Column(db.String(100), nullable=False)
    profile = db.Column(db.String(20), nullable=True, default='combined')  # 'investment', 'lifestyle', 'combined'
    weight = db.Column(db.Numeric(3, 2), default=1.0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Unique constraint for criteria_name + profile combination
    __table_args__ = (
        db.UniqueConstraint('criteria_name', 'profile', name='uq_criteria_profile'),
    )
    
    def __repr__(self):
        return f'<ScoringCriteria {self.criteria_name}[{self.profile}]: {self.weight}>'

class LandHistory(db.Model):
    """Tracks changes to favorite properties over time"""
    __tablename__ = 'land_history'

    id = db.Column(db.Integer, primary_key=True)
    land_id = db.Column(db.Integer, db.ForeignKey('lands.id', ondelete='CASCADE'), nullable=False)
    snapshot_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

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
    land = db.relationship('Land', backref=db.backref('history', lazy='dynamic', order_by='LandHistory.snapshot_date.desc()'))

    def __repr__(self):
        return f'<LandHistory {self.land_id} - {self.change_type} @ {self.snapshot_date}>'

    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            'id': self.id,
            'land_id': self.land_id,
            'snapshot_date': self.snapshot_date.isoformat() if self.snapshot_date else None,
            'price': float(self.price) if self.price else None,
            'title': self.title,
            'description': self.description,
            'area': float(self.area) if self.area else None,
            'land_type': self.land_type,
            'url': self.url,
            'change_type': self.change_type,
            'price_previous': float(self.price_previous) if self.price_previous else None,
            'price_change_amount': float(self.price_change_amount) if self.price_change_amount else None,
            'price_change_percentage': float(self.price_change_percentage) if self.price_change_percentage else None
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
            price_previous=price_previous
        )

        # Calculate price change if previous price provided
        if price_previous and land.price:
            snapshot.price_change_amount = float(land.price) - float(price_previous)
            if float(price_previous) > 0:
                snapshot.price_change_percentage = (snapshot.price_change_amount / float(price_previous)) * 100

        return snapshot


class SyncHistory(db.Model):
    __tablename__ = 'sync_history'

    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(20), nullable=False)  # 'full', 'incremental'
    backend = db.Column(db.String(20), nullable=False)    # 'imap', 'gmail'
    total_emails_found = db.Column(db.Integer, default=0)
    new_properties_added = db.Column(db.Integer, default=0)
    price_updated_count = db.Column(db.Integer, default=0)  # Properties with price changes
    expired_count = db.Column(db.Integer, default=0)  # Properties marked as expired/removed
    sync_duration = db.Column(db.Integer)  # Duration in seconds
    status = db.Column(db.String(20), default='completed')  # 'completed', 'failed', 'partial'
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    def __repr__(self):
        return f'<SyncHistory {self.sync_type} - {self.new_properties_added} properties>'


class AppSetting(db.Model):
    __tablename__ = 'app_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    value = db.Column(JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<AppSetting {self.key}>"
