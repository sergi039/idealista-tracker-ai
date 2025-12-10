"""
Tests for database models.
"""

import pytest
from decimal import Decimal
from datetime import datetime
from app import create_app, db
from models import Land, ScoringCriteria
from tests import setup_test_environment


@pytest.fixture
def app():
    """Create test Flask application"""
    setup_test_environment()
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    """Create test client"""
    return app.test_client()


class TestLandModel:
    """Test cases for Land model"""
    
    def test_land_creation(self, app):
        """Test creating a new land record"""
        with app.app_context():
            land = Land(
                source_email_id='test_email_123',
                title='Beautiful Land in Valencia',
                url='https://www.idealista.com/inmueble/123456',
                price=Decimal('150000.00'),
                area=Decimal('1500.00'),
                municipality='Valencia',
                location_lat=Decimal('39.4699'),
                location_lon=Decimal('-0.3763'),
                land_type='developed',
                description='Amazing piece of land with sea views',
                legal_status='Developed'
            )
            
            db.session.add(land)
            db.session.commit()
            
            # Test that the land was created
            assert land.id is not None
            assert land.title == 'Beautiful Land in Valencia'
            assert land.price == Decimal('150000.00')
            assert land.area == Decimal('1500.00')
            assert land.land_type == 'developed'
            assert land.created_at is not None
    
    def test_land_type_constraint(self, app):
        """Test land type constraint"""
        with app.app_context():
            # Valid land types should work
            land1 = Land(
                source_email_id='test_email_dev',
                land_type='developed'
            )
            land2 = Land(
                source_email_id='test_email_build',
                land_type='buildable'
            )
            
            db.session.add(land1)
            db.session.add(land2)
            db.session.commit()
            
            assert land1.land_type == 'developed'
            assert land2.land_type == 'buildable'
    
    def test_unique_source_email_id(self, app):
        """Test unique constraint on source_email_id"""
        with app.app_context():
            land1 = Land(
                source_email_id='duplicate_email',
                title='First Land'
            )
            land2 = Land(
                source_email_id='duplicate_email',
                title='Second Land'
            )
            
            db.session.add(land1)
            db.session.commit()
            
            # Adding second land with same email ID should fail
            db.session.add(land2)
            with pytest.raises(Exception):  # IntegrityError
                db.session.commit()
    
    def test_jsonb_fields(self, app):
        """Test JSONB fields functionality"""
        with app.app_context():
            infrastructure_data = {
                'electricity': True,
                'water': True,
                'internet': False,
                'gas': True
            }
            
            transport_data = {
                'train_station_distance': 2500,
                'airport_distance': 45000,
                'highway_access': True
            }
            
            land = Land(
                source_email_id='jsonb_test',
                infrastructure_basic=infrastructure_data,
                transport=transport_data
            )
            
            db.session.add(land)
            db.session.commit()
            
            # Test that JSONB data was stored correctly
            assert land.infrastructure_basic == infrastructure_data
            assert land.transport == transport_data
            assert land.infrastructure_basic['electricity'] is True
            assert land.transport['train_station_distance'] == 2500
    
    def test_to_dict_method(self, app):
        """Test Land.to_dict() method"""
        with app.app_context():
            land = Land(
                source_email_id='to_dict_test',
                title='Test Land',
                price=Decimal('100000.00'),
                area=Decimal('1000.00'),
                municipality='Test City',
                location_lat=Decimal('40.0'),
                location_lon=Decimal('-3.0'),
                land_type='buildable',
                score_total=Decimal('75.5')
            )
            
            db.session.add(land)
            db.session.commit()
            
            land_dict = land.to_dict()
            
            assert isinstance(land_dict, dict)
            assert land_dict['title'] == 'Test Land'
            assert land_dict['price'] == 100000.0
            assert land_dict['area'] == 1000.0
            assert land_dict['land_type'] == 'buildable'
            assert land_dict['score_total'] == 75.5
            assert 'created_at' in land_dict
            assert isinstance(land_dict['infrastructure_basic'], dict)


class TestScoringCriteriaModel:
    """Test cases for ScoringCriteria model"""
    
    def test_scoring_criteria_creation(self, app):
        """Test creating scoring criteria"""
        with app.app_context():
            criteria = ScoringCriteria(
                criteria_name='infrastructure_basic',
                weight=Decimal('0.20'),
                active=True
            )
            
            db.session.add(criteria)
            db.session.commit()
            
            assert criteria.id is not None
            assert criteria.criteria_name == 'infrastructure_basic'
            assert criteria.weight == Decimal('0.20')
            assert criteria.active is True
            assert criteria.created_at is not None
            assert criteria.updated_at is not None
    
    def test_default_values(self, app):
        """Test default values for ScoringCriteria"""
        with app.app_context():
            criteria = ScoringCriteria(criteria_name='test_criteria')
            
            db.session.add(criteria)
            db.session.commit()
            
            assert criteria.weight == Decimal('1.0')
            assert criteria.active is True
    
    def test_criteria_repr(self, app):
        """Test string representation of ScoringCriteria"""
        with app.app_context():
            criteria = ScoringCriteria(
                criteria_name='transport',
                weight=Decimal('0.15')
            )
            
            db.session.add(criteria)
            db.session.commit()
            
            repr_str = repr(criteria)
            assert 'transport' in repr_str
            assert '0.15' in repr_str


class TestModelRelationships:
    """Test model relationships and queries"""
    
    def test_land_query_filtering(self, app):
        """Test querying and filtering lands"""
        with app.app_context():
            # Create test lands
            land1 = Land(
                source_email_id='filter_test_1',
                title='Developed Land Valencia',
                municipality='Valencia',
                land_type='developed',
                price=Decimal('200000')
            )
            land2 = Land(
                source_email_id='filter_test_2',
                title='Buildable Land Madrid',
                municipality='Madrid',
                land_type='buildable',
                price=Decimal('150000')
            )
            land3 = Land(
                source_email_id='filter_test_3',
                title='Another Valencia Property',
                municipality='Valencia',
                land_type='buildable',
                price=Decimal('180000')
            )
            
            db.session.add_all([land1, land2, land3])
            db.session.commit()
            
            # Test filtering by land type
            developed_lands = Land.query.filter_by(land_type='developed').all()
            assert len(developed_lands) == 1
            assert developed_lands[0].title == 'Developed Land Valencia'
            
            # Test filtering by municipality
            valencia_lands = Land.query.filter_by(municipality='Valencia').all()
            assert len(valencia_lands) == 2
            
            # Test filtering by price range
            affordable_lands = Land.query.filter(Land.price <= 160000).all()
            assert len(affordable_lands) == 1
            assert affordable_lands[0].municipality == 'Madrid'
    
    def test_land_sorting(self, app):
        """Test sorting land records"""
        with app.app_context():
            # Create lands with different scores
            land1 = Land(
                source_email_id='sort_test_1',
                title='Low Score Land',
                score_total=Decimal('45.0')
            )
            land2 = Land(
                source_email_id='sort_test_2',
                title='High Score Land',
                score_total=Decimal('85.0')
            )
            land3 = Land(
                source_email_id='sort_test_3',
                title='Medium Score Land',
                score_total=Decimal('65.0')
            )
            
            db.session.add_all([land1, land2, land3])
            db.session.commit()
            
            # Test sorting by score descending
            sorted_lands = Land.query.order_by(Land.score_total.desc()).all()
            assert sorted_lands[0].title == 'High Score Land'
            assert sorted_lands[1].title == 'Medium Score Land'
            assert sorted_lands[2].title == 'Low Score Land'
            
            # Test sorting by score ascending
            sorted_lands_asc = Land.query.order_by(Land.score_total.asc()).all()
            assert sorted_lands_asc[0].title == 'Low Score Land'
            assert sorted_lands_asc[2].title == 'High Score Land'


class TestModelValidation:
    """Test model validation and edge cases"""
    
    def test_empty_jsonb_fields(self, app):
        """Test handling of empty JSONB fields"""
        with app.app_context():
            land = Land(
                source_email_id='empty_jsonb_test',
                infrastructure_basic=None,
                transport={}
            )
            
            db.session.add(land)
            db.session.commit()
            
            # Test that empty JSONB fields are handled correctly in to_dict
            land_dict = land.to_dict()
            assert land_dict['infrastructure_basic'] == {}
            assert land_dict['transport'] == {}
    
    def test_none_values_to_dict(self, app):
        """Test to_dict with None values"""
        with app.app_context():
            land = Land(
                source_email_id='none_values_test',
                title='Test Land',
                price=None,
                area=None,
                location_lat=None,
                score_total=None
            )
            
            db.session.add(land)
            db.session.commit()
            
            land_dict = land.to_dict()
            assert land_dict['price'] is None
            assert land_dict['area'] is None
            assert land_dict['location_lat'] is None
            assert land_dict['score_total'] is None
    
    def test_large_numbers(self, app):
        """Test handling of large numeric values"""
        with app.app_context():
            land = Land(
                source_email_id='large_numbers_test',
                price=Decimal('999999999.99'),
                area=Decimal('50000.00'),
                location_lat=Decimal('89.999999'),
                location_lon=Decimal('-179.999999')
            )
            
            db.session.add(land)
            db.session.commit()
            
            assert land.price == Decimal('999999999.99')
            assert land.location_lat == Decimal('89.999999')
            
            # Test to_dict conversion
            land_dict = land.to_dict()
            assert land_dict['price'] == 999999999.99
            assert abs(land_dict['location_lat'] - 89.999999) < 0.000001
