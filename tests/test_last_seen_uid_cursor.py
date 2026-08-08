"""Regression tests for issue #24: last_seen_uid must never run ahead of the DB.

Both IMAP services used to persist `max(uids)` inside `get_idealista_emails()`,
before `run_ingestion()` had written anything. A failure between fetch and commit
therefore made those emails invisible forever.

The tests below drive the *real* fetch/parse/ingest path — only `IMAPClient`
(the network boundary) is faked — and cause a *real* database failure by letting
one email collide with the `properties.source_email_id` unique constraint. No DB
call is mocked, so the assertion is about what actually got committed.
"""

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from app import create_app, db
from config import Config
from models import Land, Property
import services.property_imap_service as property_imap_module
from services.imap_service import IMAPService
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment
from utils.uid_cursor import UidBatchCursor, read_uid_file, write_uid_file

INTERNAL_DATE = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def _raw_email(subject: str, url: str, body_html: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Idealista <noresponder@idealista.com>"
    msg.set_content("See the listing in your browser")
    msg.add_alternative(
        f'<html><body><a href="{url}">{subject}</a>{body_html}</body></html>',
        subtype="html",
    )
    return msg.as_bytes()


def _land_email(uid: int) -> bytes:
    url = f"https://www.idealista.com/inmueble/{100000 + uid}/"
    return _raw_email(
        "New plot of land in your search",
        url,
        f"<p>Terreno en Santa Eulalia del Río</p><p>{200 + uid}.000 €</p><p>1.500 m²</p>",
    )


class _FakeIMAPClient:
    """Stands in for the IMAP server only. Everything below it is the real code."""

    payloads: dict[int, bytes] = {}
    fetched_uids: list[int] = []

    def __init__(self, host, port=None, ssl=None):
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
        _FakeIMAPClient.fetched_uids = list(uids)
        return {
            uid: {
                b"RFC822": _FakeIMAPClient.payloads[uid],
                b"INTERNALDATE": INTERNAL_DATE,
            }
            for uid in uids
        }


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


@pytest.fixture
def uid_file(tmp_path, monkeypatch):
    """Point both cursor paths at a temp dir so no repo/data file is touched."""
    path = tmp_path / ".last_seen_uid_properties"
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PROPERTIES_PATH", str(path))
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PATH", str(tmp_path / ".last_seen_uid"))
    monkeypatch.setattr(Config, "BASE_DIR", str(tmp_path / "base"))
    return path


def _make_service(monkeypatch) -> PropertyIMAPService:
    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient", _FakeIMAPClient, raising=True
    )
    service = PropertyIMAPService()
    service.user = "owner@example.com"
    service.password = "dummy"
    service.host = "imap.example.com"
    service.folder = "Idealista"
    return service


def test_cursor_stops_at_the_email_whose_commit_failed(app, uid_file, monkeypatch):
    """UID 2 fails to commit for real, so the cursor may not pass UID 1."""
    Config.AUTO_TRAVEL_ENRICHMENT = False
    Config.AUTO_PROPERTY_SCORING = False

    _FakeIMAPClient.payloads = {uid: _land_email(uid) for uid in (1, 2, 3)}

    with app.app_context():
        # A pre-existing row owns source_email_id "imap_2" while pointing at an
        # unrelated listing, so ingesting UID 2 hits the real unique constraint
        # instead of being deduped away.
        blocker = Property()
        blocker.source_email_id = "imap_2"
        blocker.url = "https://www.idealista.com/inmueble/999999/"
        blocker.idealista_property_id = 999999
        db.session.add(blocker)
        db.session.commit()

        service = _make_service(monkeypatch)
        assert service.last_seen_uid == 0
        service.run_ingestion(sync_type="test")

        assert Property.query.filter_by(source_email_id="imap_1").one_or_none()
        assert Property.query.filter_by(source_email_id="imap_3").one_or_none()

        # The cursor must not step over UID 2: it was fetched but never stored.
        assert read_uid_file(str(uid_file)) == 1
        assert service.last_seen_uid == 1

        # Second run, after the collision is cleared: the lost emails come back.
        db.session.delete(blocker)
        db.session.commit()
        Property.query.filter_by(source_email_id="imap_3").delete()
        db.session.commit()

        second = _make_service(monkeypatch)
        assert second.last_seen_uid == 1
        second.run_ingestion(sync_type="test")

        assert _FakeIMAPClient.fetched_uids == [2, 3]
        assert Property.query.filter_by(source_email_id="imap_2").one_or_none()
        assert Property.query.filter_by(source_email_id="imap_3").one_or_none()
        assert read_uid_file(str(uid_file)) == 3


def test_cursor_advances_over_filtered_and_committed_emails(app, uid_file, monkeypatch):
    """A run with nothing to store still advances: skipped emails are finished."""
    Config.AUTO_TRAVEL_ENRICHMENT = False
    Config.AUTO_PROPERTY_SCORING = False

    _FakeIMAPClient.payloads = {
        1: _land_email(1),
        # Blacklisted subject: dropped during parsing, no DB work owed.
        2: _raw_email(
            "Welcome to Idealista", "https://www.idealista.com/", "<p>hi</p>"
        ),
        3: _land_email(3),
    }

    with app.app_context():
        service = _make_service(monkeypatch)
        service.run_ingestion(sync_type="test")

        assert read_uid_file(str(uid_file)) == 3
        assert Property.query.count() == 2


def test_unfetchable_uid_does_not_advance_the_cursor(app, uid_file, monkeypatch):
    """A UID the server returned no body for is a gap, not a skip."""
    Config.AUTO_TRAVEL_ENRICHMENT = False
    Config.AUTO_PROPERTY_SCORING = False

    _FakeIMAPClient.payloads = {1: _land_email(1), 2: _land_email(2)}

    class _GappyClient(_FakeIMAPClient):
        def fetch(self, uids, parts):
            data = super().fetch(uids, parts)
            data[1] = {b"INTERNALDATE": INTERNAL_DATE}  # body missing for UID 1
            return data

    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient", _GappyClient, raising=True
    )
    with app.app_context():
        service = PropertyIMAPService()
        service.user = "owner@example.com"
        service.password = "dummy"
        service.host = "imap.example.com"
        service.folder = "Idealista"
        service.run_ingestion(sync_type="test")

        assert Property.query.filter_by(source_email_id="imap_2").one_or_none()
        assert read_uid_file(str(uid_file)) is None
        assert service.last_seen_uid == 0


def test_legacy_land_cursor_stops_at_the_email_whose_commit_failed(
    app, uid_file, monkeypatch
):
    """Same guarantee for the legacy Land pipeline (services/imap_service.py)."""
    lands_uid_file = uid_file.parent / ".last_seen_uid"

    _FakeIMAPClient.payloads = {uid: _land_email(uid) for uid in (1, 2, 3)}
    monkeypatch.setattr("services.imap_service.IMAPClient", _FakeIMAPClient)
    # Enrichment and AI description are paid external services: stub them out so
    # the test only exercises ingestion. Neither is the call under test.
    monkeypatch.setattr(
        "services.enrichment_service.EnrichmentService.enrich_land",
        lambda self, land_id: True,
    )
    monkeypatch.setattr(
        "services.description_service.DescriptionService.enhance_description",
        lambda self, description, property_data: {"processing_status": "skipped"},
    )

    with app.app_context():
        blocker = Land()
        blocker.source_email_id = "imap_2"
        blocker.url = "https://www.idealista.com/inmueble/999999/"
        blocker.idealista_property_id = 999999
        db.session.add(blocker)
        db.session.commit()

        service = IMAPService()
        service.user = "owner@example.com"
        service.password = "dummy"
        service.host = "imap.example.com"
        service.folder = "Idealista"
        service.run_ingestion(sync_type="test")

        assert Land.query.filter_by(source_email_id="imap_1").one_or_none()
        assert Land.query.filter_by(source_email_id="imap_3").one_or_none()
        assert read_uid_file(str(lands_uid_file)) == 1
        assert service.last_seen_uid == 1


def test_corrupt_cursor_file_raises_instead_of_reprocessing_everything(
    app, uid_file, monkeypatch
):
    """Silently reading 0 would re-ingest the whole mailbox with no signal."""
    uid_file.write_text("not-a-uid", encoding="utf-8")

    with app.app_context():
        with pytest.raises(ValueError):
            PropertyIMAPService()

        with pytest.raises(ValueError):
            monkeypatch.setattr(Config, "LAST_SEEN_UID_PATH", str(uid_file))
            IMAPService()


def test_cursor_file_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "cursor"
    write_uid_file(str(path), 41)
    write_uid_file(str(path), 42)

    assert path.read_text(encoding="utf-8") == "42"
    assert [p.name for p in tmp_path.iterdir()] == ["cursor"]


def test_empty_cursor_file_reads_as_zero(tmp_path):
    path = tmp_path / "cursor"
    path.write_text("", encoding="utf-8")
    assert read_uid_file(str(path)) == 0
    assert read_uid_file(str(tmp_path / "absent")) is None


class TestUidBatchCursor:
    def test_watermark_stops_before_the_first_unresolved_uid(self):
        cursor = UidBatchCursor([10, 11, 12], start=9)
        assert cursor.resolve(10) is True
        assert cursor.watermark == 10

        # 11 stays unresolved: resolving 12 must not move the watermark past it.
        assert cursor.resolve(12) is False
        assert cursor.watermark == 10
        assert cursor.pending == {11}

        assert cursor.resolve(11) is True
        assert cursor.watermark == 12

    def test_unknown_and_missing_uids_are_ignored(self):
        cursor = UidBatchCursor([5], start=4)
        assert cursor.resolve(None) is False
        assert cursor.resolve("nope") is False
        assert cursor.resolve(99) is False
        assert cursor.watermark == 4


class TestParsingFailureHoldsTheCursor:
    """An email that raised is neither committed nor deliberately skipped.

    The first version of this fix resolved a UID in `finally` whenever nothing
    was emitted, which lumped "filtered out" together with "blew up". A
    transient parser failure - a decode error, a dependency hiccup, a bug hit by
    one odd email - therefore advanced the cursor past a real listing and lost
    it permanently: issue #24 again, one layer further in.

    Both services must instead hold the cursor behind the failing email so the
    next run re-reads it. A permanently broken email keeps the cursor parked and
    logs why, which is the trade #24 chose: visibly stuck beats silently lost.
    """

    def test_property_pipeline_holds_the_cursor_at_a_raising_email(
        self, app, uid_file, monkeypatch
    ):
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False

        _FakeIMAPClient.payloads = {uid: _land_email(uid) for uid in (1, 2, 3)}

        real_extract = property_imap_module.extract_idealista_property_id
        failing_url = f"https://www.idealista.com/inmueble/{100000 + 2}/"

        def explode_on_uid_2(url):
            if url == failing_url:
                raise ValueError("temporary parser failure")
            return real_extract(url)

        with app.app_context():
            monkeypatch.setattr(
                property_imap_module,
                "extract_idealista_property_id",
                explode_on_uid_2,
            )

            service = _make_service(monkeypatch)
            service.run_ingestion(sync_type="test")

            assert Property.query.filter_by(source_email_id="imap_1").one_or_none()
            assert Property.query.filter_by(source_email_id="imap_2").one_or_none() is None

            # UID 2 raised, so the cursor may not pass UID 1 - not 2, and not 3.
            assert read_uid_file(str(uid_file)) == 1
            assert service.last_seen_uid == 1

        # Once the transient failure is gone the email comes back and lands.
        with app.app_context():
            monkeypatch.setattr(
                property_imap_module,
                "extract_idealista_property_id",
                real_extract,
            )
            _FakeIMAPClient.fetched_uids = []

            second = _make_service(monkeypatch)
            assert second.last_seen_uid == 1
            second.run_ingestion(sync_type="test")

            assert _FakeIMAPClient.fetched_uids == [2, 3]
            assert Property.query.filter_by(source_email_id="imap_2").one_or_none()
            assert read_uid_file(str(uid_file)) == 3

    def test_legacy_pipeline_holds_the_cursor_at_a_raising_email(
        self, app, uid_file, monkeypatch
    ):
        Config.AUTO_TRAVEL_ENRICHMENT = False

        _FakeIMAPClient.payloads = {uid: _land_email(uid) for uid in (1, 2, 3)}

        monkeypatch.setattr(
            "services.imap_service.IMAPClient", _FakeIMAPClient, raising=True
        )
        service = IMAPService()
        service.user = "owner@example.com"
        service.password = "dummy"
        service.host = "imap.example.com"
        service.folder = "Idealista"

        real_parse = service.email_parser.parse_idealista_email
        seen = {"count": 0}

        def explode_on_second_email(email_content):
            seen["count"] += 1
            if seen["count"] == 2:
                raise ValueError("temporary parser failure")
            return real_parse(email_content)

        with app.app_context():
            monkeypatch.setattr(
                service.email_parser,
                "parse_idealista_email",
                explode_on_second_email,
            )

            service.run_ingestion(sync_type="test")

            assert Land.query.filter_by(source_email_id="imap_1").one_or_none()
            assert Land.query.filter_by(source_email_id="imap_2").one_or_none() is None
            assert read_uid_file(str(Config.LAST_SEEN_UID_PATH)) == 1
