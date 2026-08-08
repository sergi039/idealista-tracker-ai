from unittest.mock import patch

from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment


class _FakeIMAPClient:
    last_instance = None

    def __init__(self, host, port=None, ssl=None, timeout=None):
        self.host = host
        self.port = port
        self.ssl = ssl
        self.logged_in = False
        self.selected_folder = None
        self.search_calls = []
        _FakeIMAPClient.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        self.logged_in = True
        return True

    def select_folder(self, name, readonly=True):
        self.selected_folder = (name, bool(readonly))

    def search(self, args):
        self.search_calls.append(args)
        return []

    def fetch(self, uids, parts):
        return {}


def test_property_imap_service_uses_gmail_label_from_imap_folder(monkeypatch):
    setup_test_environment()
    with patch("services.property_imap_service.IMAPClient", _FakeIMAPClient):
        service = PropertyIMAPService()
        # Config reads env at import time; patch instance fields directly for this unit test.
        service.user = "user@example.com"
        service.password = "dummy"
        service.host = "imap.gmail.com"
        service.folder = "IdealistaProperties"
        service.get_idealista_emails(max_results=1)

    client = _FakeIMAPClient.last_instance
    assert client is not None
    assert client.logged_in is True
    assert client.search_calls, "Expected a Gmail X-GM-RAW search call"

    # Ensure label filter is applied (prevents mixing with legacy).
    raw_query = None
    for call in client.search_calls:
        if call and len(call) == 2 and call[0] == "X-GM-RAW":
            raw_query = call[1]
            break

    assert raw_query is not None
    assert "from:noresponder@idealista.com" in raw_query
    assert "label:IdealistaProperties" in raw_query
