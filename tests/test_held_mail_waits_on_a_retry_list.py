"""An email that cannot be stored waits on a retry list, not in front of the cursor.

Measured on production 2026-11-10: fotocasa answered "blocked" for the page of
UID 494344, the cursor stayed at 494342, and every run re-read the same 200
oldest emails while 273 newer ones were never fetched. The email must not be
lost (#24), so it moves to `<cursor file>.held.json`; the cursor passes it.

The end-to-end behaviour is pinned where the portal fakes live
(tests/test_fotocasa_email_ingestion.py, tests/test_last_seen_uid_cursor.py).
Here: the list itself, the bound on retries, and the run's history line.
"""

import json

import pytest

from app import create_app, db
from config import Config
from models import SyncHistory
from services import property_imap_service as module
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment
from utils.uid_cursor import HeldUids


@pytest.fixture
def held_path(tmp_path):
    return str(tmp_path / ".last_seen_uid_properties.held.json")


class TestTheList:
    def test_a_missing_file_is_an_empty_list(self, held_path):
        assert HeldUids(held_path).uids() == []

    def test_an_unreadable_file_raises_instead_of_forgetting(self, held_path):
        with open(held_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with pytest.raises(ValueError):
            HeldUids(held_path)

    def test_a_hold_survives_a_reload_and_counts_its_attempts(self, held_path):
        held = HeldUids(held_path)
        held.hold(7, "blocked")
        held.hold(7, "blocked again")

        reloaded = HeldUids(held_path)
        assert reloaded.uids() == [7]
        with open(held_path, encoding="utf-8") as f:
            entry = json.load(f)["7"]
        assert entry["attempts"] == 2
        assert entry["reason"] == "blocked again"

    def test_a_release_survives_a_reload(self, held_path):
        held = HeldUids(held_path)
        held.hold(7, "blocked")
        held.hold(9, "blocked")
        held.release([7])
        assert HeldUids(held_path).uids() == [9]

    def test_the_least_recently_tried_are_due_first(self, held_path):
        held = HeldUids(held_path)
        for uid in (1, 2, 3):
            held.hold(uid, "blocked")
        held.hold(1, "blocked")  # 1 was tried again just now

        assert held.due(2) == [2, 3]
        assert held.due(0) == []

    def test_a_failed_write_changes_nothing_in_memory(self, held_path, monkeypatch):
        held = HeldUids(held_path)
        held.hold(7, "blocked")

        def refuse(path, text):
            raise OSError("disk full")

        monkeypatch.setattr("utils.uid_cursor._write_atomically", refuse)
        with pytest.raises(OSError):
            held.hold(8, "blocked")
        with pytest.raises(OSError):
            held.release([7])
        assert held.uids() == [7]


@pytest.fixture
def app(tmp_path, monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    monkeypatch.setattr(
        Config, "LAST_SEEN_UID_PROPERTIES_PATH", str(tmp_path / ".last_seen_uid")
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.rollback()
        db.drop_all()


def _run_with_held(monkeypatch, uids):
    """One run that fetched nothing, with ``uids`` already on the list."""
    service = PropertyIMAPService()
    held = HeldUids(service._held_file_path())
    for uid in uids:
        held.hold(uid, "blocked")

    def no_mail():
        service._held = HeldUids(service._held_file_path())
        return []

    monkeypatch.setattr(service, "get_idealista_emails", no_mail)
    service.run_ingestion(sync_type="test")
    return SyncHistory.query.order_by(SyncHistory.id.desc()).first()


def test_a_run_that_leaves_mail_waiting_is_partial_and_names_it(app, monkeypatch):
    history = _run_with_held(monkeypatch, [494344, 494581])

    assert history.status == "partial"
    assert "2 email(s) could not be stored" in history.error_message
    assert "494344, 494581" in history.error_message


def test_a_run_with_nothing_waiting_is_completed(app, monkeypatch):
    history = _run_with_held(monkeypatch, [])

    assert history.status == "completed"
    assert history.error_message is None


def test_the_history_line_names_a_bounded_number_of_uids(app, monkeypatch):
    history = _run_with_held(monkeypatch, range(1, module.HELD_UIDS_SHOWN + 5))

    assert f"{module.HELD_UIDS_SHOWN + 4} email(s)" in history.error_message
    assert history.error_message.endswith(", ...)")
    assert f", {module.HELD_UIDS_SHOWN + 1}" not in history.error_message


def test_an_email_half_stored_on_its_retry_is_not_released(app, monkeypatch):
    """One email, two listings: the first is stored, the second fails and the
    write recording that fails too. The email must still be on the list."""
    service = PropertyIMAPService()
    HeldUids(service._held_file_path()).hold(7, "blocked")
    service._held = HeldUids(service._held_file_path())

    def refuse(self, uid, reason):
        raise OSError("disk full")

    service._advance_uid_cursor({"uid": 7})
    monkeypatch.setattr(HeldUids, "hold", refuse)
    service._hold_uid(7, "its second listing could not be stored")
    service._release_stored_held_uids()

    assert HeldUids(service._held_file_path()).uids() == [7]


def test_an_email_stored_in_full_on_its_retry_is_released(app):
    service = PropertyIMAPService()
    HeldUids(service._held_file_path()).hold(7, "blocked")
    service._held = HeldUids(service._held_file_path())

    service._advance_uid_cursor({"uid": 7})
    assert HeldUids(service._held_file_path()).uids() == [7], "not before the end"
    service._release_stored_held_uids()

    assert HeldUids(service._held_file_path()).uids() == []
