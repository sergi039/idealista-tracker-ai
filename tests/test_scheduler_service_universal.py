from unittest.mock import Mock, patch

import pytest

from tests import setup_test_environment


@pytest.fixture(autouse=True)
def _setup_env():
    setup_test_environment()


def test_run_scheduled_ingestion_uses_property_target_by_default(monkeypatch):
    from config import Config
    from services.scheduler_service import run_scheduled_ingestion

    monkeypatch.setattr(Config, "INGESTION_TARGET", "properties", raising=False)

    mock_instance = Mock()
    mock_instance.run_ingestion.return_value = 7

    with patch("services.property_imap_service.PropertyIMAPService", return_value=mock_instance) as mock_ctor:
        run_scheduled_ingestion()
        mock_ctor.assert_called_once()
        mock_instance.run_ingestion.assert_called_once()


def test_run_scheduled_ingestion_uses_legacy_lands_when_configured(monkeypatch):
    from config import Config
    from services.scheduler_service import run_scheduled_ingestion

    monkeypatch.setattr(Config, "INGESTION_TARGET", "lands", raising=False)

    mock_instance = Mock()
    mock_instance.run_ingestion.return_value = 3

    with patch("services.imap_service.IMAPService", return_value=mock_instance) as mock_ctor:
        run_scheduled_ingestion()
        mock_ctor.assert_called_once()
        mock_instance.run_ingestion.assert_called_once()


def test_run_listing_status_check_is_skipped_for_properties(monkeypatch):
    from config import Config
    from services.scheduler_service import run_listing_status_check

    monkeypatch.setattr(Config, "INGESTION_TARGET", "properties", raising=False)

    with patch("services.listing_status_service.ListingStatusService") as mock_service:
        run_listing_status_check()
        mock_service.assert_not_called()


def test_run_listing_status_check_runs_for_lands(monkeypatch):
    from config import Config
    from services.scheduler_service import run_listing_status_check

    monkeypatch.setattr(Config, "INGESTION_TARGET", "lands", raising=False)

    mock_instance = Mock()
    mock_instance.check_favorites_status.return_value = {"checked": 0, "removed": 0, "sold": 0}

    with patch("services.listing_status_service.ListingStatusService", return_value=mock_instance) as mock_ctor:
        run_listing_status_check()
        mock_ctor.assert_called_once()
        mock_instance.check_favorites_status.assert_called_once()

