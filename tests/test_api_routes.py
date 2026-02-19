"""
Tests for API routes functionality.
"""

import pytest
import json
from unittest.mock import Mock, patch
from decimal import Decimal
from app import create_app, db
from models import Land, Property, PropertyAiAnalysisVariant, ScoringCriteria, SearchProfile
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


@pytest.fixture
def test_lands(app):
    """Create test land records"""
    with app.app_context():
        lands = [
            Land(
                source_email_id='api_test_1',
                title='Test Land Valencia',
                municipality='Valencia',
                land_type='developed',
                price=Decimal('150000.00'),
                area=Decimal('1500.00'),
                score_total=Decimal('85.5')
            ),
            Land(
                source_email_id='api_test_2',
                title='Test Land Madrid',
                municipality='Madrid',
                land_type='buildable',
                price=Decimal('200000.00'),
                area=Decimal('2000.00'),
                score_total=Decimal('72.3')
            ),
            Land(
                source_email_id='api_test_3',
                title='Test Land Barcelona',
                municipality='Barcelona',
                land_type='developed',
                price=Decimal('300000.00'),
                area=Decimal('1200.00'),
                score_total=Decimal('91.2')
            )
        ]
        db.session.add_all(lands)
        db.session.commit()
        return [land.id for land in lands]


@pytest.fixture
def test_criteria(app):
    """Create test scoring criteria"""
    with app.app_context():
        criteria = [
            ScoringCriteria(criteria_name='infrastructure_basic', weight=Decimal('0.20')),
            ScoringCriteria(criteria_name='transport', weight=Decimal('0.25')),
            ScoringCriteria(criteria_name='environment', weight=Decimal('0.15'))
        ]
        db.session.add_all(criteria)
        db.session.commit()
        return criteria


class TestAPIHealthCheck:
    """Test API health check endpoint"""
    
    def test_health_check(self, client):
        """Test health check endpoint"""
        response = client.get('/api/healthz')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['ok'] is True


class TestManualIngestion:
    """Test manual ingestion endpoint"""
    
    @patch('services.property_imap_service.PropertyIMAPService')
    def test_manual_ingestion_success(self, mock_imap_service, client):
        """Test successful manual ingestion"""
        # Mock IMAP service
        mock_instance = Mock()
        mock_imap_service.return_value = mock_instance
        mock_instance.run_ingestion.return_value = 5
        
        response = client.post('/api/ingest/email/run')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['processed_count'] == 5
        assert 'Successfully processed' in data['message']

    @patch('utils.auth.check_admin_auth', return_value=False)
    def test_manual_ingestion_full_requires_admin(self, mock_check_admin, client):
        response = client.post('/api/ingest/email/run', json={'sync_type': 'full'})
        assert response.status_code == 401
        data = json.loads(response.data)
        assert data['success'] is False

    @patch('utils.auth.check_admin_auth', return_value=True)
    @patch('services.property_imap_service.PropertyIMAPService')
    def test_manual_ingestion_full_uses_run_full_sync(self, mock_imap_service, mock_check_admin, client):
        mock_instance = Mock()
        mock_imap_service.return_value = mock_instance
        mock_instance.run_full_sync.return_value = 7

        response = client.post('/api/ingest/email/run', json={'sync_type': 'full'})
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['sync_type'] == 'full'
        assert data['processed_count'] == 7
        mock_instance.run_full_sync.assert_called_once()

    @patch('services.property_imap_service.PropertyIMAPService')
    def test_manual_ingestion_failure(self, mock_imap_service, client):
        """Test manual ingestion failure"""
        # Mock IMAP service to raise exception
        mock_instance = Mock()
        mock_imap_service.return_value = mock_instance
        mock_instance.run_ingestion.side_effect = Exception("IMAP error")
        
        response = client.post('/api/ingest/email/run')
        
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False


class TestPropertyFavorites:
    def test_toggle_property_favorite(self, client, app):
        with app.app_context():
            prop = Property(
                source_email_id="fav_prop_1",
                title="Test Property",
                municipality="Madrid",
            )
            db.session.add(prop)
            db.session.commit()
            prop_id = prop.id

        response = client.post(f"/api/property/{prop_id}/favorite")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["is_favorite"] is True

        response2 = client.post(f"/api/property/{prop_id}/favorite")
        assert response2.status_code == 200
        data2 = json.loads(response2.data)
        assert data2["success"] is True
        assert data2["is_favorite"] is False


class TestPropertyStatus:
    def test_set_property_status_success(self, client, app):
        with app.app_context():
            prop = Property(
                source_email_id="status_prop_1",
                title="Status Test Property",
                municipality="Madrid",
            )
            db.session.add(prop)
            db.session.commit()
            prop_id = prop.id

        response = client.post(f"/api/property/{prop_id}/set-status", json={"status": "removed"})
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["status"] == "removed"

        with app.app_context():
            refreshed = db.session.get(Property, prop_id)
            assert refreshed is not None
            assert refreshed.listing_status == "removed"
            assert refreshed.listing_last_checked is not None
            assert refreshed.listing_removed_date is not None

        response2 = client.post(f"/api/property/{prop_id}/set-status", json={"status": "active"})
        assert response2.status_code == 200
        data2 = json.loads(response2.data)
        assert data2["success"] is True
        assert data2["status"] == "active"

        with app.app_context():
            refreshed2 = db.session.get(Property, prop_id)
            assert refreshed2 is not None
            assert refreshed2.listing_status == "active"
            assert refreshed2.listing_removed_date is None

    def test_set_property_status_invalid(self, client, app):
        with app.app_context():
            prop = Property(
                source_email_id="status_prop_2",
                title="Status Invalid Test Property",
                municipality="Madrid",
            )
            db.session.add(prop)
            db.session.commit()
            prop_id = prop.id

        response = client.post(f"/api/property/{prop_id}/set-status", json={"status": "bad_status"})
        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False


class TestPropertiesCSVExport:
    def test_export_properties_csv(self, client, app):
        with app.app_context():
            from services.search_profile_service import SearchProfileService

            profile = SearchProfileService.get_default_profile(create=True)
            assert profile is not None
            profile_id = profile.id

            prop = Property(
                source_email_id="csv_prop_1",
                title="CSV Export Property",
                municipality="Madrid",
                search_profile_id=profile_id,
            )
            db.session.add(prop)
            db.session.commit()
            prop_id = prop.id

        response = client.get(f"/properties/export.csv?profile_id={profile_id}")
        assert response.status_code == 200
        assert response.headers.get("Content-Type", "").startswith("text/csv")
        assert "idealista_properties.csv" in response.headers.get("Content-Disposition", "")

        csv_text = response.data.decode("utf-8", errors="ignore")
        assert "ID,Profile ID,Title,URL" in csv_text
        assert str(prop_id) in csv_text


class TestPropertiesAPI:
    def test_get_properties_defaults_to_default_profile(self, client, app):
        with app.app_context():
            from services.search_profile_service import SearchProfileService

            default_profile = SearchProfileService.get_default_profile(create=True)
            assert default_profile is not None
            default_profile_id = default_profile.id

            other_profile = SearchProfile(name="Other", is_active=True, is_default=False, travel_targets={"presets": {}, "custom": []})
            db.session.add(other_profile)
            db.session.commit()

            p1 = Property(source_email_id="api_prop_1", title="A", municipality="Madrid", search_profile_id=default_profile.id)
            p2 = Property(source_email_id="api_prop_2", title="B", municipality="Madrid", search_profile_id=other_profile.id)
            db.session.add_all([p1, p2])
            db.session.commit()

        response = client.get("/api/properties")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["count"] == 1
        assert data["selected_profile_id"] == default_profile_id
        assert data["properties"][0]["title"] == "A"

    def test_get_properties_filter_and_hide_removed(self, client, app):
        with app.app_context():
            from services.search_profile_service import SearchProfileService

            default_profile = SearchProfileService.get_default_profile(create=True)
            assert default_profile is not None

            active = Property(
                source_email_id="api_prop_active",
                title="Active Apartment",
                municipality="Madrid",
                search_profile_id=default_profile.id,
                property_category="housing",
                property_subtype="apartment",
                listing_status="active",
            )
            removed = Property(
                source_email_id="api_prop_removed",
                title="Removed Apartment",
                municipality="Madrid",
                search_profile_id=default_profile.id,
                property_category="housing",
                property_subtype="apartment",
                listing_status="removed",
            )
            db.session.add_all([active, removed])
            db.session.commit()

        # Default hides removed.
        response = client.get("/api/properties?category=housing&subtype=apartment")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["count"] == 1
        assert data["properties"][0]["title"] == "Active Apartment"

        # Disable hide_removed -> both appear.
        response2 = client.get("/api/properties?category=housing&subtype=apartment&hide_removed=0")
        assert response2.status_code == 200
        data2 = json.loads(response2.data)
        assert data2["success"] is True
        assert data2["count"] == 2

    def test_get_properties_filter_unclassified(self, client, app):
        with app.app_context():
            from services.search_profile_service import SearchProfileService

            default_profile = SearchProfileService.get_default_profile(create=True)
            assert default_profile is not None

            unclassified = Property(
                source_email_id="api_prop_unclassified",
                title="Unclassified",
                municipality="Madrid",
                search_profile_id=default_profile.id,
                property_category=None,
                property_subtype=None,
                listing_status="active",
            )
            classified = Property(
                source_email_id="api_prop_classified",
                title="Classified Apartment",
                municipality="Madrid",
                search_profile_id=default_profile.id,
                property_category="housing",
                property_subtype="apartment",
                listing_status="active",
            )
            db.session.add_all([unclassified, classified])
            db.session.commit()

        response = client.get("/api/properties?category=__none__&hide_removed=0")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["count"] == 1
        assert data["properties"][0]["title"] == "Unclassified"

    def test_get_property_detail(self, client, app):
        with app.app_context():
            from services.search_profile_service import SearchProfileService

            profile = SearchProfileService.get_default_profile(create=True)
            assert profile is not None

            prop = Property(source_email_id="api_prop_detail_1", title="Detail", municipality="Madrid", search_profile_id=profile.id)
            db.session.add(prop)
            db.session.commit()
            prop_id = prop.id

        response = client.get(f"/api/properties/{prop_id}")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["property"]["id"] == prop_id
        assert data["property"]["title"] == "Detail"

class TestLandsAPI:
    """Test lands API endpoints"""
    
    def test_get_lands_default(self, client, test_lands):
        """Test getting lands with default parameters"""
        response = client.get('/api/lands')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['count'] == 3
        assert len(data['lands']) == 3
        
        # Check that lands are sorted by score_total desc by default
        scores = [land['score_total'] for land in data['lands']]
        assert scores == sorted(scores, reverse=True)
    
    def test_get_lands_with_filter(self, client, test_lands):
        """Test getting lands with land type filter"""
        response = client.get('/api/lands?filter=developed')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['count'] == 2  # Only developed lands
        
        for land in data['lands']:
            assert land['land_type'] == 'developed'
    
    def test_get_lands_with_sorting(self, client, test_lands):
        """Test getting lands with custom sorting"""
        response = client.get('/api/lands?sort=price&order=asc')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        
        # Check ascending price order
        prices = [land['price'] for land in data['lands'] if land['price']]
        assert prices == sorted(prices)
    
    def test_get_lands_with_pagination(self, client, test_lands):
        """Test getting lands with pagination"""
        response = client.get('/api/lands?limit=2&offset=1')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['count'] == 2  # Limited to 2 results
        assert len(data['lands']) == 2
    
    def test_get_lands_invalid_sort(self, client, test_lands):
        """Test getting lands with invalid sort field"""
        response = client.get('/api/lands?sort=invalid_field')
        
        # Should still work, just ignore invalid sort
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True


class TestLandDetailAPI:
    """Test land detail API endpoint"""
    
    def test_get_land_detail_success(self, client, test_lands):
        """Test getting land details successfully"""
        land_id = test_lands[0]
        response = client.get(f'/api/lands/{land_id}')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'land' in data
        assert data['land']['id'] == land_id
        assert data['land']['title'] == 'Test Land Valencia'
    
    def test_get_land_detail_not_found(self, client):
        """Test getting details for non-existent land"""
        response = client.get('/api/lands/99999')
        
        assert response.status_code == 404
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'not found' in data['error'].lower()
    
    def test_get_land_detail_with_score_breakdown(self, client, test_lands):
        """Test getting land details with score breakdown"""
        # Add score breakdown to a land
        with client.application.app_context():
            land = db.session.get(Land, test_lands[0])
            land.environment = {
                'score_breakdown': {
                    'infrastructure_basic': 85.0,
                    'transport': 90.0,
                    'environment': 70.0
                }
            }
            db.session.commit()
        
        response = client.get(f'/api/lands/{test_lands[0]}')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'score_breakdown' in data['land']
        assert data['land']['score_breakdown']['infrastructure_basic'] == 85.0


class TestCriteriaAPI:
    """Test scoring criteria API endpoints"""
    
    @patch('services.scoring_service.ScoringService')
    def test_get_criteria_success(self, mock_scoring_service, client, test_criteria):
        """Test getting current scoring criteria"""
        # Mock scoring service
        mock_instance = Mock()
        mock_scoring_service.return_value = mock_instance
        mock_instance.get_current_weights.return_value = {
            'infrastructure_basic': 0.20,
            'transport': 0.25,
            'environment': 0.15
        }
        
        response = client.get('/api/criteria')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'criteria' in data
        assert data['criteria']['infrastructure_basic'] == 0.20
        assert data['criteria']['transport'] == 0.25
    
    @patch('services.scoring_service.ScoringService')
    def test_update_criteria_success(self, mock_scoring_service, client):
        """Test updating scoring criteria successfully"""
        # Mock scoring service
        mock_instance = Mock()
        mock_scoring_service.return_value = mock_instance
        mock_instance.update_weights.return_value = True
        
        new_criteria = {
            'criteria': {
                'infrastructure_basic': 0.30,
                'transport': 0.20,
                'environment': 0.25
            }
        }
        
        response = client.put(
            '/api/criteria',
            data=json.dumps(new_criteria),
            content_type='application/json'
        )
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'updated successfully' in data['message']
        
        # Verify update_weights was called with correct data
        mock_instance.update_weights.assert_called_once_with(new_criteria['criteria'])
    
    def test_update_criteria_missing_data(self, client):
        """Test updating criteria with missing data"""
        response = client.put(
            '/api/criteria',
            data=json.dumps({}),
            content_type='application/json'
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'Missing criteria data' in data['error']
    
    def test_update_criteria_invalid_weight(self, client):
        """Test updating criteria with invalid weight"""
        invalid_criteria = {
            'criteria': {
                'infrastructure_basic': -0.5  # Negative weight
            }
        }
        
        response = client.put(
            '/api/criteria',
            data=json.dumps(invalid_criteria),
            content_type='application/json'
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'must be a positive number' in data['error']
    
    @patch('services.scoring_service.ScoringService')
    def test_update_criteria_service_failure(self, mock_scoring_service, client):
        """Test updating criteria when service fails"""
        # Mock scoring service failure
        mock_instance = Mock()
        mock_scoring_service.return_value = mock_instance
        mock_instance.update_weights.return_value = False
        
        criteria = {
            'criteria': {
                'infrastructure_basic': 0.25
            }
        }
        
        response = client.put(
            '/api/criteria',
            data=json.dumps(criteria),
            content_type='application/json'
        )
        
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'Failed to update criteria' in data['error']


class TestSchedulerStatus:
    """Test scheduler status endpoint"""
    
    @patch('services.scheduler_service.get_scheduler_status')
    def test_scheduler_status_success(self, mock_get_status, client):
        """Test getting scheduler status successfully"""
        mock_get_status.return_value = {
            'status': 'running',
            'jobs': [
                {
                    'id': 'morning_ingestion',
                    'name': 'Morning Gmail Ingestion',
                    'next_run': '2024-01-15T07:00:00',
                    'trigger': 'cron'
                }
            ]
        }
        
        response = client.get('/api/scheduler/status')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'scheduler' in data
        assert data['scheduler']['status'] == 'running'
        assert len(data['scheduler']['jobs']) == 1
    
    @patch('services.scheduler_service.get_scheduler_status')
    def test_scheduler_status_failure(self, mock_get_status, client):
        """Test scheduler status with exception returns generic error (no leak)"""
        mock_get_status.side_effect = Exception("Scheduler error")

        response = client.get('/api/scheduler/status')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        # Must NOT leak the actual exception message
        assert 'Scheduler error' not in data['error']
        assert 'error' in data


class TestStatsAPI:
    """Test statistics API endpoint"""
    
    def test_get_stats_success(self, client, test_lands):
        """Test getting application statistics"""
        response = client.get('/api/stats')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'stats' in data
        
        stats = data['stats']
        assert stats['total_lands'] == 3
        assert 'land_types' in stats
        assert stats['land_types']['developed'] == 2
        assert stats['land_types']['buildable'] == 1
        assert 'scores' in stats
        assert 'municipality_distribution' in stats
    
    def test_get_stats_empty_database(self, client):
        """Test getting statistics with empty database"""
        response = client.get('/api/stats')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['stats']['total_lands'] == 0
        assert data['stats']['land_types']['developed'] == 0
        assert data['stats']['land_types']['buildable'] == 0
        assert data['stats']['scores']['average'] == 0


class TestAPIErrorHandling:
    """Test API error handling"""
    
    def test_invalid_json(self, client):
        """Test handling of invalid JSON in request"""
        response = client.put(
            '/api/criteria',
            data='invalid json',
            content_type='application/json'
        )
        
        assert response.status_code == 400
    
    def test_method_not_allowed(self, client):
        """Test method not allowed responses"""
        response = client.delete('/api/lands')
        
        assert response.status_code == 405
    
    def test_not_found_endpoint(self, client):
        """Test accessing non-existent endpoint"""
        response = client.get('/api/nonexistent')
        
        assert response.status_code == 404


class TestAPIDataValidation:
    """Test API data validation"""
    
    def test_lands_api_data_types(self, client, test_lands):
        """Test that lands API returns correct data types"""
        response = client.get('/api/lands')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        
        for land in data['lands']:
            # Check required fields exist
            assert 'id' in land
            assert 'source_email_id' in land
            
            # Check data types
            assert isinstance(land['id'], int)
            assert isinstance(land['source_email_id'], str)
            
            if land['price'] is not None:
                assert isinstance(land['price'], (int, float))
            if land['area'] is not None:
                assert isinstance(land['area'], (int, float))
            if land['score_total'] is not None:
                assert isinstance(land['score_total'], (int, float))
    
    def test_criteria_weight_validation(self, client):
        """Test comprehensive criteria weight validation"""
        test_cases = [
            # Invalid weight types
            {'criteria': {'test': 'string'}},
            {'criteria': {'test': None}},
            {'criteria': {'test': [1, 2, 3]}},
            
            # Invalid weight values
            {'criteria': {'test': -1}},
            {'criteria': {'test': 15}},  # Too high
        ]
        
        for test_case in test_cases:
            response = client.put(
                '/api/criteria',
                data=json.dumps(test_case),
                content_type='application/json'
            )
            
            assert response.status_code == 400
            data = json.loads(response.data)
            assert data['success'] is False


class TestUniversalPropertyAI:
    @patch('services.property_ai_service.PropertyAIService.analyze_property_structured')
    def test_property_ai_openai_does_not_overwrite_claude_field(self, mock_analyze, client, app):
        with app.app_context():
            prop = Property(source_email_id='prop_ai_openai_1', title='Test Property', municipality='Alicante')
            prop.ai_analysis = {"provider": "claude", "note": "keep"}
            db.session.add(prop)
            db.session.commit()

            mock_analyze.return_value = {
                "status": "success",
                "structured_analysis": {"provider": "openai", "ok": True},
                "model": "gpt-test",
            }

            resp = client.post(f'/api/property/{prop.id}/analyze/structured', json={'provider': 'openai'})
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True
            assert data['provider'] == 'openai'
            assert mock_analyze.called is True
            _, kwargs = mock_analyze.call_args
            assert kwargs.get('provider') == 'openai'

            updated = db.session.get(Property, prop.id)
            assert updated.ai_analysis == {"provider": "claude", "note": "keep"}

            variant = (
                PropertyAiAnalysisVariant.query.filter_by(property_id=prop.id, provider='openai')
                .order_by(PropertyAiAnalysisVariant.created_at.desc())
                .first()
            )
            assert variant is not None
            assert variant.analysis.get('provider') == 'openai'
            assert variant.model == 'gpt-test'

    @patch('services.property_ai_service.PropertyAIService.analyze_property_structured')
    def test_property_ai_claude_updates_property_ai_analysis(self, mock_analyze, client, app):
        with app.app_context():
            prop = Property(source_email_id='prop_ai_claude_1', title='Test Property', municipality='Alicante')
            db.session.add(prop)
            db.session.commit()

            mock_analyze.return_value = {
                "status": "success",
                "structured_analysis": {"provider": "claude", "ok": True},
                "model": "claude-test",
            }

            resp = client.post(f'/api/property/{prop.id}/analyze/structured', json={'provider': 'claude'})
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True
            assert data['provider'] == 'claude'
            assert mock_analyze.called is True
            _, kwargs = mock_analyze.call_args
            assert kwargs.get('provider') == 'claude'

            updated = db.session.get(Property, prop.id)
            assert updated.ai_analysis.get('provider') == 'claude'

            variant = (
                PropertyAiAnalysisVariant.query.filter_by(property_id=prop.id, provider='claude')
                .order_by(PropertyAiAnalysisVariant.created_at.desc())
                .first()
            )
            assert variant is not None
            assert variant.analysis.get('provider') == 'claude'
            assert variant.model == 'claude-test'

    def test_property_ai_compare_endpoint(self, client, app):
        with app.app_context():
            prop = Property(source_email_id='prop_ai_compare_1', title='Test Property', municipality='Alicante')
            prop.ai_analysis = {"rental_market_analysis": {"investment_rating": "GOOD - Above average returns"}}
            db.session.add(prop)
            db.session.commit()

            db.session.add(
                PropertyAiAnalysisVariant(
                    property_id=prop.id,
                    provider='openai',
                    model='gpt-test',
                    analysis={"rental_market_analysis": {"investment_rating": "MODERATE - Standard market returns"}},
                )
            )
            db.session.commit()

            resp = client.get(f'/api/property/{prop.id}/analysis/compare')
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True
            assert data['property_id'] == prop.id
            assert data['has_chatgpt'] is True
            assert data['chatgpt_model'] == 'gpt-test'
            assert 'comparison' in data
            assert 'claude' in data['comparison']
            assert 'expected' in data['comparison']

    @patch('services.property_enrichment_service.PropertyEnrichmentService.enrich_property', return_value=True)
    def test_property_google_enrich_endpoint(self, mock_enrich, client, app):
        with app.app_context():
            prop = Property(source_email_id='prop_enrich_1', title='Test Property', municipality='Alicante')
            db.session.add(prop)
            db.session.commit()

            resp = client.post(f'/api/property/{prop.id}/enrich', json={})
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True
            assert 'enriched successfully' in data['message'].lower()
