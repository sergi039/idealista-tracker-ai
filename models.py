from datetime import datetime
from enum import Enum
from app import db
from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB

class LandTypeEnum(str, Enum):
    # Match existing database values (lowercase) until migration is applied
    developed = 'developed'
    buildable = 'buildable'

class Land(db.Model):
    __tablename__ = 'lands'
    
    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    email_subject = db.Column(db.Text)  # Original email subject line
    email_sender = db.Column(db.String(255))  # Email sender
    title = db.Column(db.Text)
    url = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), index=True)
    area = db.Column(db.Numeric(10, 2), index=True)
    municipality = db.Column(db.String(255), index=True)
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    location_accuracy = db.Column(db.String(20), default='unknown')  # 'precise', 'approximate', 'unknown'
    land_type = db.Column(db.Enum(LandTypeEnum), nullable=True, index=True)
    description = db.Column(db.Text)
    
    # JSONB fields for complex data
    infrastructure_basic = db.Column(JSONB)  # electricity, water, internet, gas
    infrastructure_extended = db.Column(JSONB)  # supermarket, school, restaurants, hospital
    transport = db.Column(JSONB)  # train, airport, highway, bus
    environment = db.Column(JSONB)  # sea_view, mountain_view, forest, orientation
    neighborhood = db.Column(JSONB)  # new_houses, area_price_level, noise
    services_quality = db.Column(JSONB)  # schools rating, restaurants rating, cafes rating
    
    legal_status = db.Column(db.String(50))
    property_details = db.Column(JSONB)  # AI analysis and property details in JSON format
    ai_analysis = db.Column(JSONB)  # Structured AI analysis with 5 blocks
    enhanced_description = db.Column(JSONB)  # AI-enhanced professional description data
    score_total = db.Column(db.Numeric(5, 2), index=True)
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
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    email_date = db.Column(db.DateTime)  # Date when the email was received
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # Last update time
    
    # Composite indexes for commonly queried field combinations
    __table_args__ = (
        Index('ix_location_coords', 'location_lat', 'location_lon'),  # For exact coordinate lookups
        Index('ix_score_location', 'municipality', 'score_total'),    # Filter by municipality, then sort by score
    )
    
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

class SyncHistory(db.Model):
    __tablename__ = 'sync_history'
    
    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(20), nullable=False)  # 'full', 'incremental'
    backend = db.Column(db.String(20), nullable=False)    # 'imap', 'gmail'
    total_emails_found = db.Column(db.Integer, default=0)
    new_properties_added = db.Column(db.Integer, default=0)
    sync_duration = db.Column(db.Integer)  # Duration in seconds
    status = db.Column(db.String(20), default='completed')  # 'completed', 'failed', 'partial'
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    def __repr__(self):
        return f'<SyncHistory {self.sync_type} - {self.new_properties_added} properties>'
