"""A location a person established outranks the geocoder (GEO-002, #265).

Measured on production 2026-08-20. `ensure_coordinates(refresh=True)` writes
Google's accuracy unconditionally, and the only thing it defends is a portal pin
(#393). So a `precise` somebody curated silently returns to `approximate`, and
the components that label unlocks -- the sea distance and the travel average,
via `services/coordinate_quality.py` -- drop out of the score with it.

The exposure is not hypothetical and neither is the curation. Three rows carry a
location a person established, in three different ad-hoc shapes, and **nothing
in the repository read any of them**: 161 and 792 under
`enrichment["coordinate_provenance"]` (`method` values that do not match, and
timestamps under two different names), 774 under `enrichment["cadastre"]`. 161
and 792 both carry a `precise` their own `enrichment["geocoding"]` record
contradicts -- the fingerprint of a write made outside the geocoder. 161 is
defended today only by accident, because it happens to also carry a portal pin;
792 is not defended at all, and 129 of the 130 `precise` rows carry no pin.

Two things are pinned here that are decisions rather than mechanics.

**A malformed block does not stop a geocode.** A block that cannot be read is
not a hand-set location, because the alternative is a row pinned to a
coordinate that nothing can correct and nothing can explain.

**The block is not written where `portal_coordinate` looks.** That was the
other route considered for this row and it is rejected: a conclusion drawn from
the cadastre stored under "the pin the portal published" is the STATUS-002
mistake in a new column.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.coordinate_quality import (
    is_hand_set,
    manual_coordinate,
    portal_coordinate,
    record_manual_coordinate,
)
from services.property_location_service import (
    PropertyLocationService,
    clear_location_by_hand,
    set_location_by_hand,
)
from tests import setup_test_environment

# The barrio centre a session established for property 792 on 2026-08-20, and
# the village centroid Google answers with for its title.
HAND_LAT, HAND_LON = 43.539637, -5.547554
GOOGLE_LAT, GOOGLE_LON = 43.5397250, -5.5478309
NOTE = "cadastre_barrio_verified: Barrio del Medio, Quintes; 13 parcels, spread 341 m"


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        db.session.add(
            SearchProfile(
                name="Plots",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
        )
        db.session.commit()
        yield app
        db.drop_all()


def _row(**overrides):
    row = Property(
        source_email_id="geo002:792",
        title="Land for sale in Quintes, Villaviciosa",
        municipality="Villaviciosa",
        url="https://www.idealista.com/en/inmueble/91523456/",
        location_lat=HAND_LAT,
        location_lon=HAND_LON,
        location_accuracy="precise",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    db.session.add(row)
    db.session.commit()
    return row


class _Geocoder:
    """Stands in for Google, and records what it was asked.

    Records the queries rather than counting calls, for the reason
    `tests/test_portal_pin_survives_a_refresh.py` gives: the question here is
    "was Google asked at all", and an empty list answers it in a way a zero
    could not distinguish from a stub that was never wired in.
    """

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.queries = []

    def geocode_address(self, query):
        self.queries.append(query)
        return self.answers.pop(0) if self.answers else None


def _google_says(accuracy, lat=GOOGLE_LAT, lon=GOOGLE_LON):
    return {
        "lat": lat,
        "lng": lon,
        "accuracy": accuracy,
        "formatted_address": "Quintes, Villaviciosa, Spain",
        "types": ["locality", "political"],
        "address_components": [{"types": ["postal_code"], "long_name": "33314"}],
    }


class TestTheDefect:
    def test_without_the_block_a_curated_precise_is_lost(self, app):
        """The behaviour this ticket is about, pinned so the fix can be seen."""
        with app.app_context():
            row = _row()
            geocoder = _Geocoder([_google_says("approximate")])

            PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)

            assert geocoder.queries, "Google was not asked at all"
            assert row.location_accuracy == "approximate"
            assert float(row.location_lat) == pytest.approx(GOOGLE_LAT)


class TestTheDefence:
    def test_a_hand_set_row_is_not_geocoded_at_all(self, app):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row,
                lat=HAND_LAT,
                lon=HAND_LON,
                accuracy="precise",
                note=NOTE,
                commit=True,
            )
            geocoder = _Geocoder([_google_says("approximate")])

            assert (
                PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)
                is True
            )

            # Not merely "the row is unchanged": the request was never made, so
            # the refusal costs nothing and cannot be defeated by an answer.
            assert geocoder.queries == []
            assert row.location_accuracy == "precise"
            assert float(row.location_lat) == pytest.approx(HAND_LAT)

    def test_even_a_precise_answer_does_not_displace_it(self, app):
        """`improves_on` is not consulted: a person outranks a better label."""
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="approximate", note=NOTE
            )
            geocoder = _Geocoder([_google_says("precise", lat=43.9, lon=-5.9)])

            PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)

            assert geocoder.queries == []
            assert row.location_accuracy == "approximate"
            assert float(row.location_lat) == pytest.approx(HAND_LAT)

    def test_a_block_whose_columns_were_emptied_reports_no_coordinate(self, app):
        """The return value keeps meaning "does this row have a coordinate"."""
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            row.location_lat = None
            row.location_lon = None
            db.session.commit()
            geocoder = _Geocoder([_google_says("approximate")])

            assert (
                PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)
                is False
            )
            assert geocoder.queries == []


class TestTheWriter:
    def test_it_writes_the_columns_the_block_and_what_it_displaced(self, app):
        with app.app_context():
            row = _row(
                location_lat=GOOGLE_LAT,
                location_lon=GOOGLE_LON,
                location_accuracy="approximate",
            )

            outcome = set_location_by_hand(
                row,
                lat=HAND_LAT,
                lon=HAND_LON,
                accuracy="precise",
                note=NOTE,
                source="cadastre",
                commit=True,
            )

            stored = db.session.get(Property, row.id)
            assert float(stored.location_lat) == pytest.approx(HAND_LAT)
            assert stored.location_accuracy == "precise"

            hand = manual_coordinate(stored)
            assert hand is not None
            assert hand.accuracy == "precise"
            assert hand.source == "cadastre"
            assert hand.note == NOTE
            assert hand.set_at

            # What the row said before is recorded, so clearing the block is a
            # decision somebody can act on rather than a value that is gone.
            assert outcome["displaced"]["accuracy"] == "approximate"
            assert float(outcome["displaced"]["lat"]) == pytest.approx(GOOGLE_LAT)

    def test_the_block_is_not_where_portal_coordinate_looks(self, app):
        """The rejected route, pinned: an inference is not a portal's pin."""
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )

            assert portal_coordinate(row) is None
            assert "coordinate" not in (row.enrichment.get("import") or {})

    def test_it_leaves_the_other_blocks_alone(self, app):
        with app.app_context():
            row = _row(enrichment={"sea": {"status": "ok"}, "pool": {"state": "none"}})

            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )

            stored = db.session.get(Property, row.id)
            assert stored.enrichment["sea"] == {"status": "ok"}
            assert stored.enrichment["pool"] == {"state": "none"}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"note": "   "},
            {"note": ""},
            {"accuracy": "surveyed"},
            {"accuracy": ""},
            {"lat": "not a number"},
            {"lat": 91.0},
            {"lon": -181.0},
        ],
    )
    def test_it_refuses_what_it_cannot_stand_behind(self, app, kwargs):
        with app.app_context():
            row = _row(location_accuracy="approximate")
            args = {
                "lat": HAND_LAT,
                "lon": HAND_LON,
                "accuracy": "precise",
                "note": NOTE,
            }
            args.update(kwargs)

            with pytest.raises(ValueError):
                set_location_by_hand(row, **args)

            # Refused before anything was assigned: a bad argument must leave
            # the row as it was, not half-written.
            assert row.location_accuracy == "approximate"
            assert not is_hand_set(row)


class TestClearing:
    def test_it_removes_the_block_and_leaves_the_coordinate(self, app):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )

            outcome = clear_location_by_hand(row, commit=True)

            assert outcome["cleared"] is True
            assert outcome["previous"]["note"] == NOTE
            assert not is_hand_set(row)
            # Deliberately not restored: the block is not guaranteed to be
            # newer than the columns.
            assert float(row.location_lat) == pytest.approx(HAND_LAT)
            assert row.location_accuracy == "precise"

    def test_a_cleared_row_is_geocoded_again(self, app):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            clear_location_by_hand(row, commit=True)
            geocoder = _Geocoder([_google_says("approximate")])

            PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)

            assert geocoder.queries, "the computed path did not resume"
            assert row.location_accuracy == "approximate"

    def test_clearing_a_row_that_has_no_block_says_so(self, app):
        with app.app_context():
            row = _row()

            assert clear_location_by_hand(row, commit=True) == {
                "cleared": False,
                "previous": None,
            }


class TestAMalformedBlockIsNotAHandSetLocation:
    @pytest.mark.parametrize(
        "block",
        [
            None,
            {},
            "a string",
            {"lat": "43.5"},
            {"lat": "43.5", "lon": "-5.5"},
            {"lat": "43.5", "lon": "-5.5", "accuracy": "precise"},
            {"lat": "43.5", "lon": "-5.5", "accuracy": "precise", "note": "  "},
            {"lat": "x", "lon": "-5.5", "accuracy": "precise", "note": "n"},
            {"lat": "43.5", "lon": "-5.5", "accuracy": "surveyed", "note": "n"},
        ],
    )
    def test_it_reads_as_absent_and_the_geocode_proceeds(self, app, block):
        with app.app_context():
            row = _row(enrichment={"location": block} if block is not None else {})
            geocoder = _Geocoder([_google_says("approximate")])

            assert manual_coordinate(row) is None
            PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)

            assert geocoder.queries, "a block that cannot be read pinned the row"


class TestTheRecordFormat:
    def test_the_coordinate_is_stored_as_text(self):
        """`Numeric(10, 7)` is matched by decimal text, not by a JSON float."""
        block = record_manual_coordinate(
            None, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
        )

        assert block["location"]["lat"] == str(HAND_LAT)
        assert isinstance(block["location"]["lon"], str)


class TestTheWriterHonoursTheColumnContract:
    """`services/enrichment_write.py`'s rule, asked of this writer too.

    Not inherited by being written in the same shape: SQLAlchemy's SQLite
    dialect drops `FOR UPDATE` silently, so nothing about the lock is visible
    in the stored row and only the call can be observed.
    """

    def _spy(self, monkeypatch):
        seen = []
        original = db.session.refresh

        def spy(obj, *args, **kwargs):
            seen.append(kwargs.get("with_for_update"))
            return original(obj, *args, **kwargs)

        monkeypatch.setattr(db.session, "refresh", spy)
        return seen

    def test_the_row_is_read_for_update(self, app, monkeypatch):
        with app.app_context():
            row = _row()
            seen = self._spy(monkeypatch)

            set_location_by_hand(
                row,
                lat=HAND_LAT,
                lon=HAND_LON,
                accuracy="precise",
                note=NOTE,
                commit=True,
            )

            assert True in seen

    def test_commit_false_takes_no_lock(self, app, monkeypatch):
        with app.app_context():
            row = _row()
            seen = self._spy(monkeypatch)

            set_location_by_hand(
                row,
                lat=HAND_LAT,
                lon=HAND_LON,
                accuracy="precise",
                note=NOTE,
                commit=False,
            )

            assert True not in seen

    def test_a_dirty_session_is_refused(self, app):
        from services.enrichment_write import EnrichmentWriteContractError

        with app.app_context():
            row = _row()
            row.title = "touched"

            with pytest.raises(EnrichmentWriteContractError):
                set_location_by_hand(
                    row,
                    lat=HAND_LAT,
                    lon=HAND_LON,
                    accuracy="precise",
                    note=NOTE,
                    commit=True,
                )

    def test_a_bad_argument_never_takes_the_lock(self, app, monkeypatch):
        """An argument that cannot be stored costs a raise, not a lock and a
        rollback -- the same reason `check_writable` runs ahead of the geocode."""
        with app.app_context():
            row = _row()
            seen = self._spy(monkeypatch)

            with pytest.raises(ValueError):
                set_location_by_hand(
                    row,
                    lat=HAND_LAT,
                    lon=HAND_LON,
                    accuracy="precise",
                    note="   ",
                    commit=True,
                )

            assert True not in seen

    def test_clearing_is_locked_too(self, app, monkeypatch):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            seen = self._spy(monkeypatch)

            clear_location_by_hand(row, commit=True)

            assert True in seen


class TestTheRepairToolSaysWhatItSkipped:
    """`utils/refresh_property_accuracy.py` is the realistic trigger for this
    defect -- it is the one thing in the tree that calls
    `ensure_coordinates(refresh=True)` over a scope -- and adding the refusal
    changed what its summary means.

    Without this, a hand-set row is reported as `precise -> precise` and
    counted with the rows that were re-geocoded and came back the same. Nothing
    was asked about it. That is #98's defect inside a report, and it is the kind
    a mutation of this branch could not find: the tool is existing code, and a
    new call to a shared function is a change to that function.

    Tested through `main()` rather than a helper, because a green unit suite
    over a hook nothing calls is the failure this repository keeps repeating
    (#309).
    """

    def _run(self, app, monkeypatch, ids):
        import app as app_module
        import utils.refresh_property_accuracy as tool

        monkeypatch.setattr(tool, "create_app", lambda *a, **k: app)
        monkeypatch.setattr(
            app_module, "create_app", lambda *a, **k: app, raising=False
        )

        asked = []

        def _never_reaches_google(self, prop, refresh=False, **kwargs):
            asked.append(prop.id)
            return True

        monkeypatch.setattr(
            tool.PropertyLocationService, "ensure_coordinates", _never_reaches_google
        )

        records = []
        monkeypatch.setattr(
            tool.logger, "info", lambda msg, *a: records.append(msg % a if a else msg)
        )

        # `main()` reads `sys.argv` rather than taking an argv, so the call is
        # made the way the shell makes it.
        monkeypatch.setattr(
            "sys.argv",
            [
                "refresh_property_accuracy",
                "--ids",
                ",".join(str(i) for i in ids),
                "--sleep",
                "0",
            ],
        )
        tool.main()
        return asked, records

    def test_a_hand_set_row_is_skipped_and_named(self, app, monkeypatch):
        with app.app_context():
            hand = _row()
            set_location_by_hand(
                hand, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            ordinary = _row(source_email_id="geo002:ordinary")

            asked, records = self._run(app, monkeypatch, [hand.id, ordinary.id])

            assert hand.id not in asked, "the tool asked about a hand-set row"
            assert ordinary.id in asked, "the tool skipped a row it should re-geocode"

            said = "\n".join(records)
            assert f"id={hand.id}" in said and "set by hand" in said
            assert "1 skipped (location set by hand)" in said


class TestTheToolNamesADisagreement:
    """The block is provenance; the columns are the value; the score reads the
    columns. Nothing inside the app can move the columns of a hand-set row any
    more, but an out-of-band script still can -- the boundary
    `services/ingest_policy.py` records as uncloseable -- and this tool is the
    only window onto the result.
    """

    def test_a_moved_coordinate_is_called_out(self, app):
        from utils.set_property_location import _describe

        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )

            assert "DISAGREES" not in _describe(row)

            row.location_lat = 43.9
            db.session.commit()

            assert "DISAGREES" in _describe(row)

    def test_a_changed_accuracy_is_called_out(self, app):
        from utils.set_property_location import _describe

        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            row.location_accuracy = "approximate"
            db.session.commit()

            said = _describe(row)
            assert "DISAGREES" in said and "'approximate'" in said

    def test_a_row_with_no_block_says_a_refresh_may_overwrite_it(self, app):
        from utils.set_property_location import _describe

        with app.app_context():
            said = _describe(_row())

            assert "hand-set      no" in said
            assert "DISAGREES" not in said
