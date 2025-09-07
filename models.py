from datetime import datetime
from app import db
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB

class Land(db.Model):
    __tablename__ = 'lands'
    
    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    title = db.Column(db.Text)
    url = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2))
    area = db.Column(db.Numeric(10, 2))
    municipality = db.Column(db.String(255))
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    land_type = db.Column(db.String(20), CheckConstraint("land_type IN ('developed', 'buildable')"))
    description = db.Column(db.Text)
    
    # JSONB fields for complex data
    infrastructure_basic = db.Column(JSONB)  # electricity, water, internet, gas
    infrastructure_extended = db.Column(JSONB)  # supermarket, school, restaurants, hospital
    transport = db.Column(JSONB)  # train, airport, highway, bus
    environment = db.Column(JSONB)  # sea_view, mountain_view, forest, orientation
    neighborhood = db.Column(JSONB)  # new_houses, area_price_level, noise
    services_quality = db.Column(JSONB)  # schools rating, restaurants rating, cafes rating
    
    legal_status = db.Column(db.String(50))
    property_details = db.Column(db.Text)  # Full property description from Idealista
    score_total = db.Column(db.Numeric(5, 2))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
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
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class ScoringCriteria(db.Model):
    __tablename__ = 'scoring_criteria'
    
    id = db.Column(db.Integer, primary_key=True)
    criteria_name = db.Column(db.String(100), nullable=False)
    weight = db.Column(db.Numeric(3, 2), default=1.0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<ScoringCriteria {self.criteria_name}: {self.weight}>'
