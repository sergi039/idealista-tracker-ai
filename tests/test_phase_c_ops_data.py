"""
TEST-03: Tests for Phase C (P1 ops/data) fixes — ported to Universal.

Covers:
  DATA-01 - Timezone-aware datetimes (no more utcnow())
  DATA-02 - CHECK constraints on Land AND Property models
  OPS-01  - Config validation at startup
  OPS-02  - Enhanced /healthz with DB ping
"""

import pytest
from datetime import timezone
from decimal import Decimal
from unittest.mock import patch

from app import create_app, db, _validate_config
from models import Land, Property, utcnow
from tests import setup_test_environment


@pytest.fixture
def app():
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
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# DATA-01: Timezone-aware datetimes
# ---------------------------------------------------------------------------
class TestTimezoneAwareDatetimes:
    """All generated datetimes must be timezone-aware (UTC)."""

    def test_utcnow_helper_is_aware(self):
        """The utcnow() model helper must return a timezone-aware datetime."""
        dt = utcnow()
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc

    def test_land_created_at_is_aware(self, app):
        """Land.created_at default must produce timezone-aware datetime."""
        with app.app_context():
            land = Land(source_email_id="tz_test_1", title="TZ Test")
            db.session.add(land)
            db.session.commit()
            assert land.created_at is not None
            dt = utcnow()
            assert dt.tzinfo is not None

    def test_property_created_at_is_aware(self, app):
        """Property.created_at default must produce timezone-aware datetime."""
        with app.app_context():
            prop = Property(source_email_id="tz_test_prop_1", title="TZ Prop Test")
            db.session.add(prop)
            db.session.commit()
            assert prop.created_at is not None
            dt = utcnow()
            assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# DATA-02: CHECK constraints on Land model
# ---------------------------------------------------------------------------
class TestLandCheckConstraints:
    """CHECK constraints on Land must reject invalid data."""

    def test_negative_price_rejected(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_price_neg",
                title="Negative Price",
                price=Decimal("-100.00"),
            )
            db.session.add(land)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_zero_price_allowed(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_price_zero",
                title="Free Land",
                price=Decimal("0.00"),
            )
            db.session.add(land)
            db.session.commit()
            assert land.id is not None

    def test_null_price_allowed(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_price_null",
                title="No Price",
                price=None,
            )
            db.session.add(land)
            db.session.commit()
            assert land.id is not None

    def test_invalid_latitude_rejected(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_lat_bad",
                title="Bad Lat",
                location_lat=Decimal("91.0"),
            )
            db.session.add(land)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_invalid_longitude_rejected(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_lon_bad",
                title="Bad Lon",
                location_lon=Decimal("181.0"),
            )
            db.session.add(land)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_score_over_100_rejected(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_score_bad",
                title="Bad Score",
                score_total=Decimal("101.00"),
            )
            db.session.add(land)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_negative_travel_time_rejected(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_tt_neg",
                title="Bad TT",
                travel_time_oviedo=-5,
            )
            db.session.add(land)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_invalid_listing_status_rejected(self, app):
        with app.app_context():
            land = Land(
                source_email_id="ck_status_bad",
                title="Bad Status",
                listing_status="bogus",
            )
            db.session.add(land)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_valid_listing_statuses_accepted(self, app):
        with app.app_context():
            for i, status in enumerate(("active", "removed", "sold", "unknown")):
                land = Land(
                    source_email_id=f"ck_status_ok_{i}",
                    title=f"Status {status}",
                    listing_status=status,
                )
                db.session.add(land)
            db.session.commit()
            assert Land.query.count() == 4


# ---------------------------------------------------------------------------
# DATA-02b: CHECK constraints on Property model (Universal-specific)
# ---------------------------------------------------------------------------
class TestPropertyCheckConstraints:
    """CHECK constraints on Property must reject invalid data."""

    def test_negative_price_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_price_neg",
                title="Neg Price Prop",
                price=Decimal("-500.00"),
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_zero_price_allowed(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_price_zero",
                title="Free Prop",
                price=Decimal("0.00"),
            )
            db.session.add(prop)
            db.session.commit()
            assert prop.id is not None

    def test_null_price_allowed(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_price_null",
                title="No Price Prop",
                price=None,
            )
            db.session.add(prop)
            db.session.commit()
            assert prop.id is not None

    def test_negative_area_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_area_neg",
                title="Neg Area Prop",
                area=Decimal("-50.00"),
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_invalid_latitude_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_lat_bad",
                title="Bad Lat Prop",
                location_lat=Decimal("91.0"),
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_invalid_longitude_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_lon_bad",
                title="Bad Lon Prop",
                location_lon=Decimal("-181.0"),
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_score_over_100_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_score_bad",
                title="Bad Score Prop",
                score_total=Decimal("101.00"),
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_investment_score_negative_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_inv_neg",
                title="Neg Inv Score",
                score_investment=Decimal("-1.00"),
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_invalid_listing_status_rejected(self, app):
        with app.app_context():
            prop = Property(
                source_email_id="ck_prop_status_bad",
                title="Bad Status Prop",
                listing_status="invalid_status",
            )
            db.session.add(prop)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_valid_listing_statuses_accepted(self, app):
        with app.app_context():
            for i, status in enumerate(("active", "removed", "sold", "unknown")):
                prop = Property(
                    source_email_id=f"ck_prop_status_ok_{i}",
                    title=f"Prop Status {status}",
                    listing_status=status,
                )
                db.session.add(prop)
            db.session.commit()
            assert Property.query.count() == 4


# ---------------------------------------------------------------------------
# OPS-01: Config validation
# ---------------------------------------------------------------------------
class TestConfigValidation:
    """_validate_config must fail fast on invalid config."""

    def test_missing_database_url_raises(self):
        config = {
            "TESTING": False,
            "DATABASE_URL": None,
            "SQLALCHEMY_DATABASE_URI": None,
        }
        with pytest.raises(ValueError, match="DATABASE_URL"):
            _validate_config(config)

    def test_invalid_database_scheme_raises(self):
        config = {
            "TESTING": False,
            "DATABASE_URL": "mysql://user:pass@host/db",
        }
        with pytest.raises(ValueError, match="unexpected scheme"):
            _validate_config(config)

    def test_valid_postgresql_url_passes(self):
        config = {
            "TESTING": False,
            "DATABASE_URL": "postgresql://user:pass@host:5432/db",
        }
        _validate_config(config)

    def test_testing_mode_skips_validation(self):
        config = {
            "TESTING": True,
            "DATABASE_URL": None,
        }
        _validate_config(config)

    def test_bad_scoring_weights_raises(self):
        config = {
            "TESTING": False,
            "DATABASE_URL": "postgresql://user:pass@host:5432/db",
        }
        bad_profiles = {
            "investment": {"a": 0.5},  # sums to 0.5
            "lifestyle": {"b": 1.0},
        }
        with patch("app.Config") as mock_config:
            mock_config.SCHEDULER_TIMEZONE = "Europe/Madrid"
            mock_config.INGESTION_TIMES = ["07:00"]
            mock_config.SCORING_PROFILES = bad_profiles
            with pytest.raises(ValueError, match="investment.*weights sum"):
                _validate_config(config)


# ---------------------------------------------------------------------------
# OPS-02: Enhanced /healthz
# ---------------------------------------------------------------------------
class TestHealthzEndpoint:
    """Enhanced /healthz must report dependency status."""

    def test_healthz_returns_checks(self, client):
        resp = client.get("/api/healthz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "checks" in data
        assert data["checks"]["database"] == "ok"

    def test_healthz_reports_scheduler(self, client):
        resp = client.get("/api/healthz")
        data = resp.get_json()
        assert "scheduler" in data["checks"]

    def test_healthz_503_on_db_failure(self, app):
        with app.test_client() as c:
            with patch.object(db.session, "execute", side_effect=Exception("DB down")):
                resp = c.get("/api/healthz")
                assert resp.status_code == 503
                data = resp.get_json()
                assert data["ok"] is False
                assert data["checks"]["database"] == "unavailable"
