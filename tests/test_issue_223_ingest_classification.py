"""Issue #223: ingestion classified the raw title, address and all.

PR #139 stopped an address deciding the property type — but only inside
`classify_property()`, the *manual* reclassify path. `PropertyIMAPService` ran
its own sequence over the full listing title, so a plot in "Caserio **Casa** de
Anes" was stored as housing while the reclassify tool called it land. Casa,
Chalet, Villa and Piso are ordinary Asturian street and hamlet names.

The category is not a label: the scorer weights criteria per category and
`PropertyAIService` prompts a different JSON schema per category, so a
misclassified plot is a paid analysis of the wrong question.

The end-to-end tests drive the real fetch/parse/ingest path with only
`IMAPClient` (the network boundary) faked, and assert on the row that landed.
"""

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from models import Property
from services.property_classification_service import PropertyClassificationService
from services.property_imap_service import PropertyIMAPService
from services.settings_service import DEFAULT_PROPERTY_CLASSIFICATION_RULES
from tests import setup_test_environment

INTERNAL_DATE = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)

# A land title whose address carries a housing word, and its honest opposite.
LAND_WITH_HOUSE_ADDRESS = (
    "Land plot in Caserio Casa de Anes, 267, Parroquias Norte, Siero"
)
REAL_HOUSE = "Detached house in Barrio de Prendonés, 1, El Franco"


@pytest.fixture
def rules():
    return sorted(
        (dict(rule) for rule in DEFAULT_PROPERTY_CLASSIFICATION_RULES),
        key=lambda rule: int(rule.get("priority", 0)),
        reverse=True,
    )


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


class TestTheOrderIsWrittenOnce:
    @pytest.mark.parametrize(
        "title",
        [
            LAND_WITH_HOUSE_ADDRESS,
            "Terreno en Casa Blanca, 12, Villaviciosa",
            "Land in Chalet de Arriba, 3, Siero",
            "Plot of land in Villa Rosario, 8, Llanes",
        ],
    )
    def test_an_address_cannot_claim_the_type(self, title, rules):
        category, _ = PropertyClassificationService.classify_sources(
            title, subject="New listing in your search", body="", rules=rules
        )

        assert category == "land"

    def test_a_house_is_still_a_house(self, rules):
        category, subtype = PropertyClassificationService.classify_sources(
            REAL_HOUSE, subject="", body="", rules=rules
        )

        assert (category, subtype) == ("housing", "house")

    def test_a_title_that_says_nothing_falls_through_to_subject_then_body(self, rules):
        """The head only ever narrows: the later sources must still be read."""
        from_subject, _ = PropertyClassificationService.classify_sources(
            "Reference 55-A", subject="Terreno en venta", body="", rules=rules
        )
        from_body, _ = PropertyClassificationService.classify_sources(
            "Reference 55-A", subject="", body="Parcela de 1.200 m2", rules=rules
        )

        assert from_subject == "land"
        assert from_body == "land"

    def test_the_manual_path_delegates_to_the_same_order(self, app, rules):
        """`classify_property` and ingestion must not drift apart again."""
        prop = Property(
            source_email_id="issue-223-manual",
            title=LAND_WITH_HOUSE_ADDRESS,
        )
        db.session.add(prop)
        db.session.commit()

        assert PropertyClassificationService.classify_property(prop)[0] == "land"


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


def _raw_email(title: str, url: str, body_html: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "1 new listing that matches your search criteria"
    msg["From"] = "Idealista <noresponder@idealista.com>"
    msg.set_content("See it in your browser")
    msg.add_alternative(
        f'<html><body><a href="{url}">{title}</a>{body_html}</body></html>',
        subtype="html",
    )
    return msg.as_bytes()


def _ingest(app, monkeypatch):
    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient", _FakeIMAPClient, raising=True
    )
    service = PropertyIMAPService()
    service.user = "owner@example.com"
    service.password = "dummy"
    service.host = "imap.example.com"
    service.folder = "Idealista"
    return service.run_ingestion(sync_type="test")


class TestIngestionStoresTheTypeTheTitleStates:
    def test_a_plot_at_a_house_shaped_address_is_ingested_as_land(
        self, app, uid_file, monkeypatch
    ):
        _FakeIMAPClient.payloads = {
            1: _raw_email(
                LAND_WITH_HOUSE_ADDRESS,
                "https://www.idealista.com/inmueble/990223/",
                "<p>70.000 €</p><p>1.200 m²</p>",
            )
        }

        with app.app_context():
            assert _ingest(app, monkeypatch) == 1
            prop = Property.query.one()

            assert prop.title == LAND_WITH_HOUSE_ADDRESS
            assert prop.property_category == "land", (
                "the address decided the type: this is the #139 defect, in the "
                "path that actually creates rows"
            )

    def test_a_real_house_is_still_ingested_as_housing(
        self, app, uid_file, monkeypatch
    ):
        _FakeIMAPClient.payloads = {
            2: _raw_email(
                REAL_HOUSE,
                "https://www.idealista.com/inmueble/990224/",
                "<p>99.000 €</p><p>320 m²</p>",
            )
        }

        with app.app_context():
            assert _ingest(app, monkeypatch) == 1
            prop = Property.query.one()

            assert prop.property_category == "housing"
