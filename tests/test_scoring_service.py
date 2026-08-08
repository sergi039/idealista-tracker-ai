"""
Tests for scoring service functionality.
"""

import pytest
from decimal import Decimal
from app import create_app, db
from config import Config
from models import Land, ScoringCriteria
from services.scoring_service import ScoringService
from tests import setup_test_environment


@pytest.fixture
def app():
    """Create test Flask application"""
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def scoring_service():
    """Create ScoringService instance"""
    return ScoringService()


@pytest.fixture
def test_land(app):
    """Create test land with enriched data"""
    with app.app_context():
        land = Land(
            source_email_id="scoring_test_1",
            title="Test Land for Scoring",
            municipality="Valencia",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            location_lat=Decimal("39.4699"),
            location_lon=Decimal("-0.3763"),
            description="Parcela urbana con vistas al mar y orientación sur",
            legal_status="Developed",
            infrastructure_basic={
                "electricity": True,
                "water": True,
                "internet": False,
                "gas": True,
            },
            infrastructure_extended={
                "supermarket_available": True,
                "supermarket_distance": 800,
                "school_available": True,
                "school_distance": 1200,
                "hospital_available": True,
                "hospital_distance": 2500,
                "restaurant_available": True,
                "restaurant_distance": 500,
            },
            transport={
                "train_station_available": True,
                "train_station_distance": 3000,
                "bus_station_available": True,
                "bus_station_distance": 600,
                "airport_available": True,
                "airport_distance": 45000,
            },
            environment={
                "sea_view": True,
                "mountain_view": False,
                "forest_view": False,
                "orientation": "south",
            },
            services_quality={
                "school_avg_rating": 4.2,
                "restaurant_avg_rating": 4.5,
                "cafe_avg_rating": 4.0,
            },
        )
        db.session.add(land)
        db.session.commit()
        return land.id


@pytest.fixture
def scoring_criteria(app):
    """Create test scoring criteria.

    Every name here is a real criterion, i.e. a key of
    Config.DEFAULT_SCORING_WEIGHTS, and the weights sum to 1.0 so that
    load_custom_weights' MCDM normalization is an identity. `location_quality`
    used to be `neighborhood`, which is a land data field with a scorer but has
    never been a weighted criterion: load_custom_weights now drops rows whose
    name is not a criterion (#47), so it no longer counted toward the total.
    """
    with app.app_context():
        criteria = [
            ScoringCriteria(
                criteria_name="infrastructure_basic", weight=Decimal("0.20")
            ),
            ScoringCriteria(
                criteria_name="infrastructure_extended", weight=Decimal("0.15")
            ),
            ScoringCriteria(criteria_name="transport", weight=Decimal("0.20")),
            ScoringCriteria(criteria_name="environment", weight=Decimal("0.15")),
            ScoringCriteria(criteria_name="location_quality", weight=Decimal("0.15")),
            ScoringCriteria(criteria_name="services_quality", weight=Decimal("0.10")),
            ScoringCriteria(criteria_name="legal_status", weight=Decimal("0.05")),
        ]
        db.session.add_all(criteria)
        db.session.commit()
        return criteria


class TestScoringService:
    """Test cases for ScoringService"""

    def test_init(self, scoring_service):
        """Test ScoringService initialization"""
        assert hasattr(scoring_service, "weights")
        assert len(scoring_service.weights) > 0
        assert "infrastructure_basic" in scoring_service.weights
        assert "transport" in scoring_service.weights

    def test_load_custom_weights(self, app, scoring_service, scoring_criteria):
        """Test loading custom weights from database"""
        with app.app_context():
            scoring_service.load_custom_weights()

            assert scoring_service.weights["infrastructure_basic"] == 0.20
            assert scoring_service.weights["transport"] == 0.20
            assert scoring_service.weights["environment"] == 0.15

    def test_calculate_score_success(
        self, app, scoring_service, test_land, scoring_criteria
    ):
        """Test successful score calculation"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service.calculate_score(land)

            assert score > 0
            assert score <= 100
            assert land.score_total is not None
            assert land.score_total == score

    def test_score_infrastructure_basic(self, app, scoring_service, test_land):
        """Test basic infrastructure scoring"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service._score_infrastructure_basic(land)

            # Should score 75% (3 out of 4 utilities)
            assert score == 75.0

    def test_score_infrastructure_basic_from_description(self, app, scoring_service):
        """Test basic infrastructure scoring from description"""
        with app.app_context():
            land = Land(
                source_email_id="infra_desc_test",
                description="Terreno con electricidad, agua y conexión de gas",
                infrastructure_basic={},
            )

            score = scoring_service._score_infrastructure_basic(land)

            # Should detect 3 utilities from description
            assert score == 75.0

    def test_score_infrastructure_basic_none(self, app, scoring_service):
        """Test basic infrastructure scoring with no data"""
        with app.app_context():
            land = Land(source_email_id="infra_none_test", infrastructure_basic=None)

            score = scoring_service._score_infrastructure_basic(land)

            assert score is None

    def test_score_infrastructure_extended(self, app, scoring_service, test_land):
        """Test extended infrastructure scoring"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service._score_infrastructure_extended(land)

            # All amenities available within good distances
            assert score > 80
            assert score <= 100

    def test_score_transport(self, app, scoring_service, test_land):
        """Test transport scoring"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service._score_transport(land)

            # Good transport access
            assert score > 70
            assert score <= 100

    def test_score_environment_sea_view(self, app, scoring_service, test_land):
        """Test environment scoring with sea view"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service._score_environment(land)

            # Sea view (40) + South orientation (20) = 60
            assert score == 60.0

    def test_score_environment_multiple_views(self, app, scoring_service):
        """Test environment scoring with multiple views"""
        with app.app_context():
            land = Land(
                source_email_id="multi_view_test",
                environment={
                    "sea_view": True,
                    "mountain_view": True,
                    "forest_view": True,
                    "orientation": "southeast",
                },
            )

            score = scoring_service._score_environment(land)

            # Sea(40) + Mountain(30) + Forest(20) + SE(15) = 105, capped at 100
            assert score == 100.0

    def test_score_neighborhood_default(self, app, scoring_service, test_land):
        """Test neighborhood scoring with default neutral score"""
        with app.app_context():
            land = db.session.get(Land, test_land)
            land.neighborhood = None

            score = scoring_service._score_neighborhood(land)

            assert score == 50  # Default neutral score

    def test_score_neighborhood_with_data(self, app, scoring_service):
        """Test neighborhood scoring with data"""
        with app.app_context():
            land = Land(
                source_email_id="neighborhood_test",
                neighborhood={
                    "area_price_level": "high",
                    "new_houses": True,
                    "noise": "low",
                },
            )

            score = scoring_service._score_neighborhood(land)

            # Base(50) + High price(20) + New houses(15) + Low noise(15) = 100
            assert score == 100.0

    def test_score_services_quality(self, app, scoring_service, test_land):
        """Test services quality scoring"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service._score_services_quality(land)

            # Average of 4.2, 4.5, 4.0 = 4.23, converted to percentage = 84.6%
            expected_score = ((4.2 + 4.5 + 4.0) / 3) / 5 * 100
            assert abs(score - expected_score) < 0.1

    def test_score_services_quality_none(self, app, scoring_service):
        """Test services quality scoring with no data"""
        with app.app_context():
            land = Land(source_email_id="services_none_test", services_quality=None)

            score = scoring_service._score_services_quality(land)

            assert score is None

    def test_score_legal_status_developed(self, app, scoring_service, test_land):
        """Test legal status scoring for developed land"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            score = scoring_service._score_legal_status(land)

            assert score == 100.0

    def test_score_legal_status_buildable(self, app, scoring_service):
        """Test legal status scoring for buildable land"""
        with app.app_context():
            land = Land(
                source_email_id="buildable_test",
                land_type="buildable",
                legal_status="Buildable",
            )

            score = scoring_service._score_legal_status(land)

            assert score == 80.0

    def test_score_legal_status_invalid(self, app, scoring_service):
        """Test legal status scoring for invalid status"""
        with app.app_context():
            land = Land(source_email_id="invalid_legal_test", legal_status="Rustic")

            score = scoring_service._score_legal_status(land)

            assert score == 0.0

    def test_update_weights_success(self, app, scoring_service, test_land):
        """Test successful weights update"""
        with app.app_context():
            new_weights = {
                "infrastructure_basic": 0.25,
                "transport": 0.30,
                "environment": 0.20,
            }

            result = scoring_service.update_weights(new_weights)

            assert result is True
            assert scoring_service.weights["infrastructure_basic"] == 0.25
            assert scoring_service.weights["transport"] == 0.30

            # Verify criteria were created in database
            criteria = ScoringCriteria.query.filter_by(
                criteria_name="infrastructure_basic"
            ).first()
            assert criteria is not None
            assert criteria.weight == Decimal("0.25")

    def test_update_weights_with_existing_criteria(
        self, app, scoring_service, scoring_criteria
    ):
        """A legacy shared update writes both profiles used by scoring."""
        with app.app_context():
            new_weights = {
                "infrastructure_basic": 0.30  # Update existing
            }

            result = scoring_service.update_weights(new_weights)

            assert result is True

            for profile in ("investment", "lifestyle"):
                criteria = ScoringCriteria.query.filter_by(
                    criteria_name="infrastructure_basic", profile=profile
                ).first()
                assert criteria.weight == Decimal("0.30")

    def test_get_current_weights(self, app, scoring_service, scoring_criteria):
        """Test getting current weights"""
        with app.app_context():
            weights = scoring_service.get_current_weights()

            assert isinstance(weights, dict)
            assert "infrastructure_basic" in weights
            assert weights["infrastructure_basic"] == 0.20
            assert len(weights) >= 7  # At least the default criteria

    def test_calculate_score_missing_data(self, app, scoring_service):
        """Test score calculation with missing data"""
        with app.app_context():
            # Create land with minimal data
            land = Land(
                source_email_id="minimal_data_test",
                title="Minimal Land",
                land_type="developed",
            )
            db.session.add(land)
            db.session.commit()

            score = scoring_service.calculate_score(land)

            # Should still calculate a score, even if low
            assert score >= 0
            assert score <= 100
            assert land.score_total is not None

    def test_calculate_score_all_none(self, app, scoring_service):
        """Test score calculation when all individual scores are None"""
        with app.app_context():
            land = Land(source_email_id="all_none_test", title="Empty Land")
            db.session.add(land)
            db.session.commit()

            score = scoring_service.calculate_score(land)

            # Should return 0 when no valid scores
            assert score == 0
            assert land.score_total == 0

    def test_weighted_score_calculation(self, app, scoring_service, scoring_criteria):
        """Test that weighted calculation works correctly"""
        with app.app_context():
            # Create land with predictable scores
            land = Land(
                source_email_id="weighted_test",
                land_type="developed",  # Will score 100 for legal_status
                infrastructure_basic={
                    "electricity": True,
                    "water": True,
                    "internet": True,
                    "gas": True,
                },  # Will score 100
                services_quality={"school_avg_rating": 5.0},  # Will score 100
            )
            db.session.add(land)
            db.session.commit()

            score = scoring_service.calculate_score(land)

            # With weights: legal_status(0.05) + infrastructure_basic(0.20) + services_quality(0.10)
            # = 100*0.05 + 100*0.20 + 100*0.10 = 35 points out of 35 total weight
            # = 100% of weighted total
            assert score > 90  # Should be high since all measured criteria scored 100

    def test_score_storage_in_environment(
        self, app, scoring_service, test_land, scoring_criteria
    ):
        """Test that score breakdown is stored in environment field"""
        with app.app_context():
            land = db.session.get(Land, test_land)

            scoring_service.calculate_score(land)

            assert land.environment is not None
            assert "score_breakdown" in land.environment

            breakdown = land.environment["score_breakdown"]
            assert isinstance(breakdown, dict)
            assert "infrastructure_basic" in breakdown
            assert "legal_status" in breakdown

    def test_update_weights_persists_rescored_environment(
        self, app, scoring_service, test_land
    ):
        """A rescore must replace the persisted JSON breakdown (#26)."""
        with app.app_context():
            land = db.session.get(Land, test_land)
            land.environment = {
                **land.environment,
                "scoring": {
                    "profiles": {"lifestyle": {"weights_used": {"environment": 0.22}}},
                    "combined_score": -1,
                },
                "score_breakdown": {"environment": -1},
            }
            db.session.commit()
            db.session.expire_all()

            baseline = db.session.get(Land, test_land)
            baseline_weights = baseline.environment["scoring"]["profiles"]["lifestyle"][
                "weights_used"
            ]

            assert scoring_service.update_weights(
                {"environment": 0.1, "legal_status": 0.9},
                profile="lifestyle",
            )

            db.session.expire_all()
            rescored = db.session.get(Land, test_land)
            persisted_scoring = rescored.environment["scoring"]
            persisted_weights = persisted_scoring["profiles"]["lifestyle"][
                "weights_used"
            ]

            assert persisted_weights == pytest.approx(
                {"environment": 0.1, "legal_status": 0.9}
            )
            assert persisted_weights != baseline_weights
            assert persisted_scoring["combined_score"] == pytest.approx(
                float(rescored.score_total)
            )
            assert rescored.environment["sea_view"] is True


class TestConfigWeightsNotMutated:
    """Regression tests for #44.

    ScoringService used to bind Config.DEFAULT_SCORING_WEIGHTS by reference, so
    every in-place update leaked into the class-level config dict for the rest
    of the process: GET /api/criteria then served the polluted dict and
    _validate_profiles' "unknown criterion" check silently widened.
    """

    @staticmethod
    def _combined_profile_rows():
        """Rows a real deployment ends up with.

        One genuine combined-profile criterion weight, plus the two
        investment/lifestyle rows written by POST /criteria/update_combined_mix.
        Those two are the ratio between the profile scores, owned by
        _load_combined_mix, not scoring criteria - but load_custom_weights'
        `profile == 'combined' OR profile IS NULL` filter matches them too.
        """
        return [
            ScoringCriteria(
                criteria_name="transport", profile="combined", weight=Decimal("0.50")
            ),
            ScoringCriteria(
                criteria_name="investment", profile="combined", weight=Decimal("0.32")
            ),
            ScoringCriteria(
                criteria_name="lifestyle", profile="combined", weight=Decimal("0.68")
            ),
        ]

    def test_init_does_not_mutate_config_defaults(self, app):
        """Constructing the service must leave Config.DEFAULT_SCORING_WEIGHTS alone"""
        with app.app_context():
            db.session.add_all(self._combined_profile_rows())
            db.session.commit()

            before = dict(Config.DEFAULT_SCORING_WEIGHTS)

            service = ScoringService()

            assert Config.DEFAULT_SCORING_WEIGHTS == before

            # The instance owns its dict and still picked the DB weights up, so
            # this is a live copy rather than a frozen view of the defaults.
            assert service.weights is not Config.DEFAULT_SCORING_WEIGHTS
            assert (
                service.weights["transport"]
                != Config.DEFAULT_SCORING_WEIGHTS["transport"]
            )

    def test_update_weights_does_not_mutate_config_defaults(self, app):
        """update_weights() must leave Config.DEFAULT_SCORING_WEIGHTS alone too"""
        with app.app_context():
            service = ScoringService()

            before = dict(Config.DEFAULT_SCORING_WEIGHTS)

            assert service.update_weights({"transport": 0.30}, profile="combined")

            assert Config.DEFAULT_SCORING_WEIGHTS == before
            assert service.weights["transport"] == 0.30
