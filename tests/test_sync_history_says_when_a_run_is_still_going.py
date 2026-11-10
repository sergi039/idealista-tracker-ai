"""A sync_history row says `running` while its run is going (#637).

The column default is `completed`, so a run that was still working, or that a
deploy had killed, read `completed` with `completed_at` NULL. Writers now say
`running`; `services.sync_history_status` is the one reader, and `interrupted`
is a presentation state that is never stored.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app, db
from config import Config
from models import SyncHistory
from services import sync_history_status as sh
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


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


def _run(monkeypatch, seen):
    service = PropertyIMAPService()

    def mail():
        seen.append(SyncHistory.query.order_by(SyncHistory.id.desc()).first().status)
        return []

    monkeypatch.setattr(service, "get_idealista_emails", mail)
    service.run_ingestion(sync_type="test")
    return SyncHistory.query.order_by(SyncHistory.id.desc()).first()


class TestTheWriter:
    def test_the_row_reads_running_during_the_run(self, app, monkeypatch):
        seen = []
        _run(monkeypatch, seen)
        assert seen == ["running"]

    def test_the_row_reads_completed_after_the_run(self, app, monkeypatch):
        row = _run(monkeypatch, [])
        assert row.status == "completed"
        assert row.completed_at is not None

    def test_a_failed_run_still_reads_failed(self, app, monkeypatch):
        service = PropertyIMAPService()

        def boom():
            raise RuntimeError("imap down")

        monkeypatch.setattr(service, "get_idealista_emails", boom)
        service.run_ingestion(sync_type="test")
        row = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
        assert row.status == "failed"


class TestTheReader:
    def _read(self, status, age, completed_at=None):
        started = NOW - age
        return sh.read_state(status, started.replace(tzinfo=None), completed_at, NOW)

    def test_a_fresh_running_row_is_running(self):
        assert self._read("running", timedelta(minutes=5)) == "running"

    def test_an_old_running_row_is_interrupted(self):
        age = sh.SYNC_RUN_STALE_AFTER + timedelta(minutes=1)
        assert self._read("running", age) == "interrupted"

    def test_a_legacy_completed_row_without_a_finish_is_interrupted(self):
        age = sh.SYNC_RUN_STALE_AFTER + timedelta(hours=30)
        assert self._read("completed", age) == "interrupted"

    def test_a_young_legacy_unfinished_row_is_running(self):
        assert self._read("completed", timedelta(minutes=1)) == "running"

    @pytest.mark.parametrize("status", ["completed", "partial", "failed"])
    def test_finished_rows_are_unchanged(self, status):
        done = NOW - timedelta(days=3)
        assert self._read(status, timedelta(days=3), done) == status

    def test_an_aware_start_is_compared_like_a_naive_one(self):
        old = NOW - sh.SYNC_RUN_STALE_AFTER - timedelta(seconds=1)
        assert sh.read_state("running", old, None, NOW) == "interrupted"

    def test_a_running_row_without_a_start_is_not_believed(self):
        assert sh.read_state("running", None, None, NOW) == "interrupted"


class TestTheSurface:
    def _stats(self, app):
        response = app.test_client().get("/api/stats")
        assert response.status_code == 200
        return json.loads(response.data)["stats"]["last_sync"]

    def test_api_stats_shows_running_and_interrupted(self, app):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row = SyncHistory(
            sync_type="x", backend="imap", status="running", started_at=now
        )
        db.session.add(row)
        db.session.commit()
        assert self._stats(app)["status"] == "running"

        row.started_at = now - sh.SYNC_RUN_STALE_AFTER - timedelta(hours=1)
        db.session.commit()
        assert self._stats(app)["status"] == "interrupted"

    def test_a_running_row_does_not_replace_the_last_finished_sync(self, app):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.add_all(
            [
                SyncHistory(
                    sync_type="x",
                    backend="imap",
                    status="completed",
                    started_at=now - timedelta(days=1),
                    completed_at=now - timedelta(days=1),
                ),
                SyncHistory(
                    sync_type="x", backend="imap", status="running", started_at=now
                ),
            ]
        )
        db.session.commit()
        assert self._stats(app)["status"] == "completed"

    def test_the_page_script_labels_both_states(self, app):
        response = app.test_client().get("/static/js/main.js")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Sync running" in body and "Sync interrupted" in body
