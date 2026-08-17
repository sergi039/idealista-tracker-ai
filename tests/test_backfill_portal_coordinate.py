"""Establishing the portal pin for rows imported before the field existed.

The tool exists because #393's guard reads provenance, and the 56 fotocasa rows
already in the table carry none. What it must never do is *move* a row: it
writes provenance and nothing else, and where the page disagrees with what is
stored it writes nothing at all and says so.

That last rule is the one worth testing hardest. Recording the page's
coordinate on a row that holds a different one would make the next refresh move
the row to a point it has never held -- which is #393 pointed the other way,
and would be the tool meant to fix the defect committing it.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.coordinate_quality import portal_coordinate
from services.fotocasa_source import FotocasaListing
from tests import setup_test_environment
from utils import backfill_portal_coordinate as backfill

PORTAL_LAT, PORTAL_LON = 43.5708050, -5.8932443
URL = "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d"


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Plots",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        yield app
        db.drop_all()


def _row(**overrides):
    row = Property(
        source_email_id=overrides.pop(
            "source_email_id", "manual:plots-fotocasa-2026-08-15:190280914"
        ),
        title="Imported by the old script",
        url=URL,
        location_lat=PORTAL_LAT,
        location_lon=PORTAL_LON,
        location_accuracy="approximate",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    db.session.add(row)
    db.session.commit()
    return row


def _page(lat=PORTAL_LAT, lon=PORTAL_LON, refusal=None):
    return FotocasaListing(
        url=URL,
        listing_id=190280914,
        refusal=refusal,
        latitude=None if refusal else lat,
        longitude=None if refusal else lon,
    )


def _patch(monkeypatch, listing):
    monkeypatch.setattr(backfill, "fetch_listing", lambda url, session=None: listing)


class TestOutcomes:
    def test_an_agreeing_page_records_the_pin(self, app, monkeypatch):
        _patch(monkeypatch, _page())
        with app.app_context():
            row = _row()

            result = backfill.process(row)

            assert result["outcome"] == backfill.OUTCOME_RECORDED
            assert portal_coordinate(row) == (PORTAL_LAT, PORTAL_LON, "fotocasa")

    def test_the_recorded_pin_is_the_rows_own_number(self, app, monkeypatch):
        """They agree to within a metre, and every measurement on the row was
        taken from the stored one."""
        _patch(monkeypatch, _page(lat=PORTAL_LAT + 0.000004))
        with app.app_context():
            row = _row()

            backfill.process(row)

            recorded = row.enrichment["import"]["coordinate"]
            assert recorded["lat"] == str(row.location_lat)

    def test_a_disagreeing_page_writes_nothing_and_says_so(self, app, monkeypatch):
        """The rule that keeps this tool from committing #393 in reverse."""
        _patch(monkeypatch, _page(lat=43.5489861, lon=-5.8972205))
        with app.app_context():
            row = _row()

            result = backfill.process(row)

            assert result["outcome"] == backfill.OUTCOME_DIFFERS
            assert result["metres_apart"] == pytest.approx(2447, abs=5)
            assert portal_coordinate(row) is None
            assert (row.enrichment or {}).get("import") is None

    def test_an_unreadable_page_writes_nothing(self, app, monkeypatch):
        _patch(monkeypatch, _page(refusal="blocked"))
        with app.app_context():
            row = _row()

            result = backfill.process(row)

            assert result["outcome"] == backfill.OUTCOME_UNREADABLE
            assert result["reason"] == "blocked"
            assert portal_coordinate(row) is None

    def test_a_page_with_no_coordinate_writes_nothing(self, app, monkeypatch):
        _patch(monkeypatch, _page(lat=None, lon=None))
        with app.app_context():
            row = _row()

            assert backfill.process(row)["outcome"] == backfill.OUTCOME_NO_COORDINATE
            assert portal_coordinate(row) is None

    def test_a_row_with_no_coordinate_is_not_filled_in(self, app, monkeypatch):
        """Filling an empty coordinate moves the row; that is a different
        decision from corroborating the one it has."""
        _patch(monkeypatch, _page())
        with app.app_context():
            row = _row(location_lat=None, location_lon=None)

            result = backfill.process(row)

            assert result["outcome"] == backfill.OUTCOME_NO_STORED_COORDINATE
            assert row.location_lat is None
            assert portal_coordinate(row) is None

    def test_a_dry_run_reads_and_writes_nothing(self, app, monkeypatch):
        _patch(monkeypatch, _page())
        with app.app_context():
            row = _row()

            result = backfill.process(row, apply=False)

            assert result["outcome"] == backfill.OUTCOME_RECORDED
            assert portal_coordinate(row) is None


class TestScope:
    def test_only_fotocasa_rows_with_no_pin_are_in_scope(self, app):
        with app.app_context():
            needs_it = _row(source_email_id="fotocasa-old")
            already = _row(
                source_email_id="fotocasa-new",
                enrichment={
                    "import": {
                        "coordinate": {
                            "source": "fotocasa",
                            "lat": str(PORTAL_LAT),
                            "lon": str(PORTAL_LON),
                        }
                    }
                },
            )
            idealista = _row(
                source_email_id="alert-1",
                url="https://www.idealista.com/en/inmueble/1/",
            )

            scoped = {row.id for row in backfill._scope(None, None, 0)}

            assert needs_it.id in scoped
            assert already.id not in scoped
            assert idealista.id not in scoped

    def test_a_recorded_row_leaves_scope(self, app, monkeypatch):
        """What makes an interrupted run resumable without a flag."""
        _patch(monkeypatch, _page())
        with app.app_context():
            row = _row()
            assert row.id in {r.id for r in backfill._scope(None, None, 0)}

            backfill.process(row)

            assert row.id not in {r.id for r in backfill._scope(None, None, 0)}


class TestDistance:
    def test_the_measured_pair_is_the_measured_distance(self):
        """The two points from #393, and the number the issue records."""
        apart = backfill._metres_apart(PORTAL_LAT, PORTAL_LON, 43.5489861, -5.8972205)

        assert apart == pytest.approx(2447, abs=5)

    def test_identical_points_are_zero_apart(self):
        assert backfill._metres_apart(
            PORTAL_LAT, PORTAL_LON, PORTAL_LAT, PORTAL_LON
        ) == pytest.approx(0.0, abs=0.001)
