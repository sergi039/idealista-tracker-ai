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
was defended only by accident, because it happens to also carry a portal pin,
and 130 of the 132 `precise` rows carry none (15:02Z -- re-measure rather than
quoting it). 792 gained such a pin the same afternoon, from a hand-run script
that wrote its cadastre conclusion under `source: cadastre_parcel` into the
field meaning "what the portal published" -- which defends the row and is the
STATUS-002 mistake this change exists to give an honest alternative to.

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
                # Billed geocoding: the tool will not start without a reason.
                "--reason",
                "pytest: hand-set rows survive a refresh",
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


class TestTheGuardIsRecheckedUnderTheLock:
    """The race this change reopened, found by adversarial review and
    reproduced by every refuter that looked at it.

    `ensure_coordinates` reads `manual_coordinate` once, *before* the geocode,
    which the file's own #400 history documents as taking minutes. The write
    then happens inside `locked_write`, which re-reads the row FOR UPDATE --
    and `_apply_geocode_outcome` re-derives `portal_coordinate` there, with a
    docstring explaining exactly why ("an operator who wrote a better pin while
    the geocode ran must not have it replaced"), while never re-deriving
    `manual_coordinate`. The two live under different keys, so the portal
    re-check protects nothing here.

    So a hand-set location committed while the geocode was in flight was
    overwritten by a candidate chosen against a row that did not yet have one --
    #339 and #400 again, in the mechanism this change introduced to outrank
    them both.
    """

    def test_a_hand_set_location_that_lands_mid_geocode_is_not_overwritten(self, app):
        with app.app_context():
            row = _row(location_accuracy="approximate")

            class _GeocoderThatRacesUs:
                """Stands in for Google, and for the operator who presses the
                button while Google is thinking."""

                def __init__(self):
                    self.queries = []

                def geocode_address(self, query):
                    self.queries.append(query)
                    if len(self.queries) == 1:
                        # The concurrent write, committed inside the window
                        # `ensure_coordinates` leaves open on purpose.
                        set_location_by_hand(
                            row,
                            lat=HAND_LAT,
                            lon=HAND_LON,
                            accuracy="precise",
                            note=NOTE,
                            commit=True,
                        )
                    return _google_says("approximate")

            geocoder = _GeocoderThatRacesUs()
            PropertyLocationService(geocoder).ensure_coordinates(
                row, refresh=True, commit=True
            )

            db.session.expire_all()
            stored = db.session.get(Property, row.id)
            assert geocoder.queries, "the geocode did not run; the race is untested"
            assert stored.location_accuracy == "precise"
            assert float(stored.location_lat) == pytest.approx(HAND_LAT)
            assert manual_coordinate(stored) is not None

    def test_a_refusal_mid_geocode_does_not_unlocate_the_hand_set_row(self, app):
        """The other path into the locked tail: a refresh that answers nothing
        clears the columns, and must not clear a location that arrived since."""
        with app.app_context():
            row = _row(location_accuracy="approximate")

            class _SilentButRacing:
                def __init__(self):
                    self.queries = []

                def geocode_address(self, query):
                    self.queries.append(query)
                    if len(self.queries) == 1:
                        set_location_by_hand(
                            row,
                            lat=HAND_LAT,
                            lon=HAND_LON,
                            accuracy="precise",
                            note=NOTE,
                            commit=True,
                        )
                    return None

            PropertyLocationService(_SilentButRacing()).ensure_coordinates(
                row, refresh=True, commit=True
            )

            db.session.expire_all()
            stored = db.session.get(Property, row.id)
            assert stored.location_lat is not None
            assert stored.location_accuracy == "precise"


class TestZeroIsAPlace:
    """`0, 0` is the Gulf of Guinea, and this repository already says so twice:
    `record_portal_coordinate`'s docstring refuses to write it as a stand-in for
    "no pin", and `PropertyEnrichmentService.enrich_property` reads the columns
    with `is None, not truthiness` for exactly this reason.

    The refusal added here used `bool(lat and lon)`, which reads a real
    coordinate on the equator as an absent one.
    """

    def test_a_hand_set_row_at_zero_reports_that_it_has_a_coordinate(self, app):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=0.0, lon=0.0, accuracy="precise", note="the null island"
            )
            geocoder = _Geocoder([_google_says("precise")])

            assert (
                PropertyLocationService(geocoder).ensure_coordinates(row, refresh=True)
                is True
            )
            assert geocoder.queries == []


class TestARollbackDoesNotDiscardCurationDoneAfterIt:
    """`utils/refresh_property_accuracy.py --restore` writes `enrichment`
    verbatim from a snapshot. A snapshot taken before a re-geocode run holds
    the geocoder-era value, so restoring it after somebody curated the row
    silently erases both the hand-set block and the coordinate it established.

    `utils/restore_score_snapshot.py` already settled how this repository
    answers that: rows the snapshot cannot speak for are **named**, not
    quietly overwritten. Same answer here, and narrowly -- a snapshot that
    *does* carry the hand-set block restores normally, because then the
    rollback really is putting back what a person had.
    """

    def _snapshot(self, tmp_path, rows):
        import json

        path = tmp_path / "snap.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return str(path)

    def test_a_hand_set_row_absent_from_the_snapshot_is_skipped_and_named(
        self, app, tmp_path, caplog
    ):
        import logging

        from utils.refresh_property_accuracy import _restore, _snapshot_row

        with app.app_context():
            row = _row(
                location_lat=GOOGLE_LAT,
                location_lon=GOOGLE_LON,
                location_accuracy="approximate",
            )
            taken = [_snapshot_row(row)]
            path = self._snapshot(tmp_path, taken)

            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )

            with caplog.at_level(logging.WARNING):
                restored = _restore(path)

            db.session.expire_all()
            stored = db.session.get(Property, row.id)
            assert restored == 0
            assert stored.location_accuracy == "precise"
            assert float(stored.location_lat) == pytest.approx(HAND_LAT)
            assert manual_coordinate(stored) is not None
            assert str(row.id) in caplog.text and "hand" in caplog.text.lower()

    def test_a_snapshot_that_holds_the_block_restores_normally(self, app, tmp_path):
        from utils.refresh_property_accuracy import _restore, _snapshot_row

        with app.app_context():
            row = _row(location_accuracy="approximate")
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            path = self._snapshot(tmp_path, [_snapshot_row(row)])

            row.location_lat = 43.9
            row.location_accuracy = "unknown"
            db.session.commit()

            assert _restore(path) == 1

            db.session.expire_all()
            stored = db.session.get(Property, row.id)
            assert stored.location_accuracy == "precise"
            assert float(stored.location_lat) == pytest.approx(HAND_LAT)

    def test_an_ordinary_row_still_restores(self, app, tmp_path):
        from utils.refresh_property_accuracy import _restore, _snapshot_row

        with app.app_context():
            row = _row(location_accuracy="approximate")
            path = self._snapshot(tmp_path, [_snapshot_row(row)])
            row.location_accuracy = "unknown"
            db.session.commit()

            assert _restore(path) == 1

            db.session.expire_all()
            assert db.session.get(Property, row.id).location_accuracy == "approximate"


class TestTheApiSaysWhenItRefusedTheRefresh:
    """The CLI in this same change names the rows it skipped. The endpoint
    accepted `refresh_coords`, did not perform it, and said nothing -- and a
    caller told nothing reads the silence as "done"."""

    def _post(self, client, prop_id, refresh):
        return client.post(
            f"/api/property/{prop_id}/enrich" + ("?refresh_coords=1" if refresh else "")
        )

    def test_it_names_the_refusal_and_does_not_pass_the_flag_on(self, app, monkeypatch):
        seen = {}

        def _fake(self, prop, refresh_coords=False, recalc_scoring=True):
            seen["refresh_coords"] = refresh_coords
            return True

        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            prop_id = row.id

        from services.property_enrichment_service import PropertyEnrichmentService

        monkeypatch.setattr(PropertyEnrichmentService, "enrich_property", _fake)

        with app.test_client() as client:
            body = self._post(client, prop_id, refresh=True).get_json()

        assert body.get("coordinate_refresh") == "refused_hand_set"
        assert "set by hand" in (body.get("message") or "")
        assert seen.get("refresh_coords") is False

    def test_an_ordinary_row_is_unchanged(self, app, monkeypatch):
        seen = {}

        def _fake(self, prop, refresh_coords=False, recalc_scoring=True):
            seen["refresh_coords"] = refresh_coords
            return True

        with app.app_context():
            prop_id = _row().id

        from services.property_enrichment_service import PropertyEnrichmentService

        monkeypatch.setattr(PropertyEnrichmentService, "enrich_property", _fake)

        with app.test_client() as client:
            body = self._post(client, prop_id, refresh=True).get_json()

        assert body.get("coordinate_refresh") is None
        assert seen.get("refresh_coords") is True


class TestTheToolCanJustLook:
    """`--id` on its own describes the row.

    The tool shipped without this and was described in a message to another
    session as having it. Running the command that message gave answered
    `error: --lat, --lon, --accuracy, --note required` -- caught only by
    running it on production instead of rereading the code that had just been
    written.

    It matters beyond the embarrassment: `_describe` is the only window onto a
    hand-set block whose columns have drifted from it, and requiring the caller
    to invent the coordinate they are *not* setting in order to look at the one
    that is there made the window unreachable.
    """

    def _run(self, app, monkeypatch, argv, records=None):
        import app as app_module
        import utils.set_property_location as tool

        # `main()` imports `create_app` from `app` when it runs, so the patch
        # goes on the source module rather than on the tool.
        monkeypatch.setattr(app_module, "create_app", lambda *a, **k: app)
        said = records if records is not None else []
        monkeypatch.setattr(
            tool.logger, "info", lambda msg, *a: said.append(msg % a if a else msg)
        )
        code = tool.main(argv)
        return code, "\n".join(said)

    def test_a_bare_id_describes_the_row_and_writes_nothing(self, app, monkeypatch):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            prop_id = row.id

            code, said = self._run(app, monkeypatch, ["--id", str(prop_id)])

            assert code == 0
            assert "hand-set" in said and NOTE[:20] in said
            assert "Would set" not in said and "Would clear" not in said

            stored = db.session.get(Property, prop_id)
            assert float(stored.location_lat) == pytest.approx(HAND_LAT)
            assert stored.location_accuracy == "precise"

    def test_it_names_a_drift_the_row_cannot_report_itself(self, app, monkeypatch):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )
            row.location_lat = 43.9
            db.session.commit()

            _, said = self._run(app, monkeypatch, ["--id", str(row.id)])

            assert "DISAGREES" in said

    def test_a_partial_set_of_arguments_is_still_an_error(self, app, monkeypatch):
        with app.app_context():
            prop_id = _row().id

        with pytest.raises(SystemExit):
            self._run(
                app,
                monkeypatch,
                ["--id", str(prop_id), "--lat", str(HAND_LAT), "--lon", str(HAND_LON)],
            )

    def test_apply_with_nothing_to_apply_is_an_error(self, app, monkeypatch):
        with app.app_context():
            prop_id = _row().id

        with pytest.raises(SystemExit):
            self._run(app, monkeypatch, ["--id", str(prop_id), "--apply"])

    def test_a_row_with_no_block_still_describes(self, app, monkeypatch):
        with app.app_context():
            prop_id = _row().id

            code, said = self._run(app, monkeypatch, ["--id", str(prop_id)])

            assert code == 0
            assert "hand-set      no" in said


class TestTheToolNamesTheGuardThatActuallyApplies:
    """ "No hand-set block" and "a refresh will overwrite this row" are two
    different facts, and the tool printed the second on the strength of the
    first.

    Measured on production 2026-08-20, the claim was false for exactly the two
    rows this tool exists for. 161 and 792 carry an `enrichment["import"]
    ["coordinate"]`, so `_apply_geocode_outcome` re-reads it under the lock and
    `improves_on` refuses to trade a `precise` for anything a geocode can
    answer -- a refresh leaves them alone. The report said they were about to
    be overwritten.

    It is the defect this whole change is about, in the change's own reporting:
    a consequence asserted from a guard that was never consulted.
    """

    def _describe(self, prop):
        from utils.set_property_location import _describe

        return _describe(prop)

    def _with_pin(self, source, accuracy="precise"):
        from services.coordinate_quality import record_portal_coordinate

        row = _row(location_accuracy=accuracy)
        row.enrichment = record_portal_coordinate(
            row.enrichment, source=source, lat=HAND_LAT, lon=HAND_LON
        )
        db.session.commit()
        return row

    def test_a_row_with_nothing_at_all_is_named_exposed(self, app):
        with app.app_context():
            said = self._describe(_row())

            assert "nothing defends this row" in said
            assert "EXPOSED" in said

    def test_a_precise_row_behind_a_portal_pin_is_not_called_exposed(self, app):
        with app.app_context():
            said = self._describe(self._with_pin("fotocasa"))

            assert "EXPOSED" not in said
            assert "defended by" in said
            assert "MISFILED" not in said

    def test_an_approximate_row_behind_a_pin_is_still_exposed_to_a_precise_answer(
        self, app
    ):
        """The pin is kept only against a geocode that is no better."""
        with app.app_context():
            said = self._describe(self._with_pin("fotocasa", accuracy="approximate"))

            assert "EXPOSED" in said
            assert "defended by" not in said

    def test_a_pin_whose_source_is_not_a_portal_is_named(self, app):
        """The production state of 161 and 792: a cadastre conclusion stored in
        the field meaning "what the source site published"."""
        with app.app_context():
            said = self._describe(self._with_pin("cadastre_parcel"))

            assert "MISFILED" in said
            assert "cadastre_parcel" in said
            # It still works, so the row is genuinely defended -- both facts.
            assert "defended by" in said

    def test_a_hand_set_row_says_none_of_this(self, app):
        with app.app_context():
            row = _row()
            set_location_by_hand(
                row, lat=HAND_LAT, lon=HAND_LON, accuracy="precise", note=NOTE
            )

            said = self._describe(row)

            assert "EXPOSED" not in said and "MISFILED" not in said
            assert "hand-set" in said and NOTE[:20] in said
