"""#645: a listing's area is the one its own card states, never the search name.

An idealista alert's subject carries the owner's saved-search NAME
("... costa - 350k - 150m2!"). Reading it for the area stamped every listing of
that search with 150 m2.

There is no captured idealista alert in tests/data. The card texts below are
RECONSTRUCTED from stored `description` values ("320.000 € 1.013 €/m² 4 hab.
316 m²", "99,000 € 309 €/m² 5 bed. 320 m2") and wrapped in minimal HTML; they
are not a real e-mail.
"""

from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment
from tests.test_last_seen_uid_cursor import _FakeIMAPClient, _make_service
from utils.email_parser import EmailParser
from utils.idealista_extractors import (
    extract_card_area_m2,
    extract_card_blocks_m2,
    extract_card_price_blocks_m2,
    extract_price,
    single_listing_mail,
)

SEARCH_SUBJECT = (
    "¡Nuevo chalet independiente en tu búsqueda: Galicia Rias Bajas costa - "
    "casas hasta 350k - 150m2!"
)
SPANISH_CARD = "320.000 € 1.013 €/m² 4 hab. 316 m²"
ENGLISH_CARD = "99,000 € 309 €/m² 6 bed. 150 m2"


class TestTheCardReading:
    @pytest.mark.parametrize(
        "card, expected",
        [
            (SPANISH_CARD, 316.0),
            (ENGLISH_CARD, 150.0),
            ("99,000 € 309 €/m² 5 bed. 320 m2 CHANCE. Sale of housing", 320.0),
            ("250.000 € 200 €/m² 3 hab. 2 baños 1.250 m²", 1250.0),
            ("250.000 € 3 hab. 85,5 m²", 85.5),
            # a plot card: no rooms
            ("59.000 € 118 €/m² 500 m²", 500.0),
            ("59.000 € 12.500 m²", 12500.0),
            # <p> boundaries are whitespace
            ("<p>320.000 €</p><p>1.013 €/m²</p><p>4 hab.</p><p>316 m²</p>", 316.0),
        ],
    )
    def test_the_figure_after_the_price_block_is_the_area(self, card, expected):
        assert extract_card_area_m2(card) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "1 nuevo anuncio 320.000 € 1.013 €/m² 4 hab.",
            "Chalet in Foo, costa - up to 350k - 150m2",
            "Tu búsqueda: 150m2 mínimo. 320.000 € 4 hab.",
            "",
        ],
    )
    def test_a_card_without_an_area_states_none(self, text):
        assert extract_card_area_m2(text) is None

    def test_a_search_summary_figure_is_not_a_card_figure(self):
        body = f"Tu búsqueda: costa 150 m2 o más. {SPANISH_CARD}"
        assert extract_card_blocks_m2(body) == [316.0]

    def test_each_listing_gets_its_own_card(self):
        body = (
            '<a href="https://www.idealista.com/inmueble/111/">Chalet A</a>'
            "<p>320.000 € 1.013 €/m² 4 hab. 316 m²</p>"
            '<a href="https://www.idealista.com/inmueble/222/">Chalet B</a>'
            "<p>99.000 € 309 €/m² 5 hab. 320 m²</p>"
        )
        assert extract_card_area_m2(body, idealista_property_id=111) == 316.0
        assert extract_card_area_m2(body, idealista_property_id=222) == 320.0

    def test_a_card_without_an_area_does_not_borrow_the_next_cards(self):
        body = (
            '<a href="https://www.idealista.com/inmueble/111/">Chalet A</a>'
            "<p>320.000 € 1.013 €/m² 4 hab.</p>"
            '<a href="https://www.idealista.com/inmueble/222/">Chalet B</a>'
            "<p>99.000 € 309 €/m² 5 hab. 320 m²</p>"
        )
        assert extract_card_area_m2(body, idealista_property_id=111) is None
        assert extract_card_area_m2(body, idealista_property_id=222) == 320.0


def _raw(subject: str, listings: list[tuple[int, str]]) -> bytes:
    cards = "".join(
        f'<a href="https://www.idealista.com/inmueble/{pid}/">Chalet {pid}</a>'
        f"<p>{card}</p>"
        for pid, card in listings
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Idealista <noresponder@idealista.com>"
    msg.set_content("See the listing in your browser")
    msg.add_alternative(f"<html><body>{cards}</body></html>", subtype="html")
    return msg.as_bytes()


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def service(app, tmp_path, monkeypatch) -> PropertyIMAPService:
    monkeypatch.setattr(
        Config, "LAST_SEEN_UID_PROPERTIES_PATH", str(tmp_path / ".uid_props")
    )
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PATH", str(tmp_path / ".uid"))
    monkeypatch.setattr(Config, "BASE_DIR", str(tmp_path / "base"))
    return _make_service(monkeypatch)


def _ingest(service, raw: bytes) -> list[dict]:
    _FakeIMAPClient.payloads = {1: raw}
    emails = service.get_idealista_emails()
    assert len(emails) == 1
    return emails


class TestRealPriceDropAndEnglishCards:
    """Verbatim card texts from real alert e-mails (2026-11-10)."""

    @pytest.mark.parametrize(
        "card, expected",
        [
            (
                "fotos Casa o chalet independiente en Baris, Carballo 325.000€ "
                "↓4% 312.000 € 910 €/m² 6 hab. 357 m² Chalet independiente en "
                "venta en Carballo, ",
                357.0,
            ),
            (
                "Ver 32 fotos Casa o chalet independiente en Valga 85.000€ ↓12% "
                "75.000 € 518 €/m² 3 hab. 164 m² Se vende casa para reformar ",
                164.0,
            ),
            (
                "Casa o chalet independiente en Camino dos Vilares, San "
                "Mateo-Castro-O ... 169.710 € 606 €/m² 3 hab. 280 m² PRECIO DE "
                "REMATE ",
                280.0,
            ),
            ("Casa en Foo 325.000€ ↑4% 338.000 € 910 €/m² 6 hab. 357 m² x", 357.0),
            ("Casa en Foo 325.000€ ↓4,5% 310.000 € 910 €/m² 6 hab. 357 m² x", 357.0),
            ("Casa en Foo 325.000€ ↓4.5% 310.000 € 3 hab. 1.357 m² x", 1357.0),
            # English separators: comma groups thousands
            ("58,000 € 1,500 m2 Developed plot ? NEW BUILDING PLOT", 1500.0),
        ],
    )
    def test_the_card_area_is_read(self, card, expected):
        assert extract_card_area_m2(card) == expected

    def test_the_block_price_is_the_new_price(self):
        blocks = extract_card_price_blocks_m2(
            "325.000€ ↓4% 312.000 € 910 €/m² 6 hab. 357 m²"
        )

        assert blocks == [(312000.0, 357.0)]
        assert extract_card_price_blocks_m2("58,000 € 1,500 m2") == [(58000.0, 1500.0)]


class TestRoomLabels:
    @pytest.mark.parametrize(
        "counts",
        [
            "4 hab.",
            "4 habs.",
            "4 habitación",
            "4 habitaciones",
            "4 dorm.",
            "4 dormitorio",
            "4 dormitorios",
            "4 bed.",
            "4 beds",
            "4 bedroom",
            "4 bedrooms",
            "2 baño",
            "2 baños",
            "2 bath",
            "2 baths",
            "2 bathrooms",
            "4 habitaciones 2 baños",
        ],
    )
    def test_every_label_spelling_is_a_whole_word(self, counts):
        assert extract_card_area_m2(f"320.000 € {counts} 316 m²") == 316.0

    def test_a_year_is_not_a_room_count_or_an_area(self):
        assert extract_card_area_m2("Año 2019. 320.000 € 4 hab. 316 m²") == 316.0
        assert extract_card_area_m2("320.000 € 4 hab. Año 2019") is None
        assert extract_card_area_m2("320.000 € 2019 hab. 316 m²") is None


class TestWhatIsNeverTheArea:
    def test_the_unit_price_is_not_an_area(self):
        assert extract_card_area_m2("320.000 € 1.013 €/m²") is None
        assert extract_card_area_m2("320.000 € 1.013 €/m² 4 hab.") is None

    def test_the_built_figure_beside_the_rooms_wins_over_a_plot_figure(self):
        card = "320.000 € 4 hab. 316 m² parcela 1.200 m²"

        assert extract_card_blocks_m2(card) == [316.0]
        assert extract_card_area_m2(card) == 316.0

    def test_a_plot_listing_returns_its_only_figure(self):
        assert extract_card_area_m2("59.000 € 118 €/m² 1.200 m² de parcela") == 1200.0

    def test_a_blocks_missing_area_is_none_not_the_next_blocks(self):
        text = "320.000 € 4 hab. Chalet B 99.000 € 3 hab. 90 m²"

        assert extract_card_blocks_m2(text) == [None, 90.0]
        assert extract_card_area_m2(text) is None


class TestTheCardStartsAtItsOwnLink:
    SUMMARY = "Tu búsqueda: 350.000 € 150 m² "

    def test_nothing_before_the_listing_link_is_read(self):
        body = (
            f'{self.SUMMARY}<a href="https://www.idealista.com/inmueble/111/">'
            "Chalet</a> 320.000 € 4 hab. 316 m²"
        )

        assert extract_card_area_m2(body, idealista_property_id=111) == 316.0

    def test_a_summary_before_a_card_without_an_area_is_not_the_area(self):
        body = (
            f'{self.SUMMARY}<a href="https://www.idealista.com/inmueble/111/">'
            "Chalet</a> 320.000 € 4 hab."
        )

        assert extract_card_area_m2(body, idealista_property_id=111) is None

    def test_a_listing_whose_link_is_not_in_the_body_reads_nothing(self):
        body = f"{self.SUMMARY} 320.000 € 4 hab. 316 m²"

        assert extract_card_area_m2(body, idealista_property_id=111) is None
        assert extract_card_area_m2(body, idealista_property_id=999) is None


class TestThePipeline:
    def test_the_search_name_never_supplies_the_area(self, service):
        (entry,) = _ingest(service, _raw(SEARCH_SUBJECT, [(111, SPANISH_CARD)]))

        assert entry["area"] == 316.0
        assert entry["area_type"] == "built"

    def test_a_card_without_an_area_stores_none_and_unknown(self, service):
        (entry,) = _ingest(
            service, _raw(SEARCH_SUBJECT, [(111, "320.000 € 1.013 €/m² 4 hab.")])
        )

        assert entry["area"] is None
        assert entry["area_type"] == "unknown"

    def test_an_english_card_is_read(self, service):
        (entry,) = _ingest(service, _raw(SEARCH_SUBJECT, [(111, ENGLISH_CARD)]))

        assert entry["area"] == 150.0

    def test_a_two_listing_email_ingests_the_first_listing_with_its_own_area(
        self, service
    ):
        raw = _raw(
            SEARCH_SUBJECT,
            [(111, "320.000 € 1.013 €/m² 4 hab."), (222, "99.000 € 309 €/m² 320 m²")],
        )
        (entry,) = _ingest(service, raw)

        assert entry["idealista_property_id"] == 111
        assert entry["area"] is None

    def test_the_search_name_never_supplies_the_price(self, service):
        subject = "¡Nuevo chalet en tu búsqueda: Costa hasta 350.000 € - 150m2!"
        (entry,) = _ingest(service, _raw(subject, [(111, SPANISH_CARD)]))

        assert extract_price(subject) == 350000.0  # the poison is real
        assert entry["price"] == 320000.0


def test_the_legacy_parser_reads_the_body_for_the_area():
    parser = EmailParser()
    parsed = parser.parse_idealista_email(
        {
            "subject": "Nuevo terreno en tu búsqueda: Norte - 5000m2",
            "body": '<a href="https://www.idealista.com/inmueble/5/">Terreno</a>'
            "<p>59.000 € 118 €/m² 500 m²</p>",
        }
    )
    assert parsed["area"] == 500.0

    without = parser.parse_idealista_email(
        {
            "subject": "Nuevo terreno en tu búsqueda: Norte - 5000m2",
            "body": '<a href="https://www.idealista.com/inmueble/5/">Terreno</a>'
            "<p>59.000 €</p>",
        }
    )
    assert without["area"] is None


class TestSingleListingMail:
    @pytest.mark.parametrize(
        "text",
        [
            "Hola, Sergioalicante, 1 anuncio recién publicado con tus criterios",
            "Hello Sergioalicante, 1 new listing that matches your search criteria",
            "<p>Hola, X,</p><p>1 anuncio recién publicado</p>",
            "The price of this listing has dropped from 140,000€ to 120,000€",
        ],
    )
    def test_a_stated_single_listing(self, text):
        assert single_listing_mail(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Hola, X, 11 anuncios nuevos con tus criterios",
            "Hello X, 21 new listings that match your search criteria",
            "Hola, X, 2 anuncios",
            "Hello X, 1,1 new listing",
            "320.000 € 4 hab. 316 m²",
            "",
            None,
        ],
    )
    def test_anything_else_is_not(self, text):
        assert single_listing_mail(text) is False
