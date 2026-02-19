from unittest.mock import patch

from services.imap_service import IMAPService
from tests import setup_test_environment


class _FakeIMAPClient:
    last_instance = None

    def __init__(self, host, port=None, ssl=None):
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


def test_gmail_prefers_direct_folder_selection():
    """When a folder is set and selectable, Gmail path selects it directly."""
    setup_test_environment()
    with patch("services.imap_service.IMAPClient", _FakeIMAPClient):
        service = IMAPService()
        service.user = "user@example.com"
        service.password = "dummy"
        service.host = "imap.gmail.com"
        service.folder = "IdealistaLands"
        service.get_idealista_emails(max_results=1)

    client = _FakeIMAPClient.last_instance
    assert client is not None
    assert client.logged_in is True
    # Direct folder selection should be used
    assert client.selected_folder == ("IdealistaLands", True)
    # Search should use ALL (not X-GM-RAW)
    assert client.search_calls
    assert client.search_calls[0] == ["ALL"]


class _FakeIMAPClientFolderFail:
    """IMAP client where direct folder selection fails, forcing X-GM-RAW fallback."""
    last_instance = None

    def __init__(self, host, port=None, ssl=None):
        self.host = host
        self.selected_folder = None
        self.search_calls = []
        _FakeIMAPClientFolderFail.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        return True

    def select_folder(self, name, readonly=True):
        if name not in ("[Gmail]/All Mail", "INBOX"):
            raise Exception("folder not found")
        self.selected_folder = (name, bool(readonly))

    def search(self, args):
        self.search_calls.append(args)
        return []

    def fetch(self, uids, parts):
        return {}


def test_gmail_falls_back_to_xgmraw_when_folder_unavailable():
    """When direct folder selection fails, fall back to All Mail + X-GM-RAW."""
    setup_test_environment()
    with patch("services.imap_service.IMAPClient", _FakeIMAPClientFolderFail):
        service = IMAPService()
        service.user = "user@example.com"
        service.password = "dummy"
        service.host = "imap.gmail.com"
        service.folder = "IdealistaLands"
        service.get_idealista_emails(max_results=1)

    client = _FakeIMAPClientFolderFail.last_instance
    assert client is not None
    assert client.selected_folder == ("[Gmail]/All Mail", True)
    assert client.search_calls
    raw_query = None
    for call in client.search_calls:
        if call and len(call) == 2 and call[0] == "X-GM-RAW":
            raw_query = call[1]
            break
    assert raw_query is not None
    assert "from:noresponder@idealista.com" in raw_query
    assert "label:IdealistaLands" in raw_query
