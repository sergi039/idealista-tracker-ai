"""Issue #15: every IMAP connection must carry an explicit socket timeout.

A hung socket used to block the ingestion run forever, silently stalling all
future ingestions. These tests pin the contract: both pipelines pass the
configured timeout to IMAPClient, and a timed-out connection surfaces as a
loud, non-fatal failure of that run instead of a hang.
"""

from unittest.mock import patch

from config import Config
from tests import setup_test_environment


class _CaptureTimeoutClient:
    """Records constructor kwargs, then fails the connection immediately."""

    last_kwargs = None

    def __init__(self, host, **kwargs):
        _CaptureTimeoutClient.last_kwargs = dict(kwargs)

    def __enter__(self):
        raise TimeoutError("simulated hung socket")

    def __exit__(self, exc_type, exc, tb):
        return False


def test_config_default_timeout_is_positive():
    assert Config.IMAP_TIMEOUT_SECONDS > 0


def test_legacy_imap_connects_with_timeout_and_fails_loudly():
    setup_test_environment()
    from services.imap_service import IMAPService

    _CaptureTimeoutClient.last_kwargs = None
    with patch("services.imap_service.IMAPClient", _CaptureTimeoutClient):
        service = IMAPService()
        service.user = "user@example.com"
        service.password = "dummy"
        assert service.authenticate() is False

    assert _CaptureTimeoutClient.last_kwargs is not None
    assert _CaptureTimeoutClient.last_kwargs["timeout"] == Config.IMAP_TIMEOUT_SECONDS


def test_property_imap_connects_with_timeout_and_fails_loudly():
    setup_test_environment()
    from services.property_imap_service import PropertyIMAPService

    _CaptureTimeoutClient.last_kwargs = None
    with patch("services.property_imap_service.IMAPClient", _CaptureTimeoutClient):
        service = PropertyIMAPService()
        service.user = "user@example.com"
        service.password = "dummy"
        assert service.get_idealista_emails(max_results=1) == []

    assert _CaptureTimeoutClient.last_kwargs is not None
    assert _CaptureTimeoutClient.last_kwargs["timeout"] == Config.IMAP_TIMEOUT_SECONDS
