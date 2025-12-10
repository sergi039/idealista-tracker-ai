"""
Tests for enrichment service functionality.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from decimal import Decimal
from app import create_app, db
from models import Land
from services.enrichment_service import EnrichmentService
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
def enrichment_service():
    """Create EnrichmentService instance"""
    return EnrichmentService()


@pytest.fixture
def test_land(app):
    """Create test land record"""
    with app.app_context():
        land = Land(
            source_email_id='test_enrichment_1',
            title='Test Land for Enrichment',
            municipality='Valencia',
            land_type='developed',
            price=Decimal('150000.00'),
            area=Decimal('1500.00'),
            location_lat=Decimal('39.4699'),
            location_lon=Decimal('-0.3763')
        )
        db.session.add(land)
        db.session.commit()
        return land.id


class TestEnrichmentService:
    """Test cases for EnrichmentService"""
    
    def test_init(self, enrichment_service):
        """Test EnrichmentService initialization"""
        assert hasattr(enrichment_service, 'google_maps_key')
        assert hasattr(enrichment_service, 'google_places_key')
        assert hasattr(enrichment_service, 'osm_overpass_url')
        assert hasattr(enrichment_service, 'geocoding_service')
        assert enrichment_service.osm_overpass_url == "https://overpass-api.de/api/interpreter"
    
    def test_enrich_land_not_found(self, app, enrichment_service):
        """Test enrichment with non-existent land ID"""
        with app.app_context():
            result = enrichment_service.enrich_land(99999)
            assert result is False
    
    @patch('services.enrichment_service.ScoringService')
    def test_enrich_land_no_coordinates(self, mock_scoring, app, enrichment_service):
        """Test enrichment with land that has no coordinates"""
        with app.app_context():
            # Create land without coordinates
            land = Land(
                source_email_id='no_coords_test',
                title='Land Without Coordinates',
                municipality='Unknown Location'
            )
            db.session.add(land)
            db.session.commit()
            
            # Mock geocoding failure
            with patch.object(enrichment_service.geocoding_service, 'geocode_address') as mock_geocode:
                mock_geocode.return_value = None
                
                result = enrichment_service.enrich_land(land.id)
                
                assert result is False
                mock_geocode.assert_called_once()
    
    @patch('services.enrichment_service.ScoringService')
    @patch('services.enrichment_service.EnrichmentService._enrich_with_osm_data')
    @patch('services.enrichment_service.EnrichmentService._enrich_with_google_maps')
    @patch('services.enrichment_service.EnrichmentService._enrich_with_google_places')
    @patch('services.enrichment_service.EnrichmentService._analyze_environment')
    def test_enrich_land_success(self, mock_analyze, mock_places, mock_maps, 
                                mock_osm, mock_scoring, app, enrichment_service, test_land):
        """Test successful land enrichment"""
        with app.app_context():
            # Mock scoring service
            mock_scoring_instance = Mock()
            mock_scoring.return_value = mock_scoring_instance
            
            result = enrichment_service.enrich_land(test_land)
            
            assert result is True
            
            # Verify all enrichment methods were called
            mock_places.assert_called_once()
            mock_maps.assert_called_once()
            mock_osm.assert_called_once()
            mock_analyze.assert_called_once()
            mock_scoring_instance.calculate_score.assert_called_once()
    
    @patch('services.enrichment_service.requests.get')
    def test_enrich_with_google_places_success(self, mock_get, app, enrichment_service, test_land):
        """Test Google Places enrichment"""
        with app.app_context():
            # Mock successful API response
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                'results': [
                    {
                        'name': 'Test Supermarket',
                        'rating': 4.2,
                        'place_id': 'test_place_id',
                        'types': ['supermarket'],
                        'geometry': {
                            'location': {
                                'lat': 39.4700,
                                'lng': -0.3760
                            }
                        }
                    }
                ]
            }
            mock_get.return_value = mock_response
            
            # Set API key for test
            enrichment_service.google_places_key = 'test_places_key'
            
            land = Land.query.get(test_land)
            enrichment_service._enrich_with_google_places(land)
            
            # Check that infrastructure_extended was updated
            assert land.infrastructure_extended is not None
            assert 'supermarket_available' in land.infrastructure_extended
    
    def test_enrich_with_google_places_no_api_key(self, app, enrichment_service, test_land):
        """Test Google Places enrichment without API key"""
        with app.app_context():
            enrichment_service.google_places_key = ""
            
            land = Land.query.get(test_land)
            original_infrastructure = land.infrastructure_extended
            
            enrichment_service._enrich_with_google_places(land)
            
            # Should not change anything without API key
            assert land.infrastructure_extended == original_infrastructure
    
    @patch('services.enrichment_service.requests.get')
    def test_enrich_with_google_maps_success(self, mock_get, app, enrichment_service, test_land):
        """Test Google Maps enrichment"""
        with app.app_context():
            # Mock successful distance matrix response
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                'rows': [{
                    'elements': [{
                        'status': 'OK',
                        'distance': {'value': 25000},
                        'duration': {'value': 1800}
                    }]
                }]
            }
            mock_get.return_value = mock_response
            
            # Set API key for test
            enrichment_service.google_maps_key = 'test_maps_key'
            
            land = Land.query.get(test_land)
            enrichment_service._enrich_with_google_maps(land)
            
            # Check that transport data was updated
            assert land.transport is not None
            assert any('distance_to_' in key for key in land.transport.keys())
    
    @patch('services.enrichment_service.requests.post')
    def test_enrich_with_osm_data_success(self, mock_post, app, enrichment_service, test_land):
        """Test OSM data enrichment"""
        with app.app_context():
            # Mock successful OSM response
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                'elements': [
                    {
                        'tags': {
                            'amenity': 'supermarket',
                            'name': 'OSM Supermarket'
                        }
                    },
                    {
                        'tags': {
                            'amenity': 'school',
                            'name': 'OSM School'
                        }
                    }
                ]
            }
            mock_post.return_value = mock_response
            
            land = Land.query.get(test_land)
            enrichment_service._enrich_with_osm_data(land)
            
            # Check that OSM data was stored
            assert land.infrastructure_extended is not None
            assert 'osm_amenities' in land.infrastructure_extended
            assert land.infrastructure_extended['osm_amenities']['supermarket'] == 1
            assert land.infrastructure_extended['osm_amenities']['school'] == 1
    
    def test_analyze_environment_sea_view(self, app, enrichment_service, test_land):
        """Test environment analysis for sea view detection"""
        with app.app_context():
            land = Land.query.get(test_land)
            land.description = "Parcela con vistas al mar y orientación sur"
            
            enrichment_service._analyze_environment(land)
            
            assert land.environment is not None
            assert land.environment.get('sea_view') is True
            assert land.environment.get('orientation') == 'south'
    
    def test_analyze_environment_mountain_view(self, app, enrichment_service, test_land):
        """Test environment analysis for mountain view detection"""
        with app.app_context():
            land = Land.query.get(test_land)
            land.description = "Terreno con vista a la montaña y orientación norte"
            
            enrichment_service._analyze_environment(land)
            
            assert land.environment is not None
            assert land.environment.get('mountain_view') is True
            assert land.environment.get('orientation') == 'north'
    
    def test_analyze_environment_forest_view(self, app, enrichment_service, test_land):
        """Test environment analysis for forest view detection"""
        with app.app_context():
            land = Land.query.get(test_land)
            land.description = "Parcela rodeada de bosque con mucho verde"
            
            enrichment_service._analyze_environment(land)
            
            assert land.environment is not None
            assert land.environment.get('forest_view') is True
    
    def test_calculate_distance(self, enrichment_service):
        """Test distance calculation using Haversine formula"""
        # Test known coordinates (Valencia to Madrid approximately)
        valencia_lat, valencia_lon = 39.4699, -0.3763
        madrid_lat, madrid_lon = 40.4168, -3.7038
        
        distance = enrichment_service._calculate_distance(
            valencia_lat, valencia_lon, madrid_lat, madrid_lon
        )
        
        # Distance should be approximately 302 km (302000 meters)
        assert 300000 < distance < 310000
    
    def test_calculate_distance_same_point(self, enrichment_service):
        """Test distance calculation for same point"""
        distance = enrichment_service._calculate_distance(
            39.4699, -0.3763, 39.4699, -0.3763
        )
        
        assert distance == 0.0
    
    @patch('services.enrichment_service.requests.get')
    def test_search_nearby_places_success(self, mock_get, enrichment_service):
        """Test successful nearby places search"""
        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'results': [
                {
                    'name': 'Test Restaurant',
                    'rating': 4.5,
                    'place_id': 'test_place_id',
                    'types': ['restaurant'],
                    'geometry': {
                        'location': {
                            'lat': 39.4700,
                            'lng': -0.3760
                        }
                    }
                }
            ]
        }
        mock_get.return_value = mock_response
        
        # Set API key
        enrichment_service.google_places_key = 'test_key'
        
        places = enrichment_service._search_nearby_places(
            39.4699, -0.3763, ['restaurant']
        )
        
        assert len(places) == 1
        assert places[0]['name'] == 'Test Restaurant'
        assert places[0]['rating'] == 4.5
        assert 'distance' in places[0]
    
    @patch('services.enrichment_service.requests.get')
    def test_get_distance_matrix_success(self, mock_get, enrichment_service):
        """Test successful distance matrix request"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'rows': [{
                'elements': [{
                    'status': 'OK',
                    'distance': {'value': 15000},
                    'duration': {'value': 1200}
                }]
            }]
        }
        mock_get.return_value = mock_response
        
        # Set API key
        enrichment_service.google_maps_key = 'test_key'
        
        result = enrichment_service._get_distance_matrix(
            39.4699, -0.3763, "Valencia city center, Spain"
        )
        
        assert result is not None
        assert result['distance'] == 15000
        assert result['duration'] == 1200
    
    @patch('services.enrichment_service.requests.get')
    def test_get_distance_matrix_failure(self, mock_get, enrichment_service):
        """Test distance matrix request failure"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'rows': [{
                'elements': [{
                    'status': 'NOT_FOUND'
                }]
            }]
        }
        mock_get.return_value = mock_response
        
        enrichment_service.google_maps_key = 'test_key'
        
        result = enrichment_service._get_distance_matrix(
            39.4699, -0.3763, "Invalid destination"
        )
        
        assert result is None
    
    def test_enrich_land_exception_handling(self, app, enrichment_service, test_land):
        """Test exception handling in enrich_land"""
        with app.app_context():
            # Cause an exception by making geocoding service None
            enrichment_service.geocoding_service = None
            
            result = enrichment_service.enrich_land(test_land)
            
            assert result is False
