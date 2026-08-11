"""The cleanup that removes the airport a legacy `Land` never had.

`utils/clear_legacy_land_airport.py` deletes the three `transport` keys the
unfiltered `type=airport` search wrote (see
`tests/test_legacy_land_airport_wide_search.py` for the measurement). It
rewrites live rows, so what is pinned here is the safety around it: a rollback
snapshot written before the first write, a restore that puts the old values
back, a dry run that changes nothing, and — the one that was found the hard
way — that `--rescore` still selects something after a clearing pass has
already been through.

That last one was a real hole. Selecting "lands still carrying the airport
keys" is right for clearing and wrong for rescoring: run the clearing pass
first and nothing carries them, so a later `--rescore` selected zero lands and
reported success having done nothing at all.
"""

import json

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Land  # noqa: E402
from utils import clear_legacy_land_airport as tool  # noqa: E402


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _land(key, with_airport=True):
    transport = {
        "train_station_available": True,
        "train_station_distance": 2400,
        "distance_to_oviedo_city_center": 31000,
    }
    if with_airport:
        transport.update(
            {
                # The helipad the unfiltered search accepted.
                "airport_available": True,
                "airport_distance": 6750.0,
                "airport_travel_time": 5,
            }
        )
    land = Land(
        source_email_id=f"cleanup-{key}",
        title=f"Land {key}",
        location_lat=43.5,
        location_lon=-6.8,
    )
    land.transport = transport
    db.session.add(land)
    db.session.commit()
    return land


class TestSelection:
    def test_a_rescore_after_a_clearing_pass_still_has_work_to_do(self, app):
        """The hole: nothing carries the keys once clearing has run."""
        _land("already-cleared", with_airport=False)
        _land("also-cleared", with_airport=False)
        lands = Land.query.all()

        assert tool.carrying_the_keys(lands) == []
        # Clearing has nothing left to do, and says so by selecting nothing.
        assert tool.select_lands(lands, rescore=False) == []
        # A rescore does: those scores are stale *because* the keys are gone.
        assert len(tool.select_lands(lands, rescore=True)) == 2

    def test_clearing_alone_only_touches_lands_that_carry_the_keys(self, app):
        carrying = _land("carrying", with_airport=True)
        _land("clean", with_airport=False)
        lands = Land.query.all()

        assert tool.select_lands(lands, rescore=False) == [carrying]
        assert len(tool.select_lands(lands, rescore=True)) == 2

    def test_selection_never_hands_back_the_caller_s_own_list(self, app):
        """`--rescore` must not alias the query result it was given."""
        _land("aliasing")
        lands = Land.query.all()

        assert tool.select_lands(lands, rescore=True) is not lands


class TestClearing:
    def test_only_the_three_airport_keys_go(self, app):
        land = _land("one")
        before = dict(land.transport)

        land.transport = tool._cleared(land.transport)
        db.session.commit()

        removed = set(before) - set(land.transport)
        assert removed == set(tool.AIRPORT_KEYS)
        # Neighbouring keys keep their values, not just their names.
        for key in land.transport:
            assert land.transport[key] == before[key]

    def test_a_fresh_dict_is_returned_so_sqlalchemy_notices(self, app):
        """A JSON column only persists when the top-level value is replaced."""
        land = _land("two")
        original = land.transport
        cleared = tool._cleared(original)

        assert cleared is not original
        assert "airport_distance" in original  # the input is left alone


class TestSnapshotAndRestore:
    def test_the_snapshot_round_trips_the_values_it_will_overwrite(self, app, tmp_path):
        land = _land("three")
        land.score_total = 57.93
        land.score_investment = 60.0
        land.score_lifestyle = 55.0
        db.session.commit()

        path = str(tmp_path / "snap.json")
        tool._write_snapshot([tool._snapshot_row(land)], path)

        land.transport = tool._cleared(land.transport)
        land.score_total = 59.45
        db.session.commit()
        assert "airport_distance" not in land.transport

        assert tool._restore(path) == 1
        restored = db.session.get(Land, land.id)
        assert restored.transport["airport_distance"] == 6750.0
        assert float(restored.score_total) == pytest.approx(57.93)

    def test_it_refuses_to_overwrite_an_existing_rollback_point(self, app, tmp_path):
        path = str(tmp_path / "snap.json")
        tool._write_snapshot([{"id": 1}], path)

        with pytest.raises(SystemExit):
            tool._write_snapshot([{"id": 2}], path)

        # And the first one is still intact.
        with open(path, encoding="utf-8") as handle:
            assert json.load(handle) == [{"id": 1}]
