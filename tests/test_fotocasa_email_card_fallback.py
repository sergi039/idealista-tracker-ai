"""A fotocasa listing whose page is refused is stored from the email (#636).

The real alert (`tests/data/fotocasa_alert_arteixo.html`) runs through the real
ingest loop; only the IMAP server and the page fetch are replaced.
"""

import pathlib
import time
from email.message import EmailMessage

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from config import Config
from models import Property, SearchProfile, SyncHistory
from services import fotocasa_import, fotocasa_source
from services.fotocasa_source import (
    REFUSAL_BLOCKED,
    REFUSAL_NOT_A_LISTING,
    FotocasaListing,
    cards_in_email,
)
from services.property_imap_service import (
    PORTAL_MAX_CONSECUTIVE_REFUSALS,
    PropertyIMAPService,
)
from tests import setup_test_environment
from tests.test_fotocasa_email_ingestion import _FakeIMAPClient, _held, _service
from utils.uid_cursor import HeldUids, write_uid_file

DATA = pathlib.Path(__file__).parent / "data"
ALERT = (DATA / "fotocasa_alert_arteixo.html").read_text(encoding="utf-8")
ALERT_ID = 190485369
PAGE_ID = 190280914


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
    """The page fetch, with a switch: `blocked` refuses, `gone` answers "no
    longer served". Records every URL asked for."""
    state = {"mode": "blocked", "calls": []}

    def fake_fetch(url, session=None):
        state["calls"].append(url)
        if state["mode"] == "blocked":
            return FotocasaListing(url=url, refusal=REFUSAL_BLOCKED)
        if state["mode"] == "gone":
            return FotocasaListing(url=url, refusal=REFUSAL_NOT_A_LISTING)
        raise AssertionError(f"unexpected mode {state['mode']}")

    monkeypatch.setattr(fotocasa_source, "fetch_listing", fake_fetch)
    return state


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

    def test_a_strike_on_the_anchor_itself_is_a_previous_price(self):
        """The style can sit on the anchor, not inside it: the struck amount
        and the asking one are two anchors of one listing."""
        struck = (
            _card_html(190280904)
            .replace('<a href="', '<a style="text-decoration:line-through" href="', 2)
            .replace("295.000 €", "320.000 €", 1)
        )
        (card,) = cards_in_email(struck)
        assert card.price != 320000.0

    def test_a_hostile_body_is_read_in_linear_time(self):
        """Anchors opened and never closed must not make the read quadratic."""
        hostile = '<a href="x" ' * 8000 + _card_html(190280905)
        started = time.monotonic()
        cards = cards_in_email(hostile)
        assert time.monotonic() - started < 5
        assert [c.listing_id for c in cards] == [190280905]

    def test_markup_inside_the_chosen_anchor_is_read_in_linear_time(self):
        """The reviewer's input: a valid listing anchor with a hundred thousand
        `<` before its `</a>`. The tag stripper must not rescan the rest of the
        text from each of them."""
        hostile = _card_html(190280907).replace(
            "Ver anuncio", "Ver anuncio" + "<" * 100_000
        )
        started = time.monotonic()
        (card,) = cards_in_email(hostile)
        assert time.monotonic() - started < 2
        assert card.listing_id == 190280907 and card.price == 295000.0

    def test_a_huge_line_and_a_huge_link_are_bounded(self):
        """A run of digits is rescanned from every position by the number
        grammars and by the id pattern; lines and links have a length cap."""
        long_line = _card_html(190280908, facts="1" * 100_000 + " m²")
        started = time.monotonic()
        (card,) = cards_in_email(long_line)
        assert card.area is None, "a line that long is not the facts line"
        long_link = (
            '<a href="https://www.fotocasa.es/es/comprar/vivienda/x/'
            + "1" * 200_000
            + '/d">1 €</a>'
        )
        assert cards_in_email(long_link) == []
        assert (
            fotocasa_source.listing_id_from_url(
                "https://www.fotocasa.es/es/x/" + "1" * 100_000 + "/d"
            )
            is None
        )
        assert time.monotonic() - started < 2

    def test_a_surface_the_card_does_not_name_stays_unknown(self):
        """ "900 m²" on a /vivienda/ link may be the plot: no rooms, no claim."""
        (card,) = cards_in_email(_card_html(190280909, facts="900 m²"))
        assert card.area == 900.0 and card.area_type == "unknown"
        (card,) = cards_in_email(_card_html(190280910, facts="3 habs · 900 m²"))
        assert card.area_type == "built"

    def test_the_type_is_the_first_segment_the_municipality_the_last(self):
        (card,) = cards_in_email(
            _card_html(190280915, heading="<strong>terreno</strong>, Llaranes, Avilés")
        )
        assert card.building_type == "terreno"
        assert card.municipality == "Avilés"

    def test_one_email_cannot_mint_more_than_the_cap_of_cards(self):
        body = "".join(
            _card_html(190290000 + i)
            for i in range(fotocasa_source.MAX_CARDS_PER_EMAIL + 5)
        )
        assert len(cards_in_email(body)) == fotocasa_source.MAX_CARDS_PER_EMAIL

    def test_an_oversized_body_is_not_read_for_cards(self):
        body = _card_html(190280906) + " " * fotocasa_source.MAX_ALERT_BODY_CHARS
        assert cards_in_email(body) == []

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


class TestTheCardDoesNotGuess:
    def test_an_unnamed_surface_is_stored_unknown_not_built(
        self, app, uid_file, imap, portal
    ):
        body = _card_html(190280916, facts="900 m²")
        imap.payloads = {1: _email(body)}
        _service().run_ingestion(sync_type="test")
        (prop,) = Property.query.all()
        assert float(prop.area) == 900.0 and prop.area_type == "unknown"

    def test_a_link_that_names_neither_sale_nor_rent_waits(
        self, app, uid_file, imap, portal
    ):
        body = _card_html(190280917).replace("/comprar/", "/otra-cosa/")
        imap.payloads = {1: _email(body)}
        assert _service().run_ingestion(sync_type="test") == 0
        assert Property.query.count() == 0
        assert _held(uid_file) == [1]


class TestEmailsAlreadyWaitingStayWithTheOwner:
    def test_an_email_held_after_the_fallback_shipped_may_use_its_card(
        self, app, uid_file, imap, portal, monkeypatch
    ):
        """First held for an unrelated failure, retried with the flag off: it is
        not one of the emails waiting when the fallback shipped."""
        real = PropertyIMAPService._create_portal_row
        failures = []

        def fail_once(self, *args, **kwargs):
            if not failures:
                failures.append(1)
                raise RuntimeError("database hiccup")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(PropertyIMAPService, "_create_portal_row", fail_once)
        imap.payloads = {1: _email(ALERT)}
        _service().run_ingestion(sync_type="test")
        assert _held(uid_file) == [1] and Property.query.count() == 0
        entry = HeldUids(str(uid_file) + ".held.json").entry(1)
        assert entry["card_fallback_aware"] is True

        assert _service().run_ingestion(sync_type="test") == 1
        assert Property.query.count() == 1 and _held(uid_file) == []

    def test_the_stamp_survives_further_tries_and_an_old_entry_never_gets_one(
        self, tmp_path
    ):
        held = HeldUids(str(tmp_path / "held.json"))
        held.hold(1, "old rule")
        held.hold(2, "new rule", stamp={"card_fallback_aware": True})
        held.hold(1, "old rule again", stamp={"card_fallback_aware": True})
        held.hold(2, "new rule again")
        reread = HeldUids(str(tmp_path / "held.json"))
        assert "card_fallback_aware" not in reread.entry(1)
        assert reread.entry(2)["card_fallback_aware"] is True
        assert reread.entry(2)["attempts"] == 2
        assert reread.entry(3) is None

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


class TestMunicipalityOnlyWhenIneKnowsIt:
    def test_a_district_is_unknown_not_a_municipality(
        self, app, uid_file, imap, portal
    ):
        body = _card_html(190280950, heading="<strong>casa-chalet</strong>, Llaranes")
        imap.payloads = {1: _email(body)}
        _service().run_ingestion(sync_type="test")
        prop = Property.query.one()
        assert prop.municipality is None
        assert prop.title == "casa-chalet, Llaranes", "the heading is kept"

    def test_a_name_ine_knows_is_stored_as_written(self, app, uid_file, imap, portal):
        imap.payloads = {1: _email(ALERT)}
        _service().run_ingestion(sync_type="test")
        assert Property.query.one().municipality == "Arteixo"


class TestAnIntegrityErrorIsNotAlwaysADuplicate:
    @staticmethod
    def _fail_property_commits(monkeypatch, *, store_duplicate: bool):
        real_commit = db.session.commit

        def commit():
            fresh = [o for o in db.session.new if isinstance(o, Property)]
            if not fresh:
                return real_commit()
            key = fresh[0].source_email_id
            db.session.rollback()
            if store_duplicate:  # a concurrent writer got there first
                db.session.add(Property(source_email_id=key, title="the other writer"))
                real_commit()
            raise IntegrityError("INSERT", {}, Exception("constraint"))

        monkeypatch.setattr(db.session, "commit", commit)

    def test_another_constraint_holds_the_email_instead_of_losing_it(
        self, app, uid_file, imap, portal, monkeypatch
    ):
        imap.payloads = {1: _email(ALERT)}
        with monkeypatch.context() as patched:
            self._fail_property_commits(patched, store_duplicate=False)
            _service().run_ingestion(sync_type="test")
        assert Property.query.count() == 0
        assert _held(uid_file) == [1]

        assert _service().run_ingestion(sync_type="test") == 1
        assert Property.query.count() == 1 and _held(uid_file) == []

    def test_a_real_duplicate_is_still_consumed(
        self, app, uid_file, imap, portal, monkeypatch
    ):
        imap.payloads = {1: _email(ALERT)}
        with monkeypatch.context() as patched:
            self._fail_property_commits(patched, store_duplicate=True)
            service = _service()
            assert service.run_ingestion(sync_type="test") == 0
        assert Property.query.count() == 1
        assert _held(uid_file) == [] and service.last_seen_uid == 1
