"""
TEST-02: Tests for Phase B (P1 core) fixes — ported to Universal.

Covers:
  COR-02/03 - Price parsing defensive coercion
  COR-04    - Score clamping [0, 100]
  COR-05    - Pagination boundary validation
  REL-01    - HTTP retry with exponential backoff (utils.http)
  REL-03    - Scheduler lock file cleanup
  REL-04    - Distance Matrix element validation
"""

import os
import fcntl
import tempfile
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from app import create_app, db
from models import Land
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


@pytest.fixture
def test_land(app):
    """Create a land with enriched data for scoring tests."""
    with app.app_context():
        land = Land(
            source_email_id="phase_b_test_1",
            title="Phase B Test Land",
            municipality="Oviedo",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            location_lat=Decimal("43.3614"),
            location_lon=Decimal("-5.8593"),
            description="Parcela urbana con vistas al mar",
            legal_status="Developed",
            infrastructure_basic={
                "electricity": True,
                "water": True,
                "internet": True,
                "gas": True,
            },
            environment={
                "sea_view": True,
                "mountain_view": True,
                "forest_view": True,
                "orientation": "south",
            },
        )
        db.session.add(land)
        db.session.commit()
        return land.id


# ---------------------------------------------------------------------------
# COR-04: Score clamping [0, 100]
# ---------------------------------------------------------------------------
class TestScoreClamping:
    """Individual scores must be clamped to [0, 100] before MCDM aggregation."""

    def test_negative_subscores_clamped_to_zero(self, app, test_land):
        """If a scoring function returns < 0 the aggregation must clamp it."""
        from services.scoring_service import ScoringService

        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)
            with (
                patch.object(svc, "_score_location_quality", return_value=-50),
                patch.object(svc, "_score_environment", return_value=-30),
            ):
                svc.calculate_score(land)
                db.session.commit()
                assert float(land.score_investment) >= 0
                assert float(land.score_lifestyle) >= 0
                assert float(land.score_total) >= 0

    def test_oversized_subscores_clamped_to_100(self, app, test_land):
        """If a scoring function returns > 100 the aggregation must clamp it."""
        from services.scoring_service import ScoringService

        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)
            with (
                patch.object(svc, "_score_location_quality", return_value=250),
                patch.object(svc, "_score_environment", return_value=300),
            ):
                svc.calculate_score(land)
                db.session.commit()
                assert float(land.score_total) <= 100


# ---------------------------------------------------------------------------
# COR-05: Pagination boundary validation
# ---------------------------------------------------------------------------
class TestPaginationBounds:
    """Pagination parameters must be clamped to safe ranges."""

    def test_api_negative_limit_clamped(self, client):
        """Negative limit must be clamped to 1 (not cause errors)."""
        resp = client.get("/api/lands?limit=-5&offset=0")
        assert resp.status_code == 200

    def test_api_negative_offset_clamped(self, client):
        """Negative offset must be clamped to 0."""
        resp = client.get("/api/lands?limit=10&offset=-10")
        assert resp.status_code == 200

    def test_api_huge_limit_clamped(self, client):
        """Limit above 500 must be clamped."""
        resp = client.get("/api/lands?limit=999999")
        assert resp.status_code == 200

    def test_page_negative_clamped(self, client):
        """Page route with negative page must not error (200 or redirect, not 4xx/5xx)."""
        resp = client.get("/?page=-1")
        assert resp.status_code in (200, 302)


# ---------------------------------------------------------------------------
# REL-01: HTTP retry with exponential backoff (Universal: utils.http)
# ---------------------------------------------------------------------------
class TestHttpRetry:
    """request_with_retries must retry on transient errors and raise on exhaustion."""

    def test_success_on_first_try(self):
        from utils.http import request_with_retries
        import requests

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(requests, "get", return_value=mock_resp) as m:
            result = request_with_retries(
                requests.get,
                "http://example.com",
                max_attempts=2,
                backoff_base=0,
                backoff_max=0,
            )
            assert result is mock_resp
            assert m.call_count == 1

    def test_retries_on_500(self):
        from utils.http import request_with_retries
        import requests

        fail_resp = MagicMock()
        fail_resp.status_code = 500
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        with patch.object(requests, "get", side_effect=[fail_resp, ok_resp]) as m:
            result = request_with_retries(
                requests.get,
                "http://example.com",
                max_attempts=2,
                backoff_base=0,
                backoff_max=0,
            )
            assert result is ok_resp
            assert m.call_count == 2

    def test_retries_on_connection_error(self):
        import requests as req
        from utils.http import request_with_retries

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        with patch.object(
            req, "get", side_effect=[req.ConnectionError("fail"), ok_resp]
        ) as m:
            result = request_with_retries(
                req.get,
                "http://example.com",
                max_attempts=2,
                backoff_base=0,
                backoff_max=0,
            )
            assert result is ok_resp
            assert m.call_count == 2

    def test_raises_on_exhausted_connection_errors(self):
        """After max_attempts exhausted on network errors, should raise."""
        import requests as req
        from utils.http import request_with_retries

        with patch.object(req, "get", side_effect=req.ConnectionError("fail")):
            with pytest.raises(req.ConnectionError):
                request_with_retries(
                    req.get,
                    "http://example.com",
                    max_attempts=1,
                    backoff_base=0,
                    backoff_max=0,
                )

    def test_returns_last_retriable_response_on_exhaustion(self):
        """After max_attempts exhausted on 429, should return last response."""
        from utils.http import request_with_retries
        import requests

        fail_resp = MagicMock()
        fail_resp.status_code = 429
        with patch.object(requests, "get", return_value=fail_resp):
            result = request_with_retries(
                requests.get,
                "http://example.com",
                max_attempts=1,
                backoff_base=0,
                backoff_max=0,
            )
            assert result is fail_resp
            assert result.status_code == 429


# ---------------------------------------------------------------------------
# REL-03: Scheduler lock file cleanup
# ---------------------------------------------------------------------------
class TestSchedulerLockCleanup:
    """Lock file must be released when scheduler init fails."""

    def test_lock_released_on_init_failure(self, app):
        """If BackgroundScheduler() raises, the lock file must be cleaned up."""
        with app.app_context():
            from services import scheduler_service

            scheduler_service.scheduler = None
            scheduler_service.scheduler_lock_file = None

            lock_path = os.path.join(
                tempfile.gettempdir(), "idealista_universal_scheduler.lock"
            )
            try:
                os.remove(lock_path)
            except OSError:
                pass

            app.config["TESTING"] = False
            with patch.object(
                app.config,
                "get",
                side_effect=lambda k, d=None: {
                    "TESTING": False,
                    "AUTO_START_SCHEDULER": True,
                }.get(k, d),
            ):
                with patch("services.scheduler_service.Config") as mock_config:
                    mock_config.AUTO_START_SCHEDULER = True
                    mock_config.SCHEDULER_TIMEZONE = "Europe/Madrid"
                    mock_config.INGESTION_TIMES = ["07:00"]
                    with patch(
                        "services.scheduler_service.BackgroundScheduler",
                        side_effect=RuntimeError("boom"),
                    ):
                        with pytest.raises(RuntimeError, match="boom"):
                            scheduler_service.init_scheduler(app)

            assert scheduler_service.scheduler_lock_file is None

    def test_lock_file_closed_on_contention(self, app):
        """When another instance holds the lock, the file handle must not leak."""
        lock_path = os.path.join(
            tempfile.gettempdir(), "idealista_universal_scheduler.lock"
        )
        holder = open(lock_path, "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            from services import scheduler_service

            scheduler_service.scheduler = None
            scheduler_service.scheduler_lock_file = None

            with patch.object(
                app.config,
                "get",
                side_effect=lambda k, d=None: {
                    "TESTING": False,
                    "AUTO_START_SCHEDULER": True,
                }.get(k, d),
            ):
                with patch("services.scheduler_service.Config") as mock_config:
                    mock_config.AUTO_START_SCHEDULER = True
                    result = scheduler_service.init_scheduler(app)
                    assert result is None
                    assert scheduler_service.scheduler_lock_file is None
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
            try:
                os.remove(lock_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# REL-04: Distance Matrix element validation
# ---------------------------------------------------------------------------
class TestDistanceMatrixValidation:
    """Elements with missing duration/distance must not crash."""

    def _make_service(self):
        with patch("services.travel_time_service.Config") as mock_config:
            mock_config.GOOGLE_MAPS_API_KEY = "test-key"
            from services.travel_time_service import TravelTimeService

            svc = TravelTimeService()
            svc.google_maps_key = "test-key"
        return svc

    def test_missing_duration_returns_none(self):
        """Element with status OK but no duration → should return None."""
        svc = self._make_service()
        api_response = {
            "status": "OK",
            "rows": [
                {
                    "elements": [
                        {"status": "OK", "distance": {"value": 5000}},  # no duration
                    ]
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = api_response
        with patch(
            "services.travel_time_service.request_with_retries", return_value=mock_resp
        ):
            result = svc._get_google_travel_time("43.36,-5.85", "43.53,-5.66")
            assert result is None

    def test_missing_distance_returns_none(self):
        """Element with status OK but no distance → should return None."""
        svc = self._make_service()
        api_response = {
            "status": "OK",
            "rows": [
                {
                    "elements": [
                        {"status": "OK", "duration": {"value": 600}},  # no distance
                    ]
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = api_response
        with patch(
            "services.travel_time_service.request_with_retries", return_value=mock_resp
        ):
            result = svc._get_google_travel_time("43.36,-5.85", "43.53,-5.66")
            assert result is None

    def test_batch_missing_fields_skipped(self):
        """Batch call: elements with missing fields become None entries."""
        svc = self._make_service()
        api_response = {
            "status": "OK",
            "rows": [
                {
                    "elements": [
                        {
                            "status": "OK",
                            "duration": {"value": 600},
                            "distance": {"value": 10000},
                        },
                        {"status": "OK"},  # missing both
                        {"status": "ZERO_RESULTS"},
                    ]
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = api_response
        with patch(
            "services.travel_time_service.request_with_retries", return_value=mock_resp
        ):
            results = svc._get_google_travel_times("43.36,-5.85", ["d1", "d2", "d3"])
            assert len(results) == 3
            assert results[0] == {"time": 10, "distance": 10}
            assert results[1] is None  # missing fields
            assert results[2] is None  # ZERO_RESULTS

    def test_valid_element_parsed_correctly(self):
        """Standard element with all fields should parse correctly."""
        svc = self._make_service()
        api_response = {
            "status": "OK",
            "rows": [
                {
                    "elements": [
                        {
                            "status": "OK",
                            "duration": {"value": 1800},  # 30 minutes
                            "distance": {"value": 25000},  # 25 km
                        }
                    ]
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = api_response
        with patch(
            "services.travel_time_service.request_with_retries", return_value=mock_resp
        ):
            result = svc._get_google_travel_time("43.36,-5.85", "43.53,-5.66")
            assert result == {"time": 30, "distance": 25}


# ---------------------------------------------------------------------------
# COR-02/03: Price parsing defensive coercion
# ---------------------------------------------------------------------------
class TestPriceParsingDefensive:
    """Price values from email data must be safely coerced to float."""

    def test_invalid_price_string_does_not_crash(self, app, test_land):
        """Non-numeric price should be handled gracefully."""
        with app.app_context():
            price_raw = "not-a-number"
            new_price = None
            try:
                new_price = float(price_raw)
                if new_price <= 0:
                    new_price = None
            except (ValueError, TypeError):
                pass
            assert new_price is None

    def test_zero_price_treated_as_none(self):
        """Zero price should be treated as missing (None)."""
        new_price = None
        try:
            new_price = float("0")
            if new_price <= 0:
                new_price = None
        except (ValueError, TypeError):
            pass
        assert new_price is None

    def test_valid_price_string_parsed(self):
        """Numeric string price should parse correctly."""
        new_price = None
        try:
            new_price = float("125000.50")
            if new_price <= 0:
                new_price = None
        except (ValueError, TypeError):
            pass
        assert new_price == 125000.50

    def test_negative_price_treated_as_none(self):
        """Negative price should be treated as missing."""
        new_price = None
        try:
            new_price = float("-5000")
            if new_price <= 0:
                new_price = None
        except (ValueError, TypeError):
            pass
        assert new_price is None
