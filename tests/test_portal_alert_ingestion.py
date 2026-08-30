"""Milanuncios and yaencontre alert emails reach the same table, measured.

Every fixture here is real, from the owner's mailbox and the live portal on
2026-08-30, committed token-redacted (tracking tokens, recipient
identifiers; the structure the parsers read is verbatim):

* `yaencontre_alert_boiro.html` -- the "Tenemos más inmuebles para tu
  búsqueda en Boiro" alert, ten listing cards. Yaencontre's portal answers
  DataDome to every request from these machines (403 even for robots.txt),
  so the email card is the whole source: no fetch exists to mock.
* `milanuncios_alert_solares.html` / `..._chalets.html` -- the daily
  digests. Every anchor is an opaque SparkPost tracker, so the card
  trackers are resolved (a redirect read) and the ad page is fetched;
  the page fixture `milanuncios_listing_612329827.html` is the real ad's
  `__INITIAL_PROPS__` payload.
* `fotocasa_alert_arteixo.html` -- the first real fotocasa alert, closing
  the "modeled template" caveat `test_fotocasa_email_ingestion.py` shipped
  with in the morning.

The invariants are the fotocasa door's, through the same one builder
(`fotocasa_import.build_property`): `<source>:<id>` dedup key, NULL
`listing_status_source`, portal pin where a coordinate exists, refusals
hold the UID cursor, answered "gone"/"demand" is consumed.
"""

import pathlib
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services import (
    advertiser,
    fotocasa_source,
    milanuncios_source,
    yaencontre_source,
)
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment

DATA = pathlib.Path(__file__).parent / "data"
INTERNAL_DATE = datetime(2026, 8, 30, 11, 30, tzinfo=timezone.utc)

MA_PAGE = (DATA / "milanuncios_listing_612329827.html").read_text()
MA_URL = (
    "https://www.milanuncios.com/venta-de-chalets-en-los-quintanales-(mieres)"
    "-asturias/los-quintanales-aldea-los-quintanales-612329827.htm"
)


def _wrap_email(subject: str, body_html: str, sender: str) -> bytes:
    """The real alert body inside a minimal RFC822 envelope."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.set_content("plain-text half")
    msg.add_alternative(body_html, subtype="html")
    return msg.as_bytes()


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


@pytest.fixture
def app(monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
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


class TestTheRealFotocasaAlert:
    def test_the_real_template_yields_exactly_its_one_listing(self):
        """Closes the morning's caveat: the recognizer against the real mail."""
        body = (DATA / "fotocasa_alert_arteixo.html").read_text()
        urls = fotocasa_source.listing_urls_in_text(body)
        assert len(urls) == 1
        assert fotocasa_source.listing_id_from_url(urls[0]) == 190485369


class TestYaencontreCards:
    def test_the_real_alert_parses_to_its_ten_cards(self):
        cards = yaencontre_source.cards_in_email(
            (DATA / "yaencontre_alert_boiro.html").read_text()
        )
        assert [c.listing_id for c in cards] == [
            112395195,
            112387020,
            112383620,
            112379804,
            112377899,
            112365267,
            112361336,
            112358154,
            112355259,
            112301865,
        ]
        first = cards[0]
        # Field by value, not by presence: a card full of Nones would satisfy
        # anything weaker (the cadastre-render lesson).
        assert first.title == "Casa adosada en venta en avenida Compostela, Outes"
        assert first.price == 180000.0
        assert first.area == 294.0
        assert first.area_type == "built"
        assert first.municipality == "Outes"
        assert first.attributes == {"bedrooms": 7, "bathrooms": 2}
        assert first.url == (
            "https://www.yaencontre.com/venta/casa/inmueble-75866-112395195"
        )

    def test_the_identity_is_the_second_number(self):
        # 79977 fronts three different listings of one seller in the real
        # mail; keying on it would collapse them into one row.
        assert (
            yaencontre_source.listing_id_from_url(
                "https://www.yaencontre.com/venta/casa/inmueble-79977-112387020"
            )
            == 112387020
        )
        # The search page carries no inmueble pair and must never be an id.
        assert (
            yaencontre_source.listing_id_from_url(
                "https://www.yaencontre.com/venta/casas/custom/f--350000euros,150m2/mapa"
            )
            is None
        )


class TestMilanunciosSource:
    def test_only_the_card_trackers_are_offered_for_resolution(self):
        """The same template wraps Eliminar / Desactívala / Dar de baja in
        identical trackers; touching those is how an alert gets unsubscribed
        by a robot. A card is an anchor wrapping an ad photo."""
        solares = milanuncios_source.card_tracker_urls(
            (DATA / "milanuncios_alert_solares.html").read_text()
        )
        chalets = milanuncios_source.card_tracker_urls(
            (DATA / "milanuncios_alert_chalets.html").read_text()
        )
        # Exactly the digests' own counts: "3 novedades" and "2 novedades".
        assert len(solares) == 3
        assert len(chalets) == 2

    def test_the_real_page_parses_field_by_field(self):
        row = milanuncios_source.parse_listing(MA_PAGE, MA_URL)
        assert row["status"] == "new"
        assert row["listing_id"] == 612329827
        assert row["price"] == 119000.0
        assert row["area"] == 100.0
        assert row["area_type"] == "built"
        assert row["deal_type"] == "sale"
        # "Los Quintanales (Mieres)" names the village; the INE join wants
        # the municipality in the parentheses.
        assert row["municipality"] == "Mieres"
        assert row["locality"] == "Los Quintanales (Mieres)"
        assert row["province"] == "Asturias"
        assert row["latitude"] == pytest.approx(43.2621388)
        assert row["longitude"] == pytest.approx(-5.7122013)
        assert row["publisher_type"] == "private"
        assert row["attributes"] == {"bedrooms": 3, "bathrooms": 2}

    def test_a_demand_ad_is_refused_not_stored(self):
        """Milanuncios carries "se busca" adverts; storing one would put a
        phantom listing in a table nothing can delete from."""
        body = MA_PAGE.replace(
            '\\"sellType\\":\\"supply\\"', '\\"sellType\\":\\"demand\\"'
        )
        assert body != MA_PAGE, "the fixture no longer carries the sellType key"
        row = milanuncios_source.parse_listing(body, MA_URL)
        assert row["status"] == "refused"
        assert row["reason"] == milanuncios_source.REFUSAL_NOT_SUPPLY


class TestYaencontreIngestion:
    def test_the_real_alert_creates_ten_rows_from_the_email_alone(
        self, app, uid_file, imap, monkeypatch
    ):
        def no_network(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("a yaencontre ingest must make no request")

        monkeypatch.setattr(milanuncios_source, "resolve_tracker", no_network)
        monkeypatch.setattr(milanuncios_source, "fetch_listing", no_network)
        monkeypatch.setattr(fotocasa_source, "fetch_listing", no_network)

        imap.payloads = {
            1: _wrap_email(
                "Tenemos más inmuebles para tu búsqueda en Boiro (personalizada)",
                (DATA / "yaencontre_alert_boiro.html").read_text(),
                "yaencontre <no-reply@envios.yaencontre.com>",
            )
        }
        with app.app_context():
            service = _service()
            created = service.run_ingestion(sync_type="test")

            assert created == 10
            assert service.last_seen_uid == 1

            prop = Property.query.filter_by(
                source_email_id="yaencontre:112395195"
            ).one()
            assert prop.title == "Casa adosada en venta en avenida Compostela, Outes"
            assert float(prop.price) == 180000.0
            assert float(prop.area) == 294.0
            assert prop.municipality == "Outes"
            assert prop.url == (
                "https://www.yaencontre.com/venta/casa/inmueble-75866-112395195"
            )
            # Nobody verified the listing is live: NULL, same as every portal
            # door (STATUS-002).
            assert prop.listing_status_source is None
            # The email said nothing about who is selling, so no advertiser
            # block: an absent key reads "not established", which is true.
            assert advertiser.ENRICHMENT_KEY not in (prop.enrichment or {})
            # And nothing placed a coordinate: the card carries none, and a
            # fabricated one would poison four measurements downstream.
            assert prop.location_lat is None
            assert prop.enrichment["import"]["source"] == "yaencontre"
            assert prop.enrichment["import"]["method"] == "alert_email"

            # The same alert re-read tomorrow is ten dedup no-ops.
            _FakeIMAPClient.payloads = dict(imap.payloads)
            second = _service()
            second.last_seen_uid = 0
            assert second.run_ingestion(sync_type="test") == 0
            assert Property.query.count() == 10


class TestMilanunciosIngestion:
    def _digest_email(self) -> bytes:
        return _wrap_email(
            "🏠 2 novedades en Venta de chalets en Asturias",
            (DATA / "milanuncios_alert_chalets.html").read_text(),
            "milanuncios <no-responder@milanuncios.com>",
        )

    def test_the_digest_resolves_fetches_and_lands_its_cards(
        self, app, uid_file, imap, monkeypatch
    ):
        second_url = MA_URL.replace("612329827", "612329911")
        targets = iter([MA_URL, second_url])
        resolved: list[str] = []

        def fake_resolve(url, session=None):
            resolved.append(url)
            return next(targets)

        fetched: list[str] = []

        def fake_fetch(url, session=None):
            fetched.append(url)
            row = milanuncios_source.parse_listing(MA_PAGE, MA_URL)
            if url != MA_URL:
                row["listing_id"] = milanuncios_source.listing_id_from_url(url)
                row["url"] = url
                row["title"] = "Urbanización Río Nalón"
            return row

        monkeypatch.setattr(milanuncios_source, "resolve_tracker", fake_resolve)
        monkeypatch.setattr(milanuncios_source, "fetch_listing", fake_fetch)

        imap.payloads = {1: self._digest_email()}
        with app.app_context():
            service = _service()
            created = service.run_ingestion(sync_type="test")

            assert created == 2
            assert len(resolved) == 2, "one resolve per card tracker"
            assert len(fetched) == 2
            assert service.last_seen_uid == 1

            prop = Property.query.filter_by(
                source_email_id="milanuncios:612329827"
            ).one()
            assert float(prop.price) == 119000.0
            assert prop.municipality == "Mieres"
            assert prop.listing_status_source is None
            # The portal pin, as approximate as every portal coordinate.
            assert float(prop.location_lat) == pytest.approx(43.2621388)
            assert prop.location_accuracy == "approximate"
            # sellerType "private" is the portal saying the owner sells.
            assert prop.enrichment[advertiser.ENRICHMENT_KEY]["state"] == (
                advertiser.OWNER
            )
            assert prop.enrichment["import"]["source"] == "milanuncios"

    def test_an_unresolved_tracker_holds_the_uid_cursor(
        self, app, uid_file, imap, monkeypatch
    ):
        monkeypatch.setattr(
            milanuncios_source, "resolve_tracker", lambda url, session=None: None
        )

        def must_not_fetch(url, session=None):  # pragma: no cover
            raise AssertionError("nothing resolved, nothing may be fetched")

        monkeypatch.setattr(milanuncios_source, "fetch_listing", must_not_fetch)

        imap.payloads = {1: self._digest_email()}
        with app.app_context():
            service = _service()
            assert service.run_ingestion(sync_type="test") == 0
            assert Property.query.count() == 0
            assert service.last_seen_uid == 0, "held for the next run"

    def test_a_gone_or_demand_ad_is_consumed(self, app, uid_file, imap, monkeypatch):
        monkeypatch.setattr(
            milanuncios_source,
            "resolve_tracker",
            lambda url, session=None: MA_URL,
        )
        monkeypatch.setattr(
            milanuncios_source,
            "fetch_listing",
            lambda url, session=None: {
                "url": url,
                "listing_id": 612329827,
                "status": "refused",
                "reason": milanuncios_source.REFUSAL_NOT_A_LISTING,
            },
        )

        imap.payloads = {1: self._digest_email()}
        with app.app_context():
            service = _service()
            assert service.run_ingestion(sync_type="test") == 0
            assert Property.query.count() == 0
            # The server answered; tomorrow's answer is the same.
            assert service.last_seen_uid == 1

    def test_ids_do_not_dedupe_across_portals(self, app, uid_file, imap, monkeypatch):
        """fotocasa 612329827 and milanuncios 612329827 are different
        listings; a shared id namespace would silently drop one."""
        with app.app_context():
            other = Property(
                source_email_id="fotocasa:612329827",
                url="https://www.fotocasa.es/es/comprar/vivienda/x/y/612329827/d",
                title="A fotocasa listing wearing the same number",
            )
            db.session.add(other)
            db.session.commit()

            monkeypatch.setattr(
                milanuncios_source,
                "resolve_tracker",
                lambda url, session=None: MA_URL,
            )
            monkeypatch.setattr(
                milanuncios_source,
                "fetch_listing",
                lambda url, session=None: milanuncios_source.parse_listing(
                    MA_PAGE, MA_URL
                ),
            )
            imap.payloads = {
                1: _wrap_email(
                    "🏠 1 novedades en Venta de chalets en Asturias",
                    (DATA / "milanuncios_alert_chalets.html").read_text(),
                    "milanuncios <no-responder@milanuncios.com>",
                )
            }
            service = _service()
            assert service.run_ingestion(sync_type="test") >= 1
            assert (
                Property.query.filter_by(
                    source_email_id="milanuncios:612329827"
                ).count()
                == 1
            )
