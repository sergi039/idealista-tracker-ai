"""The bulk travel recalculation for universal properties.

`utils/recalc_travel_times.py` only ever knew the legacy `Land`, so the 356
listings that live in `properties` had no bulk path -- which is why the wrong
airports found in #171 could not be corrected without one.

The run spends Google quota and rewrites `travel` plus every score column, so
what is pinned here is the safety around it: a rollback snapshot written
before the first write, a restore that puts the old values back, and a report
of which resolved places actually moved.
"""

import json
from decimal import Decimal

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from utils import recalc_property_travel as tool  # noqa: E402


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _property(key, place_name, lat=43.5, lon=-5.6):
    prop = Property(
        source_email_id=f"recalc-{key}",
        title=f"Recalc {key}",
        location_lat=lat,
        location_lon=lon,
    )
    prop.travel = {
        "targets": {
            "airport": {
                "kind": "preset",
                "status": "ok",
                "duration_min": 10,
                "place": {"name": place_name},
            }
        }
    }
    prop.score_total = 50
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheSnapshot:
    def test_it_captures_travel_and_every_score_column(self, app):
        prop = _property("snap", "GlueWay System")

        row = tool._snapshot_row(prop)

        assert row["id"] == prop.id
        assert row["travel"]["targets"]["airport"]["place"]["name"] == "GlueWay System"
        # Stored as a string so a Decimal survives the JSON round trip intact.
        assert Decimal(row["score_total"]) == Decimal("50")
        assert set(row) == {
            "id",
            "travel",
            "score_total",
            "score_investment",
            "score_lifestyle",
            "scoring",
        }

    def test_it_refuses_to_overwrite_an_existing_rollback_point(self, app, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text("[]", encoding="utf-8")

        with pytest.raises(SystemExit):
            tool._write_snapshot([], str(path))

    def test_restore_puts_the_old_values_back(self, app, tmp_path):
        prop = _property("restore", "Helipuerto HUCA")
        path = tmp_path / "snapshot.json"
        tool._write_snapshot([tool._snapshot_row(prop)], str(path))

        prop.travel = {"targets": {"airport": {"place": {"name": "Asturias Airport"}}}}
        prop.score_total = 99
        db.session.commit()

        restored = tool._restore(str(path))

        refreshed = db.session.get(Property, prop.id)
        assert restored == 1
        assert (
            refreshed.travel["targets"]["airport"]["place"]["name"] == "Helipuerto HUCA"
        )
        assert float(refreshed.score_total) == 50.0


class TestTheChangeReport:
    def test_it_reads_the_resolved_place_of_every_target(self):
        travel = {
            "targets": {
                "airport": {"place": {"name": "GlueWay System"}},
                "school": {"place": {"name": "Escuela La Serena"}},
                "hospital": {"status": "not_found"},
            }
        }

        assert tool._target_places(travel) == {
            "airport": "GlueWay System",
            "school": "Escuela La Serena",
            "hospital": None,
        }

    @pytest.mark.parametrize("travel", [None, {}, {"targets": None}, "nonsense"])
    def test_a_property_without_targets_reports_nothing(self, travel):
        assert tool._target_places(travel) == {}


class TestTheRunItself:
    def _run(self, monkeypatch, tmp_path, *, resolves_to, extra_args=()):
        """Drive main() with the Google calls replaced by a fixed answer."""

        def _fake_travel(self, prop, commit=False):
            prop.travel = {
                "targets": {
                    "airport": {
                        "kind": "preset",
                        "status": "ok",
                        "duration_min": 42,
                        "place": {"name": resolves_to},
                    }
                }
            }
            return True

        monkeypatch.setattr(
            "services.property_travel_service.PropertyTravelService.calculate_for_property",
            _fake_travel,
        )
        monkeypatch.setattr(
            "services.property_scoring_service.PropertyScoringService.calculate_for_property",
            lambda self, prop, commit=False: True,
        )
        monkeypatch.setattr(
            "utils.recalc_property_travel.create_app", lambda: _CurrentApp()
        )
        argv = [
            "recalc",
            "--snapshot",
            str(tmp_path / "snap.json"),
            "--sleep",
            "0",
            *extra_args,
        ]
        monkeypatch.setattr("sys.argv", argv)
        tool.main()

    def test_it_rewrites_the_wrong_airport_and_reports_it(
        self, app, monkeypatch, tmp_path
    ):
        _property("a", "GlueWay System")
        _property("b", "Helipuerto HUCA", lat=43.6, lon=-5.7)
        report = tmp_path / "report.json"

        self._run(
            monkeypatch,
            tmp_path,
            resolves_to="Asturias Airport",
            extra_args=("--report", str(report)),
        )

        rows = Property.query.order_by(Property.id).all()
        assert [r.travel["targets"]["airport"]["place"]["name"] for r in rows] == [
            "Asturias Airport",
            "Asturias Airport",
        ]
        changes = json.loads(report.read_text(encoding="utf-8"))
        assert len(changes) == 2
        assert changes[0]["targets"]["airport"] == {
            "before": "GlueWay System",
            "after": "Asturias Airport",
        }

    def test_an_unchanged_place_is_not_reported_as_a_change(
        self, app, monkeypatch, tmp_path
    ):
        _property("same", "Asturias Airport")
        report = tmp_path / "report.json"

        self._run(
            monkeypatch,
            tmp_path,
            resolves_to="Asturias Airport",
            extra_args=("--report", str(report)),
        )

        assert json.loads(report.read_text(encoding="utf-8")) == []

    def test_the_snapshot_is_written_before_anything_is_rewritten(
        self, app, monkeypatch, tmp_path
    ):
        _property("snapshot-first", "GlueWay System")

        self._run(monkeypatch, tmp_path, resolves_to="Asturias Airport")

        snapshot = json.loads((tmp_path / "snap.json").read_text(encoding="utf-8"))
        assert snapshot[0]["travel"]["targets"]["airport"]["place"]["name"] == (
            "GlueWay System"
        ), "the snapshot must hold the pre-run value, or it is not a rollback point"


class _CurrentApp:
    """Hands main() the app context the test fixture already established."""

    def app_context(self):
        from contextlib import nullcontext

        return nullcontext()
