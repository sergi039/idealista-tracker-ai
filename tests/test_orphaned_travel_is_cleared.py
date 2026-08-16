"""Travel measured from a coordinate the row no longer has must go (#331).

Four rows lost their coordinates deliberately: their geocode resolved to the
country, `PropertyLocationService` refused it, and they were left honestly
unlocatable. Their `travel` block stayed behind — six preset durations, a
beaches list and the travel component of `score_total`, every one of them
measured from Spain's centroid — and went on rendering as though it described
the property.

An empty travel block on a row with no coordinates is the truth. A populated
one is a measurement of somewhere else.
"""

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment
from utils.recalc_property_travel import _clear_orphaned, orphaned_travel_rows

TRAVEL = {
    "targets": {"airport": {"duration_min": 11, "place": {"name": "La Paz"}}},
    "beaches": {"status": "not_found", "items": []},
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _prop(**kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", "orphan"),
        title=kw.pop("title", "Finca offers for"),
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


class TestWhichRowsAreOrphaned:
    def test_no_coordinates_and_a_travel_block_is_orphaned(self, app):
        with app.app_context():
            prop = _prop(travel=dict(TRAVEL))
            assert orphaned_travel_rows([prop]) == [prop]

    def test_a_row_with_coordinates_is_left_alone(self, app):
        """The tool must never touch a row whose numbers still have an origin."""
        with app.app_context():
            prop = _prop(
                source_email_id="orphan_located",
                travel=dict(TRAVEL),
                location_lat=43.5,
                location_lon=-5.65,
            )
            assert orphaned_travel_rows([prop]) == []

    def test_half_a_coordinate_is_no_coordinate(self, app):
        """A latitude without a longitude locates nothing."""
        with app.app_context():
            prop = _prop(
                source_email_id="orphan_half", travel=dict(TRAVEL), location_lat=43.5
            )
            assert orphaned_travel_rows([prop]) == [prop]

    def test_a_row_with_no_travel_block_is_not_reported(self, app):
        """Nothing to clear is not the same as something to clear."""
        with app.app_context():
            assert orphaned_travel_rows([_prop(source_email_id="orphan_empty")]) == []
            assert (
                orphaned_travel_rows([_prop(source_email_id="orphan_none", travel={})])
                == []
            )


class TestTheClearReachesTheRow:
    def test_the_block_is_gone_after_the_commit(self, app):
        with app.app_context():
            prop = _prop(source_email_id="orphan_commit", travel=dict(TRAVEL))
            pid = prop.id

            assert _clear_orphaned(None, None) == 1

            db.session.expire_all()
            assert not db.session.get(Property, pid).travel

    def test_a_located_row_keeps_its_travel(self, app):
        with app.app_context():
            keep = _prop(
                source_email_id="orphan_keep",
                travel=dict(TRAVEL),
                location_lat=43.5,
                location_lon=-5.65,
            )
            pid = keep.id

            _clear_orphaned(None, None)

            db.session.expire_all()
            assert db.session.get(Property, pid).travel == TRAVEL

    def test_ids_narrows_the_scope(self, app):
        with app.app_context():
            a = _prop(source_email_id="orphan_a", travel=dict(TRAVEL))
            b = _prop(source_email_id="orphan_b", travel=dict(TRAVEL))
            b_id = b.id

            assert _clear_orphaned(None, str(a.id)) == 1

            db.session.expire_all()
            assert db.session.get(Property, b_id).travel == TRAVEL
