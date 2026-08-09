"""Regression tests for issue #25: ingestion data integrity in the universal pipeline.

Three defects, one per section below:

a) Price updates and removals were looked up with `search_profile_id` pinned to
   whatever profile *this* email resolved to. Profiles are auto-created from the
   saved-search name, and price-change / "no longer listed" templates do not
   carry that name the way a listing email does, so the lookup missed and the
   update silently no-oped.
b) Any email from Idealista carrying any idealista.com link was ingested, so
   recommendation/digest mails created rows with `idealista_property_id=None` -
   undedupable, re-inserted on every full sync because of per-email UTM
   parameters.
c) A price-change email for an untracked listing created a brand-new row, i.e. a
   thin duplicate of a listing already tracked under another profile.

The (b) test drives the real fetch/parse/ingest path with only `IMAPClient` (the
network boundary) faked, and asserts on rows that actually landed in the
database. The (a) and (c) tests feed `run_ingestion()` the exact dict shape
`get_idealista_emails()` produces and assert on committed rows; nothing on the
query/commit path is mocked.
"""

from datetime import datetime, timezone
from decimal import Decimal
from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment

INTERNAL_DATE = datetime(2026, 2, 3, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def uid_file(tmp_path, monkeypatch):
    """Keep the UID cursor inside the test's tmp dir, never the repo/data one."""
    path = tmp_path / ".last_seen_uid_properties"
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PROPERTIES_PATH", str(path))
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PATH", str(tmp_path / ".last_seen_uid"))
    monkeypatch.setattr(Config, "BASE_DIR", str(tmp_path / "base"))
    return path


def _profiles():
    """Two profiles: the email resolves to `other`, the row lives under `owner`."""
    owner = SearchProfile(
        name="Homes in Ciudad Quesada",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    other = SearchProfile(
        name="Autocreated from a price-change template",
        is_active=True,
        is_default=False,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add_all([owner, other])
    db.session.commit()
    return owner, other


def _seed_property(profile_id: int, listing_id: int, url: str) -> Property:
    prop = Property(
        source_email_id=f"imap_seed_{listing_id}",
        idealista_property_id=listing_id,
        search_profile_id=profile_id,
        url=url,
        title="Flat in Ciudad Quesada",
        deal_type="sale",
        price=Decimal("300000.00"),
        area=Decimal("90.00"),
        area_type="built",
        listing_status="active",
    )
    db.session.add(prop)
    db.session.commit()
    return prop


class TestProfileMismatchNoLongerSilentlyDropsUpdates:
    """(a) A mismatched profile must not swallow a price change or a delisting."""

    def test_price_change_updates_the_tracked_row_under_another_profile(
        self, app, monkeypatch
    ):
        with app.app_context():
            owner, other = _profiles()
            url = "https://www.idealista.com/inmueble/770001/"
            seeded = _seed_property(owner.id, 770001, url)
            seeded_id = seeded.id

            emails = [
                {
                    "type": "price_change",
                    "source_email_id": "imap_price_770001",
                    "email_received_at": INTERNAL_DATE,
                    "url": url,
                    "idealista_property_id": 770001,
                    # Resolved from the price-change template, not the template
                    # the listing itself arrived on.
                    "search_profile_id": other.id,
                    "title": "Flat in Ciudad Quesada",
                    "price": 285000.0,
                    "previous_price_hint": 300000.0,
                    "area": 90,
                }
            ]

            service = PropertyIMAPService()
            monkeypatch.setattr(
                service, "get_idealista_emails", lambda max_results=None: list(emails)
            )
            service.run_ingestion(sync_type="test")

            # No thin duplicate, and the real row carries the new price.
            assert Property.query.count() == 1
            updated = db.session.get(Property, seeded_id)
            assert float(updated.price) == 285000.0
            assert float(updated.previous_price) == 300000.0

    def test_no_longer_listed_removes_the_tracked_row_under_another_profile(
        self, app, monkeypatch
    ):
        with app.app_context():
            owner, other = _profiles()
            url = "https://www.idealista.com/inmueble/770002/"
            seeded = _seed_property(owner.id, 770002, url)
            seeded_id = seeded.id

            emails = [
                {
                    "type": "no_longer_listed",
                    "source_email_id": "imap_gone_770002",
                    "email_received_at": INTERNAL_DATE,
                    "url": url,
                    "idealista_property_id": 770002,
                    "search_profile_id": other.id,
                    "deal_type": "sale",
                }
            ]

            service = PropertyIMAPService()
            monkeypatch.setattr(
                service, "get_idealista_emails", lambda max_results=None: list(emails)
            )
            service.run_ingestion(sync_type="test")

            removed = db.session.get(Property, seeded_id)
            assert removed.listing_status == "removed"
            assert removed.listing_removed_date is not None

    def test_matching_profile_still_updates_the_row(self, app, monkeypatch):
        """The fallback must not be the only path that works."""
        with app.app_context():
            owner, _ = _profiles()
            url = "https://www.idealista.com/inmueble/770003/"
            seeded = _seed_property(owner.id, 770003, url)
            seeded_id = seeded.id

            emails = [
                {
                    "type": "price_change",
                    "source_email_id": "imap_price_770003",
                    "email_received_at": INTERNAL_DATE,
                    "url": url,
                    "idealista_property_id": 770003,
                    "search_profile_id": owner.id,
                    "title": "Flat in Ciudad Quesada",
                    "price": 275000.0,
                    "previous_price_hint": 300000.0,
                    "area": 90,
                }
            ]

            service = PropertyIMAPService()
            monkeypatch.setattr(
                service, "get_idealista_emails", lambda max_results=None: list(emails)
            )
            service.run_ingestion(sync_type="test")

            assert Property.query.count() == 1
            assert float(db.session.get(Property, seeded_id).price) == 275000.0


class TestPriceChangeNeverCreatesRows:
    """(c) Price-change alerts describe a change, not a property."""

    def test_price_change_for_an_untracked_listing_creates_nothing(
        self, app, monkeypatch
    ):
        with app.app_context():
            owner, _ = _profiles()

            emails = [
                {
                    "type": "price_change",
                    "source_email_id": "imap_price_880001",
                    "email_received_at": INTERNAL_DATE,
                    "url": "https://www.idealista.com/inmueble/880001/",
                    "idealista_property_id": 880001,
                    "search_profile_id": owner.id,
                    "title": "Some listing we never ingested",
                    "price": 199000.0,
                    "previous_price_hint": 210000.0,
                    "area": 70,
                }
            ]

            service = PropertyIMAPService()
            monkeypatch.setattr(
                service, "get_idealista_emails", lambda max_results=None: list(emails)
            )
            created = service.run_ingestion(sync_type="test")

            assert created == 0
            assert Property.query.count() == 0


class _FakeIMAPClient:
    """Stands in for the IMAP server only; everything below it is real code."""

    payloads: dict[int, bytes] = {}

    def __init__(self, host, port=None, ssl=None, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        return True

    def select_folder(self, name, readonly=True):
        return None

    def search(self, args):
        return sorted(_FakeIMAPClient.payloads)

    def fetch(self, uids, parts):
        return {
            uid: {
                b"RFC822": _FakeIMAPClient.payloads[uid],
                b"INTERNALDATE": INTERNAL_DATE,
            }
            for uid in uids
        }


def _raw_email(subject: str, url: str, body_html: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Idealista <noresponder@idealista.com>"
    msg.set_content("See it in your browser")
    msg.add_alternative(
        f'<html><body><a href="{url}">{subject}</a>{body_html}</body></html>',
        subtype="html",
    )
    return msg.as_bytes()


class TestJunkEmailsWithoutAListingUrl:
    """(b) Only a URL naming one listing may create a row."""

    def test_recommendation_emails_never_create_undedupable_rows(
        self, app, uid_file, monkeypatch
    ):
        # Two sends of the same recommendation mail. Nothing but the UTM tail
        # differs, so exact-URL dedup cannot see they are the same page - that
        # is exactly how these rows used to pile up on every full sync.
        _FakeIMAPClient.payloads = {
            1: _raw_email(
                "Homes we think you will like",
                "https://www.idealista.com/venta-viviendas/alicante/"
                "?utm_source=newsletter&utm_campaign=reco_2026_02_03",
                "<p>Casa en Alicante</p><p>250.000 €</p><p>120 m²</p>",
            ),
            2: _raw_email(
                "Homes we think you will like",
                "https://www.idealista.com/venta-viviendas/alicante/"
                "?utm_source=newsletter&utm_campaign=reco_2026_02_10",
                "<p>Casa en Alicante</p><p>250.000 €</p><p>120 m²</p>",
            ),
        }

        monkeypatch.setattr(
            "services.property_imap_service.IMAPClient", _FakeIMAPClient, raising=True
        )

        with app.app_context():
            service = PropertyIMAPService()
            service.user = "owner@example.com"
            service.password = "dummy"
            service.host = "imap.example.com"
            service.folder = "Idealista"
            created = service.run_ingestion(sync_type="test")

            assert created == 0
            assert Property.query.count() == 0
            # Deliberate skips, so the cursor is free to move past them.
            assert service.last_seen_uid == 2

    def test_real_listing_emails_are_still_ingested(self, app, uid_file, monkeypatch):
        """The gate must reject junk without also rejecting the real thing."""
        _FakeIMAPClient.payloads = {
            1: _raw_email(
                "New home in your search: Homes in Ciudad Quesada",
                "https://www.idealista.com/inmueble/990001/",
                "<p>Casa en Ciudad Quesada</p><p>250.000 €</p><p>120 m²</p>",
            )
        }

        monkeypatch.setattr(
            "services.property_imap_service.IMAPClient", _FakeIMAPClient, raising=True
        )

        with app.app_context():
            service = PropertyIMAPService()
            service.user = "owner@example.com"
            service.password = "dummy"
            service.host = "imap.example.com"
            service.folder = "Idealista"
            service.run_ingestion(sync_type="test")

            stored = Property.query.one()
            assert stored.idealista_property_id == 990001
