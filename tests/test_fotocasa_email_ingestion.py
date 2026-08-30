"""Fotocasa alert emails reach the same table through the same one builder.

Until 2026-08-30 a fotocasa alert email was never ingested at all: the Gmail
X-GM-RAW query asked only for idealista's sender, `extract_url` matched only
idealista.com links, and an email yielding no URL was silently consumed --
the UID cursor stepped over it and the listing never came back. These tests
pin the whole new path: the query asks for fotocasa mail, the listing links
are recognized, the row is written with the exact dedup key and NULL
`listing_status_source` the paste-links import writes, and a page fotocasa
refuses holds the UID cursor instead of being consumed.

**The alert email in here is synthetic.** As of 2026-08-30 the 19 fotocasa
municipality alerts had just been created and no real alert had arrived in
the owner's mailbox, so the sender address and the template are modeled, not
measured: the listing links use the `/<id>/d` shape every one of the 56
stored fotocasa URLs has, and the sender uses the fotocasa.es domain the
Gmail query matches by default. When the first real alert arrives, check its
sender against `FOTOCASA_ALERT_SENDERS` and its link shape against
`listing_urls_in_text` -- a template this parser cannot read is consumed
with a warning in the log (`property_imap_service.py`), not held.

The listing page itself is not synthetic: the fetch stub answers with the
real 40 KB payload of listing 190280914 committed under `tests/data/`.
"""

import pathlib
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services import fotocasa_import, fotocasa_source
from services.fotocasa_source import (
    REFUSAL_BLOCKED,
    REFUSAL_NOT_A_LISTING,
    FotocasaListing,
    parse_listing,
)
from services.property_imap_service import (
    FOTOCASA_MAX_CONSECUTIVE_REFUSALS,
    PropertyIMAPService,
)
from tests import setup_test_environment

FIXTURE = pathlib.Path(__file__).parent / "data" / "fotocasa_listing_190280914.html"

# The href carries an entity-encoded query tail on purpose: that is how links
# arrive inside HTML bodies, and the extractor must unescape before parsing.
LISTING_URL = (
    "https://www.fotocasa.es/es/comprar/terreno/aviles/llaranes/190280914/d"
    "?opi=300&amp;utm_campaign=alert"
)
STORED_URL = "https://www.fotocasa.es/es/comprar/terreno/aviles/llaranes/190280914/d"

INTERNAL_DATE = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)


def _alert_email(
    subject: str,
    listing_urls,
    sender: str = "fotocasa <alertas@fotocasa.es>",
) -> bytes:
    """A fotocasa alert email, in the modeled shape the module docstring owns.

    Each listing is linked twice (photo and title), the way alert templates
    link them, plus a search link and the alert-management link -- neither of
    which may ever be fetched (robots.txt disallows /buscar/).
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.set_content("Tienes anuncios nuevos en tu alerta")
    links = "".join(
        f'<a href="{u}"><img src="cid:photo"></a> <a href="{u}">Ver anuncio</a>'
        for u in listing_urls
    )
    msg.add_alternative(
        "<html><body>"
        f"<p>Tenemos {len(listing_urls)} anuncios nuevos para ti</p>"
        f"{links}"
        '<a href="https://www.fotocasa.es/es/comprar/viviendas/ribadeo/l">'
        "Ver todos</a>"
        '<a href="https://www.fotocasa.es/es/mis-alertas/">Gestionar alertas</a>'
        "</body></html>",
        subtype="html",
    )
    return msg.as_bytes()


class _FakeIMAPClient:
    """Stands in for the IMAP server only; everything below it is real code."""

    payloads: dict[int, bytes] = {}
    last_instance = None

    def __init__(self, host, port=None, ssl=None, timeout=None):
        self.search_calls = []
        _FakeIMAPClient.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        return True

    def select_folder(self, name, readonly=True):
        return None

    def search(self, args):
        self.search_calls.append(args)
        return sorted(_FakeIMAPClient.payloads)

    def fetch(self, uids, parts):
        return {
            uid: {
                b"RFC822": _FakeIMAPClient.payloads[uid],
                b"INTERNALDATE": INTERNAL_DATE,
            }
            for uid in uids
        }


@pytest.fixture
def app(monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    # These tests are about the ingest boundary, not the enrichers behind it;
    # the free pass on a row that *has* coordinates (the portal pin) would
    # reach Overpass and trip tests/network_guard.py.
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", False)

    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Default",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        app.config["DEFAULT_PROFILE_ID"] = profile.id
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


@pytest.fixture
def imap(monkeypatch):
    _FakeIMAPClient.payloads = {}
    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient", _FakeIMAPClient, raising=True
    )
    return _FakeIMAPClient


def _service() -> PropertyIMAPService:
    service = PropertyIMAPService()
    service.user = "owner@example.com"
    service.password = "dummy"
    service.host = "imap.example.com"
    service.folder = "IdealistaProperties"
    return service


def _real_listing(url: str) -> FotocasaListing:
    return parse_listing(FIXTURE.read_text(encoding="utf-8"), url)


@pytest.fixture
def fetch_calls(monkeypatch):
    """Fetch stub answering with the real committed page; records every URL."""
    calls: list[str] = []

    def fake_fetch(url, session=None):
        calls.append(url)
        return _real_listing(url)

    monkeypatch.setattr(fotocasa_source, "fetch_listing", fake_fetch)
    return calls


class TestGmailQuery:
    def _raw_query(self, client) -> str:
        for call in client.search_calls:
            if call and len(call) == 2 and call[0] == "X-GM-RAW":
                return call[1]
        raise AssertionError("Expected a Gmail X-GM-RAW search call")

    def test_the_query_asks_for_fotocasa_mail_too(self, imap, monkeypatch):
        setup_test_environment()
        monkeypatch.setattr(Config, "FOTOCASA_ALERT_SENDERS", "fotocasa.es")
        service = _service()
        service.host = "imap.gmail.com"
        service.get_idealista_emails(max_results=1)

        raw = self._raw_query(_FakeIMAPClient.last_instance)
        # An explicit parenthesised OR: two adjacent from: terms mean AND to
        # Gmail and would match no mail at all.
        assert "(from:noresponder@idealista.com OR from:fotocasa.es)" in raw
        assert "label:IdealistaProperties" in raw

    def test_no_senders_means_the_old_idealista_only_query(self, imap, monkeypatch):
        setup_test_environment()
        monkeypatch.setattr(Config, "FOTOCASA_ALERT_SENDERS", "")
        service = _service()
        service.host = "imap.gmail.com"
        service.get_idealista_emails(max_results=1)

        raw = self._raw_query(_FakeIMAPClient.last_instance)
        assert "from:noresponder@idealista.com" in raw
        assert "fotocasa" not in raw
        assert " OR " not in raw


class TestAlertEmailCreatesTheRow:
    def test_the_row_is_the_one_the_import_would_write(
        self, app, uid_file, imap, fetch_calls
    ):
        imap.payloads = {
            1: _alert_email("Alerta: 1 anuncio nuevo en Avilés", [LISTING_URL])
        }

        with app.app_context():
            service = _service()
            created = service.run_ingestion(sync_type="test")

            assert created == 1
            # Linked twice in the email (photo + title), fetched once.
            assert fetch_calls == [
                STORED_URL + "?opi=300&utm_campaign=alert",
            ]

            prop = Property.query.one()
            # The dedup key both doors write -- fotocasa_import's own helper,
            # so this cannot drift from the paste-links importer.
            assert prop.source_email_id == fotocasa_import.source_email_id_for(
                190280914
            )
            assert prop.url == STORED_URL
            assert prop.municipality == "Avilés"
            assert float(prop.price) == 68000.0
            # Nobody has checked this listing is live; NULL is how the schema
            # says that (STATUS-002). The Python-side default is "ingest", so
            # None here proves the null() write fired.
            assert prop.listing_status_source is None
            # The portal declares its own pin inexact.
            assert prop.location_accuracy == "approximate"
            assert prop.enrichment["import"]["method"] == "alert_email"
            # For an ingested row this column means "when the email arrived".
            assert prop.email_date is not None
            assert prop.email_date.date() == INTERNAL_DATE.date()
            assert prop.email_subject == "Alerta: 1 anuncio nuevo en Avilés"
            # No matcher claimed it, so the catch-all did.
            assert prop.search_profile_id == app.config["DEFAULT_PROFILE_ID"]
            # The email's work landed, so the cursor may pass it.
            assert service.last_seen_uid == 1

    def test_the_two_doors_share_one_dedup_key(self, app, uid_file, imap, fetch_calls):
        with app.app_context():
            # Door one: the paste-links import, through its real code path.
            previewed = fotocasa_import.preview_row(_real_listing(STORED_URL))
            outcome = fotocasa_import.insert_rows(
                [previewed], profile_id=app.config["DEFAULT_PROFILE_ID"]
            )
            assert len(outcome["created"]) == 1
            fetch_calls.clear()

            # Door two: the same listing arrives by alert email.
            imap.payloads = {
                1: _alert_email("Alerta: 1 anuncio nuevo en Avilés", [LISTING_URL])
            }
            service = _service()
            created = service.run_ingestion(sync_type="test")

            assert created == 0
            assert Property.query.count() == 1
            # The duplicate was seen before the fetch: no request, no gate.
            assert fetch_calls == []
            # A known listing is a consumed email, not a held one.
            assert service.last_seen_uid == 1

    def test_a_matcher_routes_the_alert_to_its_subscription(
        self, app, uid_file, imap, fetch_calls
    ):
        with app.app_context():
            ribadeo = SearchProfile(
                name="fotocasa Ribadeo",
                is_active=True,
                is_default=False,
                email_matchers=["anuncios nuevos en Ribadeo"],
                travel_targets={"presets": {}, "custom": []},
            )
            db.session.add(ribadeo)
            db.session.commit()
            ribadeo_id = ribadeo.id

            imap.payloads = {
                1: _alert_email("Alerta: 2 anuncios nuevos en Ribadeo", [LISTING_URL])
            }
            _service().run_ingestion(sync_type="test")

            prop = Property.query.one()
            assert prop.search_profile_id == ribadeo_id

    def test_a_rental_is_skipped_under_sale_only(
        self, app, uid_file, imap, monkeypatch
    ):
        def rental(url, session=None):
            listing = _real_listing(url)
            listing.deal_type = "rent"
            return listing

        monkeypatch.setattr(fotocasa_source, "fetch_listing", rental)
        monkeypatch.setattr(
            "services.property_imap_service.SettingsService.get_sale_only",
            staticmethod(lambda: True),
        )

        imap.payloads = {
            1: _alert_email("Alerta: 1 anuncio nuevo en Avilés", [LISTING_URL])
        }
        with app.app_context():
            service = _service()
            created = service.run_ingestion(sync_type="test")

            assert created == 0
            assert Property.query.count() == 0
            # A deliberate filter is a consumed email, not a held one.
            assert service.last_seen_uid == 1


class TestRefusalsHoldTheCursor:
    def test_a_blocked_page_holds_the_uid_and_the_next_run_lands_it(
        self, app, uid_file, imap, monkeypatch
    ):
        """Fotocasa's block page is a 200; consuming the email would lose the
        listing forever, holding it costs one re-read after the block lifts."""
        monkeypatch.setattr(
            fotocasa_source,
            "fetch_listing",
            lambda url, session=None: FotocasaListing(url=url, refusal=REFUSAL_BLOCKED),
        )
        imap.payloads = {
            1: _alert_email("Alerta: 1 anuncio nuevo en Avilés", [LISTING_URL])
        }

        with app.app_context():
            service = _service()
            created = service.run_ingestion(sync_type="test")

            assert created == 0
            assert Property.query.count() == 0
            assert service.last_seen_uid == 0, "a blocked page must hold the cursor"

            # The block lifts; a fresh run re-reads the same email and lands it.
            monkeypatch.setattr(
                fotocasa_source,
                "fetch_listing",
                lambda url, session=None: _real_listing(url),
            )
            second = _service()
            assert second.run_ingestion(sync_type="test") == 1
            assert Property.query.count() == 1
            assert second.last_seen_uid == 1

    def test_a_delisted_listing_is_consumed(self, app, uid_file, imap, monkeypatch):
        """`not_the_listing_page` is the server answering "gone", and the
        answer is the same tomorrow -- holding would stall the cursor forever."""
        monkeypatch.setattr(
            fotocasa_source,
            "fetch_listing",
            lambda url, session=None: FotocasaListing(
                url=url, refusal=REFUSAL_NOT_A_LISTING
            ),
        )
        imap.payloads = {
            1: _alert_email("Alerta: 1 anuncio nuevo en Avilés", [LISTING_URL])
        }

        with app.app_context():
            service = _service()
            assert service.run_ingestion(sync_type="test") == 0
            assert Property.query.count() == 0
            assert service.last_seen_uid == 1

    def test_consecutive_refusals_stop_the_fetching(
        self, app, uid_file, imap, monkeypatch
    ):
        """Measured 2026-08-17: five requests at 3 s were enough to be blocked
        for minutes. The run stops walking into the wall; the email is held."""
        calls: list[str] = []

        def blocked(url, session=None):
            calls.append(url)
            return FotocasaListing(url=url, refusal=REFUSAL_BLOCKED)

        monkeypatch.setattr(fotocasa_source, "fetch_listing", blocked)

        urls = [
            f"https://www.fotocasa.es/es/comprar/terreno/aviles/x/19028091{i}/d"
            for i in range(5)
        ]
        imap.payloads = {1: _alert_email("Alerta: 5 anuncios nuevos", urls)}

        with app.app_context():
            service = _service()
            assert service.run_ingestion(sync_type="test") == 0
            assert len(calls) == FOTOCASA_MAX_CONSECUTIVE_REFUSALS
            assert service.last_seen_uid == 0

    def test_a_held_fotocasa_email_does_not_lose_the_idealista_mail_around_it(
        self, app, uid_file, imap, monkeypatch
    ):
        monkeypatch.setattr(
            fotocasa_source,
            "fetch_listing",
            lambda url, session=None: FotocasaListing(url=url, refusal=REFUSAL_BLOCKED),
        )

        def idealista_email(listing_id: int) -> bytes:
            msg = EmailMessage()
            msg["Subject"] = "New home in your search"
            msg["From"] = "Idealista <noresponder@idealista.com>"
            msg.set_content("plain")
            msg.add_alternative(
                f'<html><body><a href="https://www.idealista.com/inmueble/'
                f'{listing_id}/">Casa</a><p>250.000 €</p><p>120 m²</p></body></html>',
                subtype="html",
            )
            return msg.as_bytes()

        imap.payloads = {
            1: idealista_email(990001),
            2: _alert_email("Alerta: 1 anuncio nuevo", [LISTING_URL]),
            3: idealista_email(990002),
        }

        with app.app_context():
            service = _service()
            created = service.run_ingestion(sync_type="test")

            # Both idealista listings landed; only the fotocasa one is pending.
            assert created == 2
            ids = {p.idealista_property_id for p in Property.query.all()}
            assert ids == {990001, 990002}
            # The watermark stops behind the held email; UID 3's work is
            # committed, so re-reading it next run is a dedup no-op.
            assert service.last_seen_uid == 1


class TestDisabled:
    def test_empty_senders_turn_the_recognition_off(
        self, app, uid_file, imap, monkeypatch
    ):
        monkeypatch.setattr(Config, "FOTOCASA_ALERT_SENDERS", "")

        def explode(url, session=None):  # pragma: no cover - must not run
            raise AssertionError("fetched a fotocasa page while disabled")

        monkeypatch.setattr(fotocasa_source, "fetch_listing", explode)

        imap.payloads = {
            1: _alert_email("Alerta: 1 anuncio nuevo en Avilés", [LISTING_URL])
        }
        with app.app_context():
            service = _service()
            assert service.run_ingestion(sync_type="test") == 0
            assert Property.query.count() == 0
            # Consumed as any URL-less email is: the old behaviour, verbatim.
            assert service.last_seen_uid == 1
