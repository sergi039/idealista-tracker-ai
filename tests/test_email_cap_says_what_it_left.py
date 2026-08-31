"""`MAX_EMAILS_PER_RUN` truncates in silence, and a capped run must say so.

Two facts, and only the second is a defect.

**Oldest first is correct.** `UidBatchCursor.watermark` advances only through
*contiguous* resolved UIDs from the start of the batch, so taking the newest N
would leave the older ones unresolved forever and the cursor would never move
at all. Ascending is the only order that drains. This is asserted here so the
next reader does not "fix" it -- the audit that surfaced this described the
oldest-first choice as the problem, and it is not.

**The silence is the defect.** `sync_history.total_emails_found` counts the
emails *taken*, so a capped run wrote a row indistinguishable from one that had
read the whole mailbox, and nothing anywhere named the backlog. That is #98 in
the ingest: an absence of measurement drawn as a measurement.
"""

import pytest

from app import create_app, db
from config import Config
from models import SearchProfile, SyncHistory
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        db.session.add(
            SearchProfile(
                name="Default",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
        )
        db.session.commit()
        yield app
        db.drop_all()


class _FakeClient:
    """An IMAP server holding `uids`, recording what was actually fetched."""

    def __init__(self, uids):
        self.uids = list(uids)
        self.fetched = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, *a, **k):
        return None

    def logout(self):
        return None

    def select_folder(self, *a, **k):
        return None

    def search(self, *a, **k):
        return list(self.uids)

    def fetch(self, uids, fields):
        self.fetched = list(uids)
        # Every message is unrecognisable, so nothing is ingested and the test
        # is about the batch arithmetic rather than about parsing.
        return {uid: {b"RFC822": b"Subject: nothing\r\n\r\nno links"} for uid in uids}


def _service(monkeypatch, client, cap):
    monkeypatch.setattr(Config, "MAX_EMAILS_PER_RUN", cap)
    monkeypatch.setattr(Config, "IMAP_USER", "someone@example.com")
    monkeypatch.setattr(Config, "IMAP_PASSWORD", "secret")
    service = PropertyIMAPService()
    service.last_seen_uid = 0
    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient", lambda *a, **k: client
    )
    return service


def test_the_cap_takes_the_oldest_because_the_cursor_can_only_drain_forward(
    app, monkeypatch
):
    client = _FakeClient([50, 10, 30, 20, 40])
    service = _service(monkeypatch, client, cap=3)

    service.get_idealista_emails()

    assert client.fetched == [10, 20, 30], (
        "the batch must be the oldest UIDs in ascending order, or "
        "UidBatchCursor.watermark can never advance past them"
    )


def test_a_capped_run_records_what_it_left(app, monkeypatch):
    client = _FakeClient(list(range(1, 11)))
    service = _service(monkeypatch, client, cap=4)

    service.run_ingestion()

    row = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
    assert row.status == "partial"
    assert "4 read" in row.error_message
    assert "6 left" in row.error_message
    assert "10 matched" in row.error_message
    assert "UIDs 5..10" in row.error_message


def test_an_uncapped_run_says_completed_and_claims_no_backlog(app, monkeypatch):
    client = _FakeClient([1, 2, 3])
    service = _service(monkeypatch, client, cap=200)

    service.run_ingestion()

    row = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
    assert row.status == "completed"
    assert row.error_message is None


def test_a_run_exactly_at_the_cap_is_not_reported_as_truncated(app, monkeypatch):
    """The boundary: 4 of 4 read nothing short, so nothing is owed."""
    client = _FakeClient([1, 2, 3, 4])
    service = _service(monkeypatch, client, cap=4)

    service.run_ingestion()

    row = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
    assert row.status == "completed"
    assert row.error_message is None


def test_a_backlog_from_an_earlier_run_is_not_reported_by_a_clean_one(app, monkeypatch):
    """The state is per run: a stale value would invent a backlog."""
    service = _service(monkeypatch, _FakeClient(list(range(1, 11))), cap=4)
    service.run_ingestion()
    assert service._truncation is not None

    # Same service instance, a mailbox that now fits.
    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient",
        lambda *a, **k: _FakeClient([98, 99]),
    )
    service.last_seen_uid = 0
    service.run_ingestion()

    row = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
    assert row.status == "completed"
    assert row.error_message is None


def test_a_run_that_could_not_read_the_mailbox_says_failed(app, monkeypatch):
    """A real failure outranks the cap's disclosure -- and used to outrank nothing.

    `get_idealista_emails` catches every exception so the emails it parsed
    before the failure are still ingested, which is right. But the failure then
    reached nothing: `run_ingestion` took the success branch and wrote
    `completed` with `total_emails_found = 0`, which in the one table recording
    these runs is indistinguishable from "no new mail". A login failure, a dead
    connection and an empty mailbox all looked the same.
    """

    class _Exploding(_FakeClient):
        def fetch(self, uids, fields):
            raise RuntimeError("the server hung up")

    service = _service(monkeypatch, _Exploding(list(range(1, 11))), cap=4)

    service.run_ingestion()

    row = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
    assert row.status == "failed"
    assert "did not complete" in row.error_message
    assert "RuntimeError" in row.error_message
