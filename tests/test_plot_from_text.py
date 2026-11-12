"""#634: a plot the listing text states is read, with its source, at ingestion.

Texts are taken from stored descriptions (milanuncios, fotocasa) and from the
shapes the issue measured; the idealista e-mails are reconstructed cards, not
captured mail. Nothing here touches the network.
"""

from email.message import EmailMessage

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import plot_text, subscription_criteria as sc
from services.plot_text import EXTRACTOR_VERSION, apply_to_property, extract_plot_m2
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment
from tests.test_last_seen_uid_cursor import _FakeIMAPClient, _make_service

CRITERIA = {"min_house_m2": 150, "min_plot_m2": 700, "max_price_eur": 300000}


def _value(text):
    reading = extract_plot_m2(text)
    return None if reading is None else reading.value


class TestTheReading:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("parcela de 1.200 m²", 1200),
            ("terreno 800m2", 800),
            ("finca de 2 ha", 20000),
            ("1,5 ha de terreno", 15000),
            ("terreno de 1,5 ha", 15000),
            ("asentada sobre una parcela de 745 m²", 745),
            ("superficie de parcela: 900 metros cuadrados", 900),
            ("parcela de 745 m2", 745),
            ("Parcela de 1,200 m2", 1200),
            ("* Finca: 1.362 m² * Superficie construida: 266 m² totales", 1362),
            ("solar de 500 m²", 500),
            ("<p>Casa con <b>parcela</b> de 2.000&nbsp;m²</p>", 2000),
        ],
    )
    def test_a_stated_plot(self, text, expected):
        assert _value(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "casa de 200 m² construidos",
            "vivienda de 150 m² útiles en parcela",
            "parcela de 745 m² construidos",
            "casa de 184 m² de vivienda",
            "casa de piedra con terreno en Ponte do Porto",
            "placas solares de 5 m2 y orientación solar",
            "parcela de 0 m²",
            "parcela de 999999999 m²",
            "parcela de 700 m2 y finca de 900 m2",
            "",
            None,
        ],
    )
    def test_anything_else_reads_nothing(self, text):
        assert _value(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "170 m2 construidos sobre una parcela de 6...",
            "170 m2 construidos sobre una parcela de 6",
            "casa sobre una parcela de 6.0",
            "casa sobre una parcela de 6 m",
        ],
    )
    def test_a_truncated_figure_is_not_read(self, text):
        assert _value(text) is None

    def test_garbage_never_raises(self):
        assert extract_plot_m2(b"parcela") is None
        assert extract_plot_m2("parcela de " + "9" * 5000 + " m2") is None


@pytest.fixture
def app(tmp_path, monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    import utils.inflight as inflight_module

    monkeypatch.setattr(inflight_module, "INFLIGHT_DIRNAME", str(tmp_path / "inflight"))
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Galicia",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
            criteria=CRITERIA,
        )
        db.session.add(profile)
        db.session.commit()
        app.config["DEFAULT_PROFILE_ID"] = profile.id
        yield app
        db.drop_all()


def _row(n=1, description="", **extra):
    row = {
        "listing_id": n,
        "url": f"https://www.fotocasa.es/es/comprar/vivienda/x/{n}/d",
        "title": "Casa en Boiro",
        "description": description,
        "price": 250000,
        "area": 200,
        "area_type": "built",
        "plot_area": None,
        "deal_type": "sale",
        "municipality": "Boiro",
    }
    row.update(extra)
    return row


def _create(app, row, source="fotocasa"):
    service = PropertyIMAPService()
    service._enrich_new_property = lambda prop: None
    landed = service._create_portal_row(
        row,
        source=source,
        email_data={"search_profile_id": app.config["DEFAULT_PROFILE_ID"]},
        sale_only=False,
        excluded_categories=set(),
    )
    assert landed
    return Property.query.one()


class TestTheHouseLabelWhateverTheSeparator:
    """Review of #661: a house label after the figure refused it only when
    whitespace stood between them; "(superficie construida)" read as a plot."""

    @pytest.mark.parametrize(
        "text",
        [
            "Finca: 1.362 m² (superficie construida), sin terreno",
            "Finca: 1.362 m² (superficie construida)",
            "Finca: 1.362 m² (construidos)",
            "Finca de 1.362 m² - construidos",
            "Finca de 1.362 m² – edificados",
            "Finca: 1.362 m²: útiles",
            "Finca 1.362 m², edificados",
            "Finca 1.362 m², construcción",
            "Finca de 1.362 m² (vivienda)",
            "Finca de 1.362 m² [casa]",
            "Finca de 300 m² / construidos",
            "Parcela de 1.362 m² (de vivienda)",
            "Terreno: 900 m² — habitables",
        ],
    )
    def test_a_house_label_after_any_separator_refuses(self, text):
        assert _value(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "Vivienda: 300 m²",
            "Casa - 300 m²",
            "Finca construida: 300 m²",
            "Finca (construidos): 300 m²",
            "Superficie construida: 1.362 m²",
        ],
    )
    def test_a_house_label_before_the_figure_is_not_a_plot(self, text):
        assert _value(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "Parcela de 1.362 m², sin parcela",
            "Casa sin terreno. Finca de 300 m²",
            "Finca de 2.000 m2. Sin ningún terreno anexo",
            "Piso sin parcela; terreno comunitario de 500 m²",
        ],
    )
    def test_a_text_that_denies_a_plot_states_none(self, text):
        assert _value(text) is None

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("parcela de 1.362 m²", 1362),
            ("finca de 2.000 m2", 2000),
            ("finca de 2.000 m2, casa de 150 m2", 2000),
            ("parcela de 2.000 m², vivienda de 150 m²", 2000),
            ("parcela de 2.000 m² (aprox.)", 2000),
            ("2.000 m² de terreno (urbanizable)", 2000),
            ("Finca: 1.362 m². Superficie construida: 266 m²", 1362),
        ],
    )
    def test_a_real_plot_still_reads(self, text, expected):
        assert _value(text) == expected

    def test_a_denying_source_vetoes_one_that_states_a_plot(self):
        assert (
            plot_text.read_sources(
                [("title", "Parcela de 1.362 m²"), ("description", "sin terreno")]
            )
            is None
        )


class TestEveryPlotMentionCarriesTheFigure:
    """Third review of #661: a list of negations ("sin ningún") missed
    "sin ninguna"; the rule is now structural -- every plot noun in the text
    must be the one that introduced the figure, or nothing is read."""

    @pytest.mark.parametrize(
        "text",
        [
            "Casa sin ninguna parcela. Finca de 300 m²",
            "No tiene parcela. Finca de 300 m²",
            "Carece de terreno. Finca de 300 m²",
            "Sin jardín ni parcela. Finca de 300 m²",
            "sin ninguna clase de terreno. finca de 300 m2",
            "Casa sin parcelas. Finca de 300 m²",
        ],
    )
    def test_any_negation_reads_nothing(self, text):
        assert _value(text) is None

    def test_the_price_is_recall_a_plot_described_apart_is_unknown(self):
        assert _value("parcela llana y vallada. Finca de 900 m²") is None

    @pytest.mark.parametrize(
        "text",
        [
            "Sin terrenito. Finca de 900 m²",
            "Sin parcelita. Finca de 900 m²",
            "Dos fincas. Finca de 900 m²",
            "Sen leira. Finca de 900 m²",
            "Con eido. Finca de 900 m²",
            "Sen terreo. Finca de 900 m²",
            "Sense terreny. Finca de 900 m²",
            "Sense parcel·la. Finca de 900 m²",
            "Urbanización La Finca, parcela de 900 m²",
            "Solar urbano. Finca de 900 m²",
            "Casa de 300 m² en parcela",
            "parcela de 300 m² ... finca de 900 m²",
        ],
    )
    def test_another_plot_word_or_figure_refuses(self, text):
        assert _value(text) is None

    @pytest.mark.parametrize(
        "text, expected",
        [
            # A garden or an orchard is part of a plot, not a statement of one.
            ("Casa con jardín. Finca de 900 m²", 900),
            ("Casa con huerta. Finca de 900 m²", 900),
            ("con placas solares. parcela de 900 m²", 900),
            ("energía solar, parcela de 900 m²", 900),
            ("PARCELA DE 900 M2", 900),
            ("parcela\nde 900 m²", 900),
            ("parcela de 900 metros cuadrados", 900),
            ("sobre una parcela de 745 m² de terreno", 745),
            ("Casa de 300 m² en parcela de 300 m²", 300),
        ],
    )
    def test_a_plot_the_figure_belongs_to_still_reads(self, text, expected):
        assert _value(text) == expected

    def test_a_source_naming_a_plot_without_a_figure_vetoes_the_others(self):
        assert (
            plot_text.read_sources(
                [
                    ("title", "Finca de 900 m²"),
                    ("description", "parcela llana y vallada"),
                ]
            )
            is None
        )

    def test_a_refused_figure_still_counts_as_a_mention(self):
        assert plot_text.mentions_plot("No tiene parcela. Finca de 300 m²")
        assert not plot_text.mentions_plot("casa con terreno")


def _sql_ids(expression):
    return {p.id for p in Property.query.filter(expression(Property, CRITERIA)).all()}


class TestPortalIngestion:
    def test_a_described_plot_reaches_both_verdicts(self, app):
        prop = _create(
            app, _row(description="Casa asentada sobre una parcela de 745 m² ...")
        )

        assert float(prop.plot_area) == 745
        block = prop.enrichment["plot_from_text"]
        assert block["value"] == 745
        assert block["field"] == "description"
        assert "parcela de 745" in block["snippet"]
        assert block["extractor_version"] == EXTRACTOR_VERSION
        assert block["portal"] == "fotocasa"
        assert block["extracted_at"].endswith("+00:00")
        # Python and SQL read the same figure: pass in both.
        assert sc.read_verdict(prop, CRITERIA)["state"] == "pass"
        assert _sql_ids(sc.passing_expression) == {prop.id}
        assert _sql_ids(sc.failing_expression) == set()

    def test_a_house_figure_in_brackets_is_not_the_plot(self, app):
        # Review of #661: this exact description was stored as plot_area=1362.
        prop = _create(
            app,
            _row(description="Finca: 1.362 m² (superficie construida), sin terreno"),
        )

        assert prop.plot_area is None
        assert "plot_from_text" not in (prop.enrichment or {})
        assert sc.read_verdict(prop, CRITERIA)["state"] == "unknown"
        assert _sql_ids(sc.passing_expression) == set()
        assert _sql_ids(sc.failing_expression) == set()

    def test_a_negated_plot_is_not_read_from_a_later_sentence(self, app):
        # Third review of #661: "ninguna" was not in the negation list.
        prop = _create(
            app, _row(description="Casa sin ninguna parcela. Finca de 300 m²")
        )

        assert prop.plot_area is None
        assert "plot_from_text" not in (prop.enrichment or {})
        assert sc.read_verdict(prop, CRITERIA)["state"] == "unknown"
        assert _sql_ids(sc.failing_expression) == set()

    def test_no_text_figure_stays_unknown_in_both(self, app):
        prop = _create(app, _row(description="casa de piedra con terreno en Boiro"))

        assert prop.plot_area is None
        assert "plot_from_text" not in (prop.enrichment or {})
        assert sc.read_verdict(prop, CRITERIA)["state"] == "unknown"
        assert _sql_ids(sc.passing_expression) == set()
        assert _sql_ids(sc.failing_expression) == set()

    def test_a_truncated_figure_is_unknown_not_fail(self, app):
        prop = _create(
            app, _row(description="170 m2 construidos sobre una parcela de 6...")
        )

        assert prop.plot_area is None
        assert sc.read_verdict(prop, CRITERIA)["state"] == "unknown"
        assert _sql_ids(sc.failing_expression) == set()

    def test_the_house_figure_is_not_the_plot(self, app):
        prop = _create(app, _row(description="Superficie construida: 210 m² totales"))

        assert prop.plot_area is None

    def test_a_stated_plot_is_never_overwritten(self, app):
        prop = _create(
            app,
            _row(plot_area=1000, description="sobre una parcela de 745 m² de terreno"),
        )

        assert float(prop.plot_area) == 1000
        assert "plot_from_text" not in (prop.enrichment or {})

    def test_a_row_without_text_is_untouched(self, app):
        prop = _create(app, _row(description=None), source="yaencontre")

        assert prop.plot_area is None


def _raw(listings):
    cards = "".join(
        f'<a href="https://www.idealista.com/inmueble/{pid}/">Chalet {pid}</a>'
        f"<p>{card}</p>"
        for pid, card in listings
    )
    msg = EmailMessage()
    msg["Subject"] = "¡Nuevo chalet en tu búsqueda: Costa - 350k - 150m2!"
    msg["From"] = "Idealista <noresponder@idealista.com>"
    msg.set_content("See the listing in your browser")
    msg.add_alternative(f"<html><body>{cards}</body></html>", subtype="html")
    return msg.as_bytes()


class TestIdealistaIngestion:
    @pytest.fixture
    def service(self, app, tmp_path, monkeypatch):
        from config import Config

        monkeypatch.setattr(
            Config, "LAST_SEEN_UID_PROPERTIES_PATH", str(tmp_path / ".uid_props")
        )
        monkeypatch.setattr(Config, "LAST_SEEN_UID_PATH", str(tmp_path / ".uid"))
        monkeypatch.setattr(Config, "BASE_DIR", str(tmp_path / "base"))
        service = _make_service(monkeypatch)
        service._enrich_new_property = lambda prop: None
        return service

    def _ingest(self, service, listings):
        _FakeIMAPClient.payloads = {1: _raw(listings)}
        service.run_ingestion(sync_type="test")
        return {p.idealista_property_id: p for p in Property.query.all()}

    def test_the_card_plot_is_read_with_its_source(self, service):
        rows = self._ingest(
            service,
            [(111, "250.000 € 4 hab. 316 m² Casa sobre una parcela de 2.000 m²")],
        )

        assert float(rows[111].plot_area) == 2000
        block = rows[111].enrichment["plot_from_text"]
        assert block["field"] == "card"
        assert block["portal"] == "idealista"

    def test_a_truncated_card_reads_nothing(self, service):
        rows = self._ingest(
            service,
            [
                (
                    111,
                    "250.000 € 4 hab. 316 m² 170 m2 construidos sobre una parcela de 6...",
                )
            ],
        )

        assert rows[111].plot_area is None

    def test_a_neighbouring_card_is_not_this_listings(self, service):
        rows = self._ingest(
            service,
            [
                (111, "250.000 € 4 hab. 316 m² Casa de piedra"),
                (222, "99.000 € 3 hab. 90 m² Casa en parcela de 2.000 m²"),
            ],
        )

        assert rows[111].plot_area is None

    def test_the_listing_area_is_never_a_plot(self, service):
        rows = self._ingest(service, [(111, "250.000 € 4 hab. 316 m² Casa de piedra")])

        assert rows[111].plot_area is None
        assert float(rows[111].area) == 316

    def test_the_saved_search_subject_is_never_read_as_the_plot(self, service):
        msg = EmailMessage()
        msg["Subject"] = "¡Nuevo chalet en tu búsqueda: Galicia - parcela de 1.000 m²!"
        msg["From"] = "Idealista <noresponder@idealista.com>"
        msg.set_content("See the listing in your browser")
        msg.add_alternative(
            "<html><body>"
            '<a href="https://www.idealista.com/inmueble/111/">Ver 3 fotos</a>'
            "<p>250.000 € 4 hab. 316 m² Casa de piedra</p>"
            "</body></html>",
            subtype="html",
        )
        _FakeIMAPClient.payloads = {1: msg.as_bytes()}
        service.run_ingestion(sync_type="test")
        row = Property.query.filter_by(idealista_property_id=111).one()

        assert row.plot_area is None
        assert "plot_from_text" not in (row.enrichment or {})


class TestApplyToProperty:
    def test_bare_land_whose_area_is_the_parcel_is_left_alone(self, app):
        prop = Property(
            source_email_id="x",
            area=5000,
            area_type="plot",
            price=1,
            description="parcela de 745 m²",
        )

        assert (
            apply_to_property(prop, [("description", prop.description)], portal="x")
            is None
        )
        assert prop.plot_area is None

    def test_two_sources_the_first_with_a_reading_wins(self, app):
        prop = Property(source_email_id="x", area=200, area_type="built", price=1)

        block = apply_to_property(
            prop,
            [("description", "sin datos"), ("title", "Casa con finca de 2 ha")],
            portal="x",
        )

        assert block["field"] == "title"
        assert float(prop.plot_area) == 20000

    def test_a_self_contradicting_source_blocks_an_agreeing_title(self, app):
        prop = Property(source_email_id="x", area=200, area_type="built", price=1)

        block = apply_to_property(
            prop,
            [
                ("description", "parcela de 700 m² y finca de 900 m²"),
                ("title", "Casa en parcela de 700 m²"),
            ],
            portal="x",
        )

        assert block is None
        assert prop.plot_area is None

    def test_sources_that_disagree_write_nothing(self, app):
        prop = Property(source_email_id="x", area=200, area_type="built", price=1)

        block = apply_to_property(
            prop,
            [
                ("description", "parcela de 500 m²"),
                ("title", "Casa con finca de 900 m²"),
            ],
            portal="x",
        )

        assert block is None
        assert prop.plot_area is None

    def test_sources_that_agree_store_the_first_field(self, app):
        prop = Property(source_email_id="x", area=200, area_type="built", price=1)

        block = apply_to_property(
            prop,
            [("description", "parcela de 745 m²"), ("title", "parcela de 745 m²")],
            portal="x",
        )

        assert block["field"] == "description"
        assert float(prop.plot_area) == 745
