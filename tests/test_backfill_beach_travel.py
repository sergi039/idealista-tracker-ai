"""The scoped beach/travel backfill (issue #271, owner rule 2026-08-14).

Auto-enrich covers only the last-30-days window plus favorites; everything
older stays manual. Within scope, only rows with no beaches key or a refused
one are selected — a measured answer, including a measured absence, must not
be re-billed. `--max-rows` defers the tail honestly, the ledger records every
processed row, and restore puts the snapshot back byte for byte.
"""

import json
from datetime import datetime, timedelta

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment
from utils.backfill_beach_travel import needs_beaches, run, select_scope

NOW = datetime.utcnow()

MEASURED_BEACHES = {
    "status": "ok",
    "max_drive_min": 20,
    "items": [{"name": "Playa", "duration_min": 5, "lat": 43.5, "lon": -6.8}],
}
MEASURED_NONE = {"status": "none_within_limit", "max_drive_min": 20, "items": []}
REFUSED = {"status": "unavailable", "stage": "places", "items": []}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _add(
    app, email_id, created_days_ago=0, favorite=False, beaches="missing", coords=True
):
    with app.app_context():
        travel = None
        if beaches != "no-travel":
            travel = {"targets": {}, "api_status": {"state": "ok"}}
            if beaches == "measured":
                travel["beaches"] = MEASURED_BEACHES
            elif beaches == "measured-none":
                travel["beaches"] = MEASURED_NONE
            elif beaches == "refused":
                travel["beaches"] = REFUSED
        prop = Property(
            source_email_id=email_id,
            title=email_id,
            municipality="Navia",
            location_lat=43.54 if coords else None,
            location_lon=-6.72 if coords else None,
            is_favorite=favorite,
            created_at=NOW - timedelta(days=created_days_ago),
            travel=travel,
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


class TestScope:
    def test_recent_and_favorites_in_old_plain_out(self, app):
        recent = _add(app, "recent", created_days_ago=3)
        old_fav = _add(app, "old-fav", created_days_ago=200, favorite=True)
        _add(app, "old-plain", created_days_ago=200)
        with app.app_context():
            ids = {p.id for p in select_scope(days=30)}
        assert ids == {recent, old_fav}, (
            "the owner's rule: last 30 days + favorites; the rest is manual"
        )

    def test_measured_rows_leave_the_scope_refused_stay(self, app):
        _add(app, "measured", beaches="measured")
        _add(app, "measured-none", beaches="measured-none")
        refused = _add(app, "refused", beaches="refused")
        missing = _add(app, "missing", beaches="missing")
        with app.app_context():
            ids = {p.id for p in select_scope(days=30)}
        assert ids == {refused, missing}, (
            "a measured absence is done (#98); a refusal is retried"
        )

    def test_no_coordinates_no_scope(self, app):
        _add(app, "no-coords", coords=False)
        with app.app_context():
            assert select_scope(days=30) == []

    def test_all_flag_ignores_the_window(self, app):
        old = _add(app, "old-plain", created_days_ago=200)
        with app.app_context():
            ids = {p.id for p in select_scope(days=30, include_all=True)}
        assert old in ids


class TestNeedsBeaches:
    def test_states(self):
        assert needs_beaches(Property(travel=None)) is True
        assert needs_beaches(Property(travel={"targets": {}})) is True
        assert needs_beaches(Property(travel={"beaches": REFUSED})) is True
        assert needs_beaches(Property(travel={"beaches": MEASURED_BEACHES})) is False
        assert needs_beaches(Property(travel={"beaches": MEASURED_NONE})) is False


class _FakeTravelService:
    """Writes a measured answer without touching Google."""

    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.calls = []

    def calculate_for_property(self, prop, commit=False):
        self.calls.append(prop.id)
        if prop.id in self.fail_ids:
            raise RuntimeError("simulated refusal")
        prop.travel = {
            "targets": {},
            "beaches": dict(MEASURED_BEACHES),
            "api_status": {"state": "ok"},
        }
        return True


class _FakeScoringService:
    def calculate_for_property(self, prop, commit=False):
        return True


class TestRun:
    def test_max_rows_defers_the_tail_and_a_rerun_completes_it(self, app, tmp_path):
        for n in range(3):
            _add(app, f"row-{n}", created_days_ago=1)
        ledger = tmp_path / "run.ledger.jsonl"
        with app.app_context():
            scope = select_scope(days=30)
            report = run(
                scope,
                str(ledger),
                max_rows=2,
                sleep_s=0,
                travel_service=_FakeTravelService(),
                scoring_service=_FakeScoringService(),
            )
            assert report["processed"] == 2
            assert report["deferred"] == 1

            # The two processed rows gained a measured answer and left the
            # scope; the rerun sees exactly the deferred one.
            remaining = select_scope(days=30)
            assert len(remaining) == 1
            report2 = run(
                remaining,
                str(ledger),
                sleep_s=0,
                travel_service=_FakeTravelService(),
                scoring_service=_FakeScoringService(),
            )
            assert report2["processed"] == 1
            assert report2["deferred"] == 0

    def test_ledger_records_every_row_and_the_report(self, app, tmp_path):
        _add(app, "row-a", created_days_ago=1)
        ledger = tmp_path / "run.ledger.jsonl"
        with app.app_context():
            run(
                select_scope(days=30),
                str(ledger),
                sleep_s=0,
                travel_service=_FakeTravelService(),
                scoring_service=_FakeScoringService(),
            )
        lines = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert lines[0]["outcome"] == "ok"
        assert lines[0]["beaches_status"] == "ok"
        assert lines[-1]["report"]["processed"] == 1

    def test_a_failed_row_is_recorded_and_does_not_stop_the_run(self, app, tmp_path):
        first = _add(app, "row-a", created_days_ago=1)
        _add(app, "row-b", created_days_ago=1)
        ledger = tmp_path / "run.ledger.jsonl"
        with app.app_context():
            report = run(
                select_scope(days=30),
                str(ledger),
                sleep_s=0,
                travel_service=_FakeTravelService(fail_ids={first}),
                scoring_service=_FakeScoringService(),
            )
        assert report["failed"] == 1
        assert report["outcomes"].get("ok") == 1
