"""
Tests for market analysis service functionality (2025 updates).
"""

import pytest
from unittest.mock import Mock, patch
from decimal import Decimal
from app import create_app, db
from models import Land
from services.market_analysis_service import MarketAnalysisService
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
def market_service():
    """Create MarketAnalysisService instance"""
    return MarketAnalysisService()


@pytest.fixture
def test_land(app):
    """Create test land for market analysis"""
    with app.app_context():
        land = Land(
            source_email_id='market_test_1',
            title='Test Land in Gijón',
            municipality='Gijón',
            land_type='developed',
            price=Decimal('65000.00'),
            area=Decimal('800.00'),
            location_lat=Decimal('43.5322'),
            location_lon=Decimal('-5.6611'),
            infrastructure_basic={
                'electricity': True,
                'water': True,
                'internet': True,
                'gas': False
            },
            travel_time_nearest_beach=15,
            travel_time_oviedo=30,
            travel_time_airport=45
        )
        db.session.add(land)
        db.session.commit()
        yield land.id
        db.session.rollback()


class TestMarketAnalysisService:
    """Test suite for MarketAnalysisService"""

    def test_construction_costs_2025_values(self, market_service):
        """Test that construction costs are updated to 2025 values"""
        # Basic tier should be €1,100-1,500/m²
        basic = market_service.CONSTRUCTION_COSTS['basic']
        assert basic['min'] == 1100
        assert basic['avg'] == 1300
        assert basic['max'] == 1500

        # Premium tier should be €1,500-2,200/m²
        premium = market_service.CONSTRUCTION_COSTS['premium']
        assert premium['min'] == 1500
        assert premium['avg'] == 1800
        assert premium['max'] == 2200

    def test_purchase_costs_ratio(self, market_service):
        """Test that purchase costs ratio is 11%"""
        assert market_service.PURCHASE_COSTS_RATIO == 0.11

    def test_rental_adjustments_exist(self, market_service):
        """Test that rental adjustments are defined for all location types"""
        assert 'urban' in market_service.RENTAL_ADJUSTMENTS
        assert 'suburban' in market_service.RENTAL_ADJUSTMENTS
        assert 'rural' in market_service.RENTAL_ADJUSTMENTS

        # Check urban has lower vacancy than rural
        assert market_service.RENTAL_ADJUSTMENTS['urban']['vacancy_rate'] < \
               market_service.RENTAL_ADJUSTMENTS['rural']['vacancy_rate']

    def test_calculate_construction_value_includes_purchase_costs(self, app, market_service, test_land):
        """Test that construction value calculation includes purchase costs"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            result = market_service.calculate_construction_value(land)

            assert 'purchase_costs' in result
            assert 'land_price_with_costs' in result

            # Purchase costs should be 11% of land price
            expected_purchase_costs = float(land.price) * 0.11
            assert abs(result['purchase_costs'] - expected_purchase_costs) < 1

            # Land price with costs should be land + purchase costs
            expected_total = float(land.price) + expected_purchase_costs
            assert abs(result['land_price_with_costs'] - expected_total) < 1

    def test_calculate_construction_value_2025_costs(self, app, market_service, test_land):
        """Test that construction uses 2025 cost values"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            result = market_service.calculate_construction_value(land)

            # value_per_m2 should be in 2025 range (1100-2200)
            assert result['value_per_m2'] >= 1100
            assert result['value_per_m2'] <= 2200

    def test_rental_analysis_returns_net_yield(self, app, market_service, test_land):
        """Test that rental analysis returns both gross and net yields"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            # Should have both gross and net yields
            assert 'gross_rental_yield' in result
            assert 'net_rental_yield' in result
            assert 'rental_yield' in result

            # Net yield should be lower than gross
            assert result['net_rental_yield'] < result['gross_rental_yield']

            # Primary rental_yield should be net
            assert result['rental_yield'] == result['net_rental_yield']

    def test_rental_analysis_includes_assumptions(self, app, market_service, test_land):
        """Test that rental analysis includes transparency assumptions"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            assert 'assumptions' in result
            assumptions = result['assumptions']

            assert 'vacancy_rate' in assumptions
            assert 'operating_expenses' in assumptions
            assert 'management_fee' in assumptions
            assert 'total_deductions' in assumptions

    def test_urban_location_detection(self, app, market_service, test_land):
        """Test that Gijón is detected as urban location"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            assert result['location_type'] == 'Urban'

    def test_rural_location_has_higher_vacancy(self, app, market_service):
        """Test that rural locations have higher vacancy assumptions"""
        with app.app_context():
            # Create rural land
            land = Land(
                source_email_id='market_test_rural',
                title='Rural Land Test',
                municipality='Cangas de Onís',  # Not a major city
                land_type='buildable',
                price=Decimal('30000.00'),
                area=Decimal('2000.00')
            )
            db.session.add(land)
            db.session.commit()

            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            # Should be Rural type
            assert result['location_type'] == 'Rural'

            # Rural vacancy should be 20%
            assert result['assumptions']['vacancy_rate'] == '20%'

    def test_total_investment_calculation(self, app, market_service, test_land):
        """Test total investment = land + purchase costs + construction"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            result = market_service.calculate_construction_value(land)

            land_price = float(land.price)
            purchase_costs = land_price * 0.11
            construction_avg = result['average_value']

            expected_total = land_price + purchase_costs + construction_avg
            assert abs(result['total_investment_avg'] - expected_total) < 1

    def test_investment_rating_based_on_net_yield(self, app, market_service, test_land):
        """Test that investment rating uses net yield, not gross"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            # Rating should be based on net yield
            net_yield = result['net_rental_yield']
            rating = result['investment_rating']

            # Verify rating logic matches net yield
            if net_yield >= 6:
                assert 'EXCELLENT' in rating
            elif net_yield >= 5:
                assert 'GOOD' in rating
            elif net_yield >= 4:
                assert 'MODERATE' in rating
            else:
                assert 'BELOW' in rating

    def test_enriched_data_contains_all_sections(self, app, market_service, test_land):
        """Test that get_enriched_data returns all three sections"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            result = market_service.get_enriched_data(land)

            assert 'construction_value_estimation' in result
            assert 'market_price_dynamics' in result
            assert 'rental_market_analysis' in result

    def test_noi_calculation(self, app, market_service, test_land):
        """Test Net Operating Income calculation"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            # NOI should be: annual_rent * (1 - vacancy) * (1 - expenses)
            annual_rent = result['annual_rent_avg']
            vacancy = 0.05  # Urban vacancy
            expenses = 0.15  # Urban operating expenses

            expected_effective_rent = annual_rent * (1 - vacancy)
            expected_noi = expected_effective_rent * (1 - expenses)

            assert abs(result['net_operating_income'] - expected_noi) < 10

    def test_payback_uses_noi(self, app, market_service, test_land):
        """Test payback period uses NOI, not gross rent"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            construction = market_service.calculate_construction_value(land)
            result = market_service.calculate_rental_analysis(land, construction)

            # Payback = total_investment / NOI
            noi = result['net_operating_income']
            total_investment = construction['total_investment_avg']

            if noi > 0:
                expected_payback = total_investment / noi
                assert abs(result['payback_period_years'] - expected_payback) < 0.5
