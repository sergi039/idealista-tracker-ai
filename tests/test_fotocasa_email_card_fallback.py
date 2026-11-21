"""A fotocasa listing whose page is refused is stored from the email (#636).

Owner decision, 2026-11-21: when the portal will not give the listing page,
the object is created from the fields of the alert email itself -- without a
request to the page, the way yaencontre always is -- and the retry list stays
bounded. These tests run the real alert of 2026-08-30
(`tests/data/fotocasa_alert_arteixo.html`) through the real ingest loop with
only the IMAP server and the page fetch replaced.

Where a test needs a page for the same listing, the alert's id is substituted
for the one the committed page fixture carries (190280914); the page says
Avilés where the card says Arteixo, which is itself a useful difference: the
card's municipality is kept, the page's other fields complete the row.
"""

import pathlib
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile, SyncHistory
from services import fotocasa_import, fotocasa_source
from services.fotocasa_source import (
    REFUSAL_BLOCKED,
    REFUSAL_NOT_A_LISTING,
    FotocasaListing,
    cards_in_email,
    parse_listing,
)
from services.property_imap_service import (
    PORTAL_MAX_CONSECUTIVE_REFUSALS,
    PropertyIMAPService,
)
from tests import setup_test_environment
from utils.uid_cursor import HeldUids, write_uid_file

DATA = pathlib.Path(__file__).parent / "data"
ALERT = (DATA / "fotocasa_alert_arteixo.html").read_text(encoding="utf-8")
PAGE = (DATA / "fotocasa_listing_190280914.html").read_text(encoding="utf-8")
ALERT_ID = 190485369
PAGE_ID = 190280914
INTERNAL_DATE = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)


def _email(body_html: str, subject: str = "Nuevo anuncio en Arteixo") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "fotocasa <enviosfotocasa@fotocasa.es>"
    msg.set_content("plain-text half")
    msg.add_alternative(body_html, subtype="html")
    return msg.as_bytes()


def _card_html(
    listing_id: int,
    *,
    price: str = "295.000 €",
    heading: str = "<strong>casa-chalet</strong>, Arteixo",
    facts: str = "5 habs · 251 m² · 3 baños",
    kind: str = "vivienda",
) -> str:
    url = (
        f"https://www.fotocasa.es/es/comprar/{kind}/arteixo/x/{listing_id}/d"
        "?stc=em&amp;utm_medium=email"
    )

    def a(inner: str) -> str:
        return f'<a href="{url}">{inner}</a>'

    return (
        f"<table><tr><td>{a('<img src=cid:p>')}</td></tr>"
        f"<tr><td>{a(f'<span>{price}</span>')}</td></tr>"
        f"<tr><td>{a(heading)}</td></tr>"
        f"<tr><td>{a(facts)}</td></tr>"
        f"<tr><td>{a('Ver anuncio')}</td></tr></table>"
    )


class _FakeIMAPClient:
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


@pytest.fixture
def app(monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", False)
    monkeypatch.setattr(Config, "FOTOCASA_EMAIL_CARD_FOR_HELD", False)
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


@pytest.fixture
def portal(monkeypatch):
    """The page fetch, with a switch: `blocked` refuses, `open` serves the
    committed page. Records every URL asked for."""
    state = {"mode": "blocked", "calls": []}

    def fake_fetch(url, session=None):
        state["calls"].append(url)
        if state["mode"] == "blocked":
            return FotocasaListing(url=url, refusal=REFUSAL_BLOCKED)
        if state["mode"] == "gone":
            return FotocasaListing(url=url, refusal=REFUSAL_NOT_A_LISTING)
        return parse_listing(PAGE, url)

    monkeypatch.setattr(fotocasa_source, "fetch_listing", fake_fetch)
    return state


def _service() -> PropertyIMAPService:
    service = PropertyIMAPService()
    service.user = "owner@example.com"
    service.password = "dummy"
    service.host = "imap.example.com"
    service.folder = "IdealistaProperties"
    return service


def _held(uid_file) -> list:
    return HeldUids(str(uid_file) + ".held.json").uids()


class TestCardParser:
    def test_the_real_alert_yields_its_card_fields(self):
        (card,) = cards_in_email(ALERT)
        assert card.listing_id == ALERT_ID
        assert card.price == 295000.0
        assert card.area == 251.0
        assert card.area_type == "built"
        assert card.title == "casa-chalet, Arteixo"
        assert card.municipality == "Arteixo"
        assert card.building_type == "casa-chalet"
        assert card.deal_type == "sale"
        assert card.attributes == {"bedrooms": 5, "bathrooms": 3}
        assert card.url.endswith(f"/{ALERT_ID}/d") and "?" not in card.url

    def test_a_card_states_only_what_it_says(self):
        html = _card_html(190280901, price="", heading="Terreno", facts="Ver detalles")
        assert cards_in_email(html) == [], "nothing stated is no card at all"

        (card,) = cards_in_email(
            _card_html(190280902, heading="Terreno", facts="1.200 m²", kind="terreno")
        )
        assert card.municipality is None, "a bare word is not a municipality"
        assert card.building_type is None
        assert card.area == 1200.0 and card.area_type == "plot"
        assert card.attributes == {}

    def test_a_struck_price_is_not_the_asking_price(self):
        html = _card_html(190280903).replace(
            "<span>295.000 €</span>",
            '<span style="text-decoration:line-through">320.000 €</span>',
        )
        (card,) = cards_in_email(html)
        assert card.price is None

    def test_one_card_per_listing_in_email_order(self):
        cards = cards_in_email(_card_html(190280911) + _card_html(190280912))
        assert [c.listing_id for c in cards] == [190280911, 190280912]

    def test_a_rental_path_is_read_as_a_rental(self):
        html = _card_html(190280913).replace("/comprar/", "/alquiler/")
        (card,) = cards_in_email(html)
        assert card.deal_type == "rent"
        html = _card_html(190280914).replace("/comprar/", "/otra-cosa/")
        (card,) = cards_in_email(html)
        assert card.deal_type is None, "an unknown transaction stays unknown"


class TestRefusedPageStoresFromTheEmail:
    def test_a_blocked_page_creates_the_row_and_consumes_the_email(
        self, app, uid_file, imap, portal
    ):
        imap.payloads = {1: _email(ALERT)}
        service = _service()
        assert service.run_ingestion(sync_type="test") == 1

        (prop,) = Property.query.all()
        assert prop.source_email_id == f"fotocasa:{ALERT_ID}"
        assert float(prop.price) == 295000.0
        assert float(prop.area) == 251.0 and prop.area_type == "built"
        assert prop.municipality == "Arteixo"
        assert prop.title == "casa-chalet, Arteixo"
        # Not stated by the email, so unknown -- never invented.
        assert prop.description is None
        assert prop.location_lat is None and prop.location_lon is None
        assert prop.plot_area is None
        imported = prop.enrichment["import"]
        assert imported["method"] == fotocasa_import.EMAIL_ONLY_METHOD
        assert imported["email_only"] is True
        assert imported["coordinate"] is None
        assert "advertiser" not in prop.enrichment
        assert prop.listing_status_source is None
        assert prop.listing_status == "active"  # the column default, not a claim

        assert _held(uid_file) == [], "a stored email leaves the retry list"
        assert service.last_seen_uid == 1
        run = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
        assert run.status == "completed"
        assert run.new_properties_added == 1

    def test_a_refusal_is_not_read_as_gone(self, app, uid_file, imap, portal):
        """The server answering "no longer served" consumes the email and stores
        nothing; a block or a timeout stores the card and says nothing about the
        listing's status."""
        portal["mode"] = "gone"
        imap.payloads = {1: _email(ALERT)}
        assert _service().run_ingestion(sync_type="test") == 0
        assert Property.query.count() == 0

        portal["mode"] = "blocked"
        imap.payloads = {2: _email(ALERT)}
        assert _service().run_ingestion(sync_type="test") == 1
        (prop,) = Property.query.all()
        assert prop.listing_status == "active"
        assert prop.listing_removed_date is None

    def test_after_the_refusal_allowance_the_cards_are_stored_without_a_fetch(
        self, app, uid_file, imap, portal
    ):
        ids = [190280920 + i for i in range(6)]
        body = "".join(_card_html(i) for i in ids)
        imap.payloads = {1: _email(body)}

        service = _service()
        assert service.run_ingestion(sync_type="test") == 6
        assert len(portal["calls"]) == PORTAL_MAX_CONSECUTIVE_REFUSALS
        assert Property.query.count() == 6
        assert _held(uid_file) == []

    def test_a_listing_with_no_card_is_still_held(self, app, uid_file, imap, portal):
        """The old rule where the email states nothing to store."""
        url = f"https://www.fotocasa.es/es/comprar/terreno/aviles/x/{PAGE_ID}/d"
        imap.payloads = {1: _email(f'<a href="{url}">Ver anuncio</a>')}
        service = _service()
        assert service.run_ingestion(sync_type="test") == 0
        assert Property.query.count() == 0
        assert _held(uid_file) == [1]

    def test_the_retry_list_does_not_grow_while_the_portal_stays_blocked(
        self, app, uid_file, imap, portal
    ):
        for uid in range(1, 8):
            imap.payloads[uid] = _email(_card_html(190280930 + uid))
            _service().run_ingestion(sync_type="test")
            assert _held(uid_file) == []
        assert Property.query.count() == 7

    def test_the_same_email_twice_is_one_row(self, app, uid_file, imap, portal):
        imap.payloads = {1: _email(ALERT), 2: _email(ALERT)}
        _service().run_ingestion(sync_type="test")
        assert Property.query.count() == 1

    def test_a_rental_card_is_skipped_under_sale_only(
        self, app, uid_file, imap, portal, monkeypatch
    ):
        monkeypatch.setattr(
            "services.property_imap_service.SettingsService.get_sale_only",
            staticmethod(lambda: True),
        )
        body = _card_html(190280941).replace("/comprar/", "/alquiler/")
        imap.payloads = {1: _email(body)}
        service = _service()
        assert service.run_ingestion(sync_type="test") == 0
        assert Property.query.count() == 0
        assert _held(uid_file) == [] and service.last_seen_uid == 1


class TestEmailsAlreadyWaitingStayWithTheOwner:
    def _wait(self, uid_file, imap):
        write_uid_file(str(uid_file), 5)
        HeldUids(str(uid_file) + ".held.json").hold(
            1, "its listing page could not be read"
        )
        imap.payloads = {1: _email(ALERT)}

    def test_a_held_email_keeps_the_old_rule_by_default(
        self, app, uid_file, imap, portal
    ):
        self._wait(uid_file, imap)
        assert _service().run_ingestion(sync_type="test") == 0
        assert Property.query.count() == 0
        assert _held(uid_file) == [1]

    def test_the_owner_can_let_the_cards_drain_the_list(
        self, app, uid_file, imap, portal, monkeypatch
    ):
        monkeypatch.setattr(Config, "FOTOCASA_EMAIL_CARD_FOR_HELD", True)
        self._wait(uid_file, imap)
        assert _service().run_ingestion(sync_type="test") == 1
        assert Property.query.count() == 1
        assert _held(uid_file) == []


class TestALaterPageCompletesTheSameRow:
    @staticmethod
    def _alert_for_page_listing() -> str:
        return ALERT.replace(str(ALERT_ID), str(PAGE_ID))

    def test_a_page_that_answers_later_fills_what_the_row_lacked(
        self, app, uid_file, imap, portal
    ):
        imap.payloads = {1: _email(self._alert_for_page_listing())}
        _service().run_ingestion(sync_type="test")
        (prop,) = Property.query.all()
        assert fotocasa_import.is_email_only(prop)
        assert prop.description is None and prop.location_lat is None
        card_price, card_area = float(prop.price), float(prop.area)
        row_id = prop.id

        portal["mode"] = "open"
        imap.payloads[2] = _email(self._alert_for_page_listing())
        service = _service()
        assert service.run_ingestion(sync_type="test") == 0

        assert Property.query.count() == 1, "completed, not duplicated"
        prop = db.session.get(Property, row_id)
        assert not fotocasa_import.is_email_only(prop)
        assert prop.description, "the page's text arrived"
        assert prop.location_lat is not None
        assert prop.location_accuracy == "approximate"
        # What the card stated stays; the page does not overwrite it.
        assert float(prop.price) == card_price and float(prop.area) == card_area
        assert prop.municipality == "Arteixo"
        assert prop.title != "casa-chalet, Arteixo", "the stub heading is replaced"
        imported = prop.enrichment["import"]
        assert imported["coordinate"]["source"] == "fotocasa"
        assert "completed_from_page_at" in imported
        assert imported["method"] == fotocasa_import.EMAIL_ONLY_METHOD
        assert "advertiser" in prop.enrichment
        assert _held(uid_file) == []

    def test_a_refusal_at_completion_changes_nothing_and_holds_nothing(
        self, app, uid_file, imap, portal
    ):
        imap.payloads = {1: _email(self._alert_for_page_listing())}
        _service().run_ingestion(sync_type="test")
        before = Property.query.one().enrichment

        imap.payloads[2] = _email(self._alert_for_page_listing())
        service = _service()
        service.run_ingestion(sync_type="test")

        (prop,) = Property.query.all()
        assert fotocasa_import.is_email_only(prop)
        assert prop.enrichment == before
        assert _held(uid_file) == [] and service.last_seen_uid == 2

    def test_gone_at_completion_does_not_mark_the_row_removed(
        self, app, uid_file, imap, portal
    ):
        imap.payloads = {1: _email(self._alert_for_page_listing())}
        _service().run_ingestion(sync_type="test")
        portal["mode"] = "gone"
        imap.payloads[2] = _email(self._alert_for_page_listing())
        _service().run_ingestion(sync_type="test")
        (prop,) = Property.query.all()
        assert prop.listing_status == "active" and fotocasa_import.is_email_only(prop)

    def test_a_row_the_page_built_is_never_touched_by_a_later_read(
        self, app, uid_file, imap, portal
    ):
        portal["mode"] = "open"
        imap.payloads = {1: _email(self._alert_for_page_listing())}
        _service().run_ingestion(sync_type="test")
        (prop,) = Property.query.all()
        assert not fotocasa_import.is_email_only(prop)
        before = (prop.title, prop.description, dict(prop.enrichment))

        row = fotocasa_import.preview_row(
            parse_listing(PAGE, f"https://www.fotocasa.es/es/x/y/z/{PAGE_ID}/d")
        )
        assert fotocasa_import.complete_from_page(prop, row) is False
        assert (prop.title, prop.description, dict(prop.enrichment)) == before

    def test_a_coordinate_the_row_already_has_is_not_moved(
        self, app, uid_file, imap, portal
    ):
        imap.payloads = {1: _email(self._alert_for_page_listing())}
        _service().run_ingestion(sync_type="test")
        prop = Property.query.one()
        prop.location_lat, prop.location_lon = 43.0, -8.0
        prop.location_accuracy = "approximate"
        db.session.commit()

        row = fotocasa_import.preview_row(
            parse_listing(PAGE, f"https://www.fotocasa.es/es/x/y/z/{PAGE_ID}/d")
        )
        assert fotocasa_import.complete_from_page(prop, row) is True
        assert (float(prop.location_lat), float(prop.location_lon)) == (43.0, -8.0)
        assert prop.enrichment["import"]["coordinate"] is not None
