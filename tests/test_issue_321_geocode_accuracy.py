"""`location_accuracy` records what Google said, not what the query looked like.

Issue #321. `_geocode_with_accuracy` used to label a result from the *shape of
the query string* -- "precise" when the title had two or more comma-separated
parts -- call the geocoder, receive the real `location_type`-derived accuracy,
and return its own guess instead. Measured on production 2026-08-15: 141 of the
182 rows labelled `precise` were labelled by that guess, and 81 confident
sea-view verdicts rested on them, because `services/sea_view_service.py:580`
skips the elevation profile for anything that is not `precise`.

That guess is not worthless -- it decides which address to try first and which
results the duplicate check applies to -- so it is kept, under the name it
deserves (`specificity`), and kept out of the stored accuracy.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Land, Property
from services.enrichment_service import EnrichmentService
from tests import setup_test_environment
from utils.refresh_property_accuracy import _is_legacy_labelled


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


@pytest.fixture
def service():
    return EnrichmentService()


def _land(title="Land in La Faza, 280, Caldones, Gijón 85,000 €", **kw):
    land = Land(
        source_email_id=kw.pop("source_email_id", "issue_321"), title=title, **kw
    )
    db.session.add(land)
    db.session.commit()
    return land


def _geocode(service, land, answer):
    """Run the geocoder with `answer` as Google's reply to every address."""
    with patch.object(
        service.geocoding_service, "geocode_address", return_value=answer
    ):
        with patch.object(service, "_is_duplicate_coordinates", return_value=False):
            return service._geocode_with_accuracy(land)


class TestTheStoredAccuracyIsGooglesAnswer:
    def test_a_specific_looking_query_does_not_make_the_result_precise(
        self, app, service
    ):
        """The title has four parts; the old code called that "precise"."""
        with app.app_context():
            land = _land()
            result = _geocode(
                service,
                land,
                {"lat": 43.5, "lng": -5.65, "accuracy": "approximate"},
            )
            assert result is not None
            assert result["accuracy"] == "approximate"

    def test_a_rooftop_answer_is_recorded_as_precise(self, app, service):
        with app.app_context():
            land = _land(source_email_id="issue_321_rooftop")
            result = _geocode(
                service, land, {"lat": 43.5, "lng": -5.65, "accuracy": "precise"}
            )
            assert result is not None
            assert result["accuracy"] == "precise"

    def test_an_answer_that_carries_no_accuracy_is_unknown(self, app, service):
        """Silence is not a licence to guess.

        A geocoder reply with no accuracy field means nobody classified this
        point. Recording the query's own shape here would be the #98 defect
        inverted -- an unverified value stored as a verified one.
        """
        with app.app_context():
            land = _land(source_email_id="issue_321_silent")
            result = _geocode(service, land, {"lat": 43.5, "lng": -5.65})
            assert result is not None
            assert result["accuracy"] == "unknown"

    def test_an_unrecognised_accuracy_is_not_passed_through(self, app, service):
        with app.app_context():
            land = _land(source_email_id="issue_321_junk")
            result = _geocode(
                service, land, {"lat": 43.5, "lng": -5.65, "accuracy": "ROOFTOP-ish"}
            )
            assert result is not None
            assert result["accuracy"] == "unknown"


class TestTheQueryShapeKeepsItsRealJob:
    def test_the_duplicate_check_still_keys_on_the_query_not_the_answer(
        self, app, service
    ):
        """A specific query landing on someone else's point is still refused.

        This is why the guess is kept rather than deleted: it says "this address
        was specific enough that a shared coordinate means the specific part was
        ignored". Google calling the result `approximate` does not change that,
        so the duplicate check must not start reading the answer instead.
        """
        with app.app_context():
            land = _land(source_email_id="issue_321_dup")
            calls = []

            def fake_geocode(address):
                calls.append(address)
                return {"lat": 43.5, "lng": -5.65, "accuracy": "approximate"}

            # Every candidate point is a duplicate, so every "precise"-looking
            # query is skipped and only a loosely-specific one can return.
            with patch.object(
                service.geocoding_service, "geocode_address", side_effect=fake_geocode
            ):
                with patch.object(
                    service, "_is_duplicate_coordinates", return_value=True
                ):
                    result = service._geocode_with_accuracy(land)

            assert len(calls) > 1, "the loop must move on past a duplicate"
            if result is not None:
                # Whatever came back did so through a non-specific attempt, and
                # still carries Google's accuracy rather than that attempt's.
                assert result["accuracy"] == "approximate"


class TestWhoTheRepairToolPaysFor:
    """The scope predicate decides what a paid backfill spends on.

    `enrichment["geocoding"]` is written only by PropertyLocationService, which
    stores Google's own verdict, so its presence means the label was measured
    and the row must be left alone. Getting this wrong costs money on rows that
    were never broken, and — worse — would overwrite good labels.
    """

    def _prop(self, **enrichment):
        return Property(
            source_email_id=f"issue_321_{len(enrichment)}_{id(enrichment)}",
            title="Chalet in Somewhere",
            enrichment=enrichment or None,
        )

    def test_a_migrated_row_with_no_geocoding_record_is_in_scope(self, app):
        with app.app_context():
            assert _is_legacy_labelled(self._prop(legacy_land={"id": 5})) is True

    def test_a_row_the_property_path_geocoded_is_left_alone(self, app):
        with app.app_context():
            prop = self._prop(
                legacy_land={"id": 5},
                geocoding={"query": "Luarca, Spain", "accuracy": "approximate"},
            )
            assert _is_legacy_labelled(prop) is False

    def test_a_row_that_was_never_migrated_is_left_alone(self, app):
        with app.app_context():
            assert _is_legacy_labelled(self._prop()) is False

    def test_a_geocoding_key_that_is_not_a_record_does_not_count_as_measured(self, app):
        """A truthy-but-wrong value must not silently exclude a broken row."""
        with app.app_context():
            prop = self._prop(legacy_land={"id": 5}, geocoding="yes")
            assert _is_legacy_labelled(prop) is True


class TestTheIdsOption:
    """`--ids` names the rows a paid run will touch, so it must not lose one.

    It exists because the default scope cannot reach issue #331's eight rows:
    they are not legacy-labelled and they already carry a geocoding record.
    """

    def test_ids_are_parsed_in_the_order_given(self):
        from utils.refresh_property_accuracy import _parse_ids

        assert _parse_ids("115, 116,117") == [115, 116, 117]

    def test_blank_entries_are_ignored(self):
        from utils.refresh_property_accuracy import _parse_ids

        assert _parse_ids("115,,116,") == [115, 116]

    def test_a_non_integer_stops_the_run_instead_of_being_skipped(self):
        """A silently dropped id makes a paid run report success over a
        smaller set than the caller asked for."""
        from utils.refresh_property_accuracy import _parse_ids

        with pytest.raises(SystemExit):
            _parse_ids("115,oops,117")

    def test_naming_nothing_stops_the_run(self):
        from utils.refresh_property_accuracy import _parse_ids

        with pytest.raises(SystemExit):
            _parse_ids(" , ")


class TestARefusalIsAResultNotAFailure:
    """Measured defect, first #331 repair run: 4 of 8 rows kept the fake point.

    The tool rolled back on every `ok is False`, which threw away both the
    nulled coordinates and the refusal record `PropertyLocationService` had
    just written. The log read "4 could not be geocoded" while those rows still
    said 40.463667,-3.749220 -- a run that reported doing nothing, and had in
    fact undone its own work.

    A transient failure still rolls back: there the old coordinates are the
    best thing known about the row.
    """

    def _row(self, **enrichment):
        prop = Property(
            source_email_id=f"persist_{len(enrichment)}_{id(enrichment)}",
            title="Finca offers for",
            location_lat=40.463667,
            location_lon=-3.749220,
            location_accuracy="approximate",
            enrichment=enrichment or None,
        )
        db.session.add(prop)
        db.session.commit()
        return prop

    def test_a_refusal_is_committed_so_the_fake_point_really_goes(self, app):
        from utils.refresh_property_accuracy import _persist_outcome

        with app.app_context():
            prop = self._row()
            # What ensure_coordinates leaves behind when every candidate is
            # refused: no coordinates, and a record saying why.
            prop.location_lat = None
            prop.location_lon = None
            prop.location_accuracy = "unknown"
            prop.enrichment = {
                "geocoding": {
                    "query": "Finca offers for, Spain",
                    "formatted_address": "Spain",
                    "accuracy": "unknown",
                    "refused": "result_too_coarse",
                    "result_types": ["country", "political"],
                }
            }

            assert _persist_outcome(prop, False) == "refused"

            db.session.expire_all()
            stored = db.session.get(Property, prop.id)
            assert stored.location_lat is None
            assert stored.enrichment["geocoding"]["refused"] == "result_too_coarse"

    def test_a_transient_failure_keeps_the_old_coordinates(self, app):
        from utils.refresh_property_accuracy import _persist_outcome

        with app.app_context():
            prop = self._row()
            prop.location_lat = None
            prop.location_lon = None  # what refresh=True nulls before trying

            assert _persist_outcome(prop, False) == "failed"

            db.session.expire_all()
            stored = db.session.get(Property, prop.id)
            assert stored.location_lat is not None

    def test_a_record_without_the_marker_is_not_a_refusal(self, app):
        from utils.refresh_property_accuracy import _was_refused

        with app.app_context():
            prop = self._row(geocoding={"query": "x", "accuracy": "approximate"})
            assert _was_refused(prop) is False
