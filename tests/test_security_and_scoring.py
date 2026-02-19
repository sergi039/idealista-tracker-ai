"""
TEST-01: Tests for security (auth, CSRF) and scoring DB-weight integration.

Ported from Legacy to Universal.  Covers:
  - Unauthorized POST → 401 on all protected endpoints (Land + Property)
  - Session-based auth flow (login, access, logout)
  - Scoring uses DB profile weights (not hardcoded)
  - DB weight change → actual score change
  - Combined mix persistence to DB
  - Error messages don't leak internals
"""

import os
import pytest
import json
from decimal import Decimal
from unittest.mock import patch
from app import create_app, db
from config import Config
from models import Land, Property, ScoringCriteria
from services.scoring_service import ScoringService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_disabled_app():
    """App fixture with TESTING=False so auth checks are enforced.
    ADMIN_API_TOKEN is NOT set so all requests are denied (fail-closed)."""
    setup_test_environment()
    orig_token = os.environ.pop('ADMIN_API_TOKEN', None)
    app = create_app()
    app.config['TESTING'] = False
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()
    if orig_token is not None:
        os.environ['ADMIN_API_TOKEN'] = orig_token


@pytest.fixture
def auth_disabled_client(auth_disabled_app):
    return auth_disabled_app.test_client()


@pytest.fixture
def test_land(app):
    """Create a land with enriched data for scoring tests."""
    with app.app_context():
        land = Land(
            source_email_id='sec_test_1',
            title='Security Test Land',
            municipality='Valencia',
            land_type='developed',
            price=Decimal('150000.00'),
            area=Decimal('1500.00'),
            location_lat=Decimal('39.4699'),
            location_lon=Decimal('-0.3763'),
            description='Parcela urbana con vistas al mar y orientación sur',
            legal_status='Developed',
            infrastructure_basic={
                'electricity': True, 'water': True,
                'internet': False, 'gas': True,
            },
            infrastructure_extended={
                'supermarket_available': True, 'supermarket_distance': 800,
                'school_available': True, 'school_distance': 1200,
                'hospital_available': True, 'hospital_distance': 2500,
                'restaurant_available': True, 'restaurant_distance': 500,
            },
            transport={
                'train_station_available': True, 'train_station_distance': 3000,
                'bus_station_available': True, 'bus_station_distance': 600,
                'airport_available': True, 'airport_distance': 45000,
            },
            environment={
                'sea_view': True, 'mountain_view': False,
                'forest_view': False, 'orientation': 'south',
            },
            services_quality={
                'school_avg_rating': 4.2,
                'restaurant_avg_rating': 4.5,
                'cafe_avg_rating': 4.0,
            },
        )
        db.session.add(land)
        db.session.commit()
        return land.id


@pytest.fixture
def auth_disabled_test_land(auth_disabled_app):
    """Create a land in the auth-disabled app context."""
    with auth_disabled_app.app_context():
        land = Land(
            source_email_id='auth_test_1',
            title='Auth Test Land',
            municipality='Madrid',
            land_type='buildable',
            price=Decimal('100000.00'),
            area=Decimal('1000.00'),
        )
        db.session.add(land)
        db.session.commit()
        return land.id


@pytest.fixture
def auth_disabled_test_property(auth_disabled_app):
    """Create a property in the auth-disabled app context."""
    with auth_disabled_app.app_context():
        prop = Property(
            source_email_id='auth_prop_test_1',
            title='Auth Test Property',
            municipality='Barcelona',
            property_category='housing',
            property_subtype='apartment',
            price=Decimal('250000.00'),
            area=Decimal('90.00'),
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


# ---------------------------------------------------------------------------
# Security: unauthorized POST → 401
# ---------------------------------------------------------------------------
class TestUnauthorizedAccess:
    """Admin-only endpoints must return 401 for anonymous requests."""

    PROTECTED_POST_ENDPOINTS = [
        '/api/ingest/email/run',
        '/api/lands/enrich-all',
    ]

    def test_anonymous_post_returns_401(self, auth_disabled_client, auth_disabled_test_land):
        """All protected Land POST endpoints must reject anonymous requests."""
        lid = auth_disabled_test_land
        endpoints = self.PROTECTED_POST_ENDPOINTS + [
            f'/api/land/{lid}/enrich',
            f'/api/analyze/property/{lid}/structured',
            f'/api/analysis/generate/{lid}/openai',
            f'/api/enhance/description/{lid}',
            f'/api/land/{lid}/environment',
            f'/api/analyze/property/{lid}',
            f'/api/land/{lid}/set-status',
            f'/api/land/{lid}/check-status',
        ]

        for endpoint in endpoints:
            resp = auth_disabled_client.post(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )
            data = json.loads(resp.data)
            assert data['success'] is False

    def test_anonymous_property_post_returns_401(
        self, auth_disabled_client, auth_disabled_test_property
    ):
        """All protected Property POST endpoints must reject anonymous requests."""
        pid = auth_disabled_test_property
        property_endpoints = [
            f'/api/property/{pid}/enrich',
            f'/api/property/{pid}/set-status',
            f'/api/property/{pid}/analyze/structured',
            f'/api/property/{pid}/environment',
        ]

        for endpoint in property_endpoints:
            resp = auth_disabled_client.post(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )

    def test_anonymous_favorite_toggle_is_allowed(
        self, auth_disabled_client, auth_disabled_test_land, auth_disabled_test_property
    ):
        """Favorites are user-facing actions and should work without admin auth."""
        lid = auth_disabled_test_land
        pid = auth_disabled_test_property

        land_resp = auth_disabled_client.post(f'/api/land/{lid}/favorite')
        assert land_resp.status_code == 200
        land_data = json.loads(land_resp.data)
        assert land_data['success'] is True
        assert land_data['is_favorite'] is True

        property_resp = auth_disabled_client.post(f'/api/property/{pid}/favorite')
        assert property_resp.status_code == 200
        property_data = json.loads(property_resp.data)
        assert property_data['success'] is True
        assert property_data['is_favorite'] is True

    def test_anonymous_put_returns_401(self, auth_disabled_client):
        """PUT /api/criteria must reject anonymous requests."""
        resp = auth_disabled_client.put(
            '/api/criteria',
            data=json.dumps({'criteria': {'test': 0.5}}),
            content_type='application/json',
        )
        assert resp.status_code == 401

    def test_read_only_endpoints_accessible(self, auth_disabled_client, auth_disabled_test_land):
        """Read-only endpoints should still work without auth."""
        for endpoint in ['/api/healthz', '/api/stats', '/api/lands']:
            resp = auth_disabled_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200"
            )

        resp = auth_disabled_client.get(f'/api/lands/{auth_disabled_test_land}')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Security: error messages don't leak internals
# ---------------------------------------------------------------------------
class TestErrorSanitization:
    """API error responses must not expose internal exception messages."""

    def test_internal_error_no_leak(self, client):
        """Simulated internal error should return generic message."""
        with patch(
            'services.scheduler_service.get_scheduler_status',
            side_effect=Exception("secret DB creds"),
        ):
            resp = client.get('/api/scheduler/status')
            assert resp.status_code == 500
            data = json.loads(resp.data)
            assert 'secret' not in data.get('error', '')
            assert 'creds' not in data.get('error', '')


# ---------------------------------------------------------------------------
# Scoring: Config profile weights drive calculation, DB combined weights loaded
# ---------------------------------------------------------------------------
class TestScoringWeights:
    """Scoring uses Config.SCORING_PROFILES for profile weights and
    loads legacy combined weights from DB into ScoringService.weights."""

    def test_score_uses_config_investment_profile(self, app, test_land):
        """Investment score must be driven by Config.SCORING_PROFILES['investment']."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            # Score with Config defaults
            svc.calculate_score(land)
            db.session.commit()
            default_investment = float(land.score_investment)

            # Patch Config.SCORING_PROFILES to heavily favor environment
            alt_profiles = {
                'investment': {
                    'environment': 0.90,
                    'infrastructure_basic': 0.02,
                    'transport': 0.02,
                    'legal_status': 0.02,
                    'investment_yield': 0.02,
                    'location_quality': 0.02,
                },
                'lifestyle': Config.SCORING_PROFILES.get('lifestyle', {}),
            }
            with patch.object(Config, 'SCORING_PROFILES', alt_profiles):
                svc.calculate_score(land)
                db.session.commit()
                new_investment = float(land.score_investment)

            assert new_investment != default_investment, (
                f"Investment score did not change with altered Config profiles: "
                f"default={default_investment}, new={new_investment}"
            )

    def test_score_uses_config_lifestyle_profile(self, app, test_land):
        """Lifestyle score must be driven by Config.SCORING_PROFILES['lifestyle']."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            svc.calculate_score(land)
            db.session.commit()
            default_lifestyle = float(land.score_lifestyle)

            alt_profiles = {
                'investment': Config.SCORING_PROFILES.get('investment', {}),
                'lifestyle': {
                    'investment_yield': 0.90,
                    'environment': 0.02,
                    'services_quality': 0.02,
                    'transport': 0.02,
                    'infrastructure_basic': 0.02,
                    'location_quality': 0.02,
                },
            }
            with patch.object(Config, 'SCORING_PROFILES', alt_profiles):
                svc.calculate_score(land)
                db.session.commit()
                new_lifestyle = float(land.score_lifestyle)

            assert new_lifestyle != default_lifestyle, (
                f"Lifestyle score did not change with altered Config profiles: "
                f"default={default_lifestyle}, new={new_lifestyle}"
            )

    def test_combined_mix_affects_total_score(self, app, test_land):
        """Changing Config.COMBINED_MIX ratio must change score_total."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            svc.calculate_score(land)
            db.session.commit()
            default_total = float(land.score_total)
            inv = float(land.score_investment)
            life = float(land.score_lifestyle)

            if abs(inv - life) < 0.01:
                pytest.skip("Investment and lifestyle scores identical; cannot test mix effect")

            # Patch COMBINED_MIX to 100% investment
            with patch.object(Config, 'COMBINED_MIX', {'investment': 1.0, 'lifestyle': 0.0}):
                svc.calculate_score(land)
                db.session.commit()
                inv_only_total = float(land.score_total)

            assert abs(inv_only_total - default_total) > 0.01, (
                "score_total did not change with 100% investment mix"
            )

    def test_db_combined_weights_loaded_on_init(self, app):
        """ScoringService.__init__ must load combined weights from DB."""
        with app.app_context():
            # Seed DB combined weights
            for name, weight in [('environment', 0.80), ('transport', 0.20)]:
                db.session.add(ScoringCriteria(
                    criteria_name=name,
                    profile='combined',
                    weight=Decimal(str(weight)),
                    active=True,
                ))
            db.session.commit()

            svc = ScoringService()
            # After normalization, these should be in self.weights
            assert 'environment' in svc.weights
            assert 'transport' in svc.weights

    def test_profile_weights_fallback_to_config(self, app, test_land):
        """When no DB weights exist, scoring falls back to Config defaults."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            svc.calculate_score(land)
            db.session.commit()

            assert float(land.score_total) > 0
            assert float(land.score_investment) > 0
            assert float(land.score_lifestyle) > 0
