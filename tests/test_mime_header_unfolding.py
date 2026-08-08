"""Folded MIME headers must be unfolded before anything parses them.

Idealista alert subjects are long enough that the mail server folds them
(RFC 5322 2.2.3): the header continues on the next line after a CRLF and one
whitespace character. Where the fold lands depends on the length of the
subject prefix -- "New detached house" folds in a different spot than "Price
reduction" -- so a saved-search name that survives the fold in one email is
cut in half in the next.

That is not hypothetical. One saved search named "houses at your custom
search area norte" produced four SearchProfile rows, because the saved-search
extractor stops at the CR:

    'New semi-detached house in your search: houses at your custom\r\n search area norte!'
        -> "houses at your custom"
    'New detached house in your search: houses at your custom search\r\n area norte!'
        -> "houses at your custom search"
    'Price reduction in your search: houses at your custom search area\r\n norte!'
        -> "houses at your custom search area"
    'New caseron in your search: houses at your custom search area norte!'
        -> "houses at your custom search area norte"   (short prefix, no fold)
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services.imap_service import IMAPService
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment

# The real subject, folded exactly the way the mail server folded it.
FOLDED_SUBJECT = (
    "New country house in your search: houses at your custom search area\r\n norte!"
)
UNFOLDED_SUBJECT = (
    "New country house in your search: houses at your custom search area norte!"
)
SEARCH_NAME = "houses at your custom search area norte"

LISTING_URL = "https://www.idealista.com/en/inmueble/112229931/"

RAW_EMAIL = (
    "From: idealista <noresponder@idealista.com>\r\n"
    f"Subject: {FOLDED_SUBJECT}\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: text/html; charset="utf-8"\r\n'
    "\r\n"
    "<html><body>"
    f'<a href="{LISTING_URL}">150,000 EUR</a>'
    "<p>200 m2</p>"
    "</body></html>\r\n"
).encode("utf-8")


class _FakeIMAPClient:
    """Serves one raw email, so the test exercises the real parsing path."""

    def __init__(self, host, port=None, ssl=None, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        return True

    def select_folder(self, name, readonly=True):
        return None

    def search(self, args):
        return [1]

    def fetch(self, uids, parts):
        return {1: {b"RFC822": RAW_EMAIL, b"INTERNALDATE": None}}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.mark.parametrize(
    "service_cls", [PropertyIMAPService, IMAPService], ids=["universal", "legacy"]
)
def test_decode_header_value_unfolds_a_folded_subject(service_cls):
    setup_test_environment()

    decoded = service_cls()._decode_header_value(FOLDED_SUBJECT)

    assert "\r" not in decoded and "\n" not in decoded
    assert decoded == UNFOLDED_SUBJECT


def test_folded_subject_does_not_fragment_the_saved_search_profile(app, monkeypatch):
    """The ingestion boundary: one folded email, one profile, full name."""
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False
        Config.AUTO_PROFILE_ASSIGNMENT = False

        with patch("services.property_imap_service.IMAPClient", _FakeIMAPClient):
            service = PropertyIMAPService()
            service.user = "user@example.com"
            service.password = "dummy"
            service.host = "imap.gmail.com"
            service.folder = "Idealista"
            service.last_seen_uid = 0
            service.run_ingestion(sync_type="test")

        prop = Property.query.filter_by(idealista_property_id=112229931).first()
        assert prop is not None, "the listing email should have been ingested"

        # The stored subject is what later classification reads back.
        assert "\r" not in (prop.email_subject or "")
        assert "\n" not in (prop.email_subject or "")

        profile = db.session.get(SearchProfile, prop.search_profile_id)
        assert profile is not None
        assert profile.name == SEARCH_NAME

        # No truncated sibling profile may exist alongside it.
        fragments = [
            p.name
            for p in SearchProfile.query.all()
            if p.name != SEARCH_NAME and SEARCH_NAME.startswith(p.name)
        ]
        assert fragments == [], (
            f"folded subject created truncated profiles: {fragments}"
        )
