"""`municipality_check` compares municipalities, and says so (GEO-001, #265).

The key was written by a comparison of **provinces**: `_row_province` and
`_result_province` both reduce to the first two digits of a postal code, or to
`administrative_area_level_2`. Across provinces that works and #348 is the
proof. Inside one it cannot work at all -- every row in this database is
Asturias, so both sides read `33` whichever municipality the answer names, and
the key that every later reader sees said `municipality`.

Measured against production on 2026-08-19, 201 records read `agreed` on that
basis, and property 559 is among them: the row says Gijón, Google answered
`Valdornón, 33350 Municipality of Siero`, and the guard that would have caught
it was the one reporting success.

Two things are pinned here, and the second is the reason this is not a rename.

**What may be read as a municipality.** Reading `formatted_address` is not an
option: four of the five watched provinces have a capital whose municipality
carries the province's own name -- `match()` answers 15030 for "A Coruña",
27028 for "Lugo", 32054 for "Ourense", 36038 for "Pontevedra". Simulated over
the 725 production rows that carry a formatted address, splitting it on commas
produced 10 contradictions of which 6 were that collision and nothing else.
Components carry types; the province is `administrative_area_level_2` and is
never read.

**What may not be read as a disagreement.** A village inside the right council,
a name that resolves nowhere, two components that disagree with each other, a
municipality outside the five provinces the index covers -- each is its own
cannot-tell, and none of them is a contradiction (#98).
"""

import pytest

from app import create_app, db
from models import Property
from services.property_location_service import (
    CHECK_UNCHECKED,
    PropertyLocationService,
    _municipality_agreement,
    read_geocoding_checks,
)
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _prop(municipality, **kw):
    fields = dict(
        source_email_id=kw.pop("source_email_id", f"geo001-{municipality}"),
        title=f"Plot in {municipality}",
        municipality=municipality,
    )
    fields.update(kw)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


def _geo(components, formatted="somewhere", accuracy="approximate"):
    return {
        "formatted_address": formatted,
        "types": ["locality", "political"],
        "accuracy": accuracy,
        "lat": 43.5,
        "lng": -5.8,
        "address_components": [
            dict(types=list(types), long_name=name) for types, name in components
        ],
    }


# The two production answers this ticket was measured on. What was measured is
# the `formatted_address` string -- that is all the row stores; the component
# *types* below are reconstructed from Google's documented vocabulary, because
# `address_components` is never persisted and re-fetching it costs a billed
# call. Said plainly rather than left to read as a capture.
SIERO_UNDER_A_GIJON_ROW = _geo(
    [
        (["postal_code"], "33350"),
        (["locality", "political"], "Valdornón"),
        (["administrative_area_level_3", "political"], "Municipality of Siero"),
        (["administrative_area_level_2", "political"], "Asturias"),
    ],
    formatted="Valdornón, 33350 Municipality of Siero, Asturias, Spain",
)

LAS_REGUERAS_UNDER_A_CANDAMO_ROW = _geo(
    [
        (["postal_code"], "33829"),
        (["locality", "political"], "Llamero"),
        (["administrative_area_level_3", "political"], "Las Regueras"),
        (["administrative_area_level_2", "political"], "Asturias"),
    ],
    formatted="Llamero, 33829 Las Regueras, Asturias, Spain",
)


class TestItComparesMunicipalities:
    def test_the_two_production_rows_are_contradicted_not_agreed(self, app):
        """559 and 262, the ticket's own confirmed cases."""
        state, row, results = _municipality_agreement(
            _prop("Gijón"), SIERO_UNDER_A_GIJON_ROW
        )
        assert state == "contradicted"
        assert row == "33024"  # Gijón
        assert results == {"33066"}  # Siero

        state, row, results = _municipality_agreement(
            _prop("Candamo", source_email_id="geo001-262"),
            LAS_REGUERAS_UNDER_A_CANDAMO_ROW,
        )
        assert (state, row, results) == ("contradicted", "33010", {"33054"})

    def test_the_same_municipality_agrees(self, app):
        state, _, _ = _municipality_agreement(_prop("Siero"), SIERO_UNDER_A_GIJON_ROW)
        assert state == "agreed"

    def test_an_administrative_prefix_does_not_hide_the_name(self, app):
        """25 of 725 production answers say "Municipality of ...", and
        `match("Municipality of Siero")` is None while `match("Siero")` is
        33066 -- so without the strip this row reads cannot-tell."""
        state, _, results = _municipality_agreement(
            _prop("Siero"), SIERO_UNDER_A_GIJON_ROW
        )
        assert results == {"33066"} and state == "agreed"


class TestWhatMustNotBecomeADisagreement:
    def test_the_province_component_is_never_read_as_a_municipality(self, app):
        """The collision that makes `formatted_address` unusable: A Coruña is
        both a province and its capital's municipality (15030)."""
        answer = _geo(
            [
                (["postal_code"], "15359"),
                (["locality", "political"], "Balteiro"),
                (["administrative_area_level_2", "political"], "A Coruña"),
            ],
            formatted="Lugar Balteiro, 1, 15359 Balteiro, A Coruña, Spain",
        )
        state, _, results = _municipality_agreement(_prop("Cedeira"), answer)
        assert results == set(), "the province is not a municipality candidate"
        assert state == "result_names_no_municipality"

    def test_a_village_inside_the_right_council_abstains(self, app):
        answer = _geo(
            [
                (["postal_code"], "33347"),
                (["locality", "political"], "Bones"),
                (["administrative_area_level_2", "political"], "Asturias"),
            ]
        )
        state, _, _ = _municipality_agreement(_prop("Ribadesella"), answer)
        assert state == "result_names_no_municipality"

    def test_a_name_from_another_province_is_a_collision_not_a_verdict(self, app):
        """The index spans five provinces, so a village called "Mieres" in A
        Coruña resolves to Asturias' municipality. The result's own province
        settles it."""
        answer = _geo(
            [
                (["postal_code"], "15100"),
                (["locality", "political"], "Mieres"),
                (["administrative_area_level_2", "political"], "A Coruña"),
            ]
        )
        state, _, results = _municipality_agreement(
            _prop("Carballo", source_email_id="geo001-collision"), answer
        )
        assert results == set()
        assert state == "result_names_no_municipality"

    def test_two_components_that_disagree_are_not_a_coin_flip(self, app):
        answer = _geo(
            [
                (["postal_code"], "33528"),
                (["locality", "political"], "Nava"),
                (["administrative_area_level_3", "political"], "Siero"),
                (["administrative_area_level_2", "political"], "Asturias"),
            ]
        )
        state, _, results = _municipality_agreement(_prop("Gijón"), answer)
        assert results == {"33040", "33066"}
        assert state == "result_names_several"

    def test_a_row_outside_the_index_cannot_be_compared(self, app):
        """The archived Alicante subscriptions hold real listings."""
        state, row, _ = _municipality_agreement(
            _prop("Sant Joan d'Alacant"), SIERO_UNDER_A_GIJON_ROW
        )
        assert (state, row) == ("row_unmatched", None)

    def test_an_answer_with_no_components_cannot_be_compared(self, app):
        state, _, _ = _municipality_agreement(
            _prop("Siero"), {"lat": 43.5, "lng": -5.8}
        )
        assert state == "result_names_no_municipality"

    def test_the_portal_alias_table_is_not_applied_to_the_answer(self, app):
        """`ALIASES` is verified in one direction only.

        "San Esteban" is what Idealista calls the capital of Muros de Nalón
        (33039) -- and a real parish of Morcín (33038) in its own right. Read
        off a geocoder's answer through the alias table, a correct result for
        Morcín becomes a code for a council 40 km away, and this check calls
        it `contradicted`.
        """
        answer = _geo(
            [
                (["postal_code"], "33163"),
                (["locality", "political"], "San Esteban"),
                (["administrative_area_level_2", "political"], "Asturias"),
            ]
        )
        state, row, results = _municipality_agreement(
            _prop("Morcín", source_email_id="geo001-alias"), answer
        )
        assert row == "33038"
        assert results == set(), "an alias source string is not the answer's name"
        assert state == "result_names_no_municipality"

    def test_an_answer_naming_no_province_resolves_nothing(self, app):
        """The collision guard needs a province to check against.

        Without one it used to pass every candidate through, which is the
        Mieres collision with the guard switched off.
        """
        answer = _geo([(["locality", "political"], "Mieres")])
        state, _, results = _municipality_agreement(
            _prop("Carballo", source_email_id="geo001-noprov"), answer
        )
        assert results == set()
        assert state == "result_names_no_municipality"

    def test_an_unindexed_row_is_reported_before_an_unresolvable_answer(self, app):
        """Both sides fail; the row's own failure is the one reported."""
        state, row, results = _municipality_agreement(
            _prop("Sant Joan d'Alacant", source_email_id="geo001-bothfail"),
            {"lat": 43.5, "lng": -5.8},
        )
        assert (state, row, results) == ("row_unmatched", None, set())


class TestEveryComponentTypeTheGuardDeclares:
    """All four are load bearing, so all four are exercised.

    Two of them had no fixture at all: deleting `postal_town` and
    `administrative_area_level_4` from the tuple left the whole suite green.
    """

    def _codes(self, kind, name="Siero"):
        from services.property_location_service import _result_municipality_codes

        return _result_municipality_codes(
            _geo(
                [
                    (["postal_code"], "33510"),
                    ([kind, "political"], name),
                    (["administrative_area_level_2", "political"], "Asturias"),
                ]
            )
        )

    @pytest.mark.parametrize(
        "kind",
        [
            "locality",
            "postal_town",
            "administrative_area_level_3",
            "administrative_area_level_4",
        ],
    )
    def test_a_municipality_under_any_declared_type_is_read(self, app, kind):
        assert self._codes(kind) == {"33066"}

    @pytest.mark.parametrize(
        "kind", ["administrative_area_level_1", "administrative_area_level_2"]
    )
    def test_the_community_and_province_levels_are_never_read(self, app, kind):
        assert self._codes(kind, name="Lugo") == set()


class TestTheRecordOnTheRow:
    def _service(self, answer):
        service = PropertyLocationService()
        service.geocoding_service.geocode_address = lambda address: answer
        return service

    def _stored(self, prop_id):
        db.session.expire_all()
        return db.session.get(Property, prop_id).enrichment["geocoding"]

    def test_both_checks_are_written_under_their_own_names(self, app):
        prop = _prop("Gijón")
        assert self._service(SIERO_UNDER_A_GIJON_ROW).ensure_coordinates(prop) is True
        db.session.commit()

        record = self._stored(prop.id)
        assert record["province_check"] == "agreed", "33 is still 33"
        assert record["municipality_check"] == "contradicted"

    def test_a_disagreement_carries_the_codes_so_nobody_re_geocodes_to_see(self, app):
        prop = _prop("Gijón", source_email_id="geo001-codes")
        self._service(SIERO_UNDER_A_GIJON_ROW).ensure_coordinates(prop)
        db.session.commit()

        record = self._stored(prop.id)
        assert record["row_municipality"] == "33024"
        assert record["result_municipalities"] == ["33066"]

    def test_a_disagreement_does_not_refuse_the_coordinate(self, app):
        """The province contradiction refuses and still does; this one records.

        Measured on production 2026-08-19: refusing here would have thrown
        away a *precise* street-level result (property 80, "Barrio Candín, 11"
        in Langreo under a row that says Siero), and `Property.municipality` is
        free text off an alert email.
        """
        prop = _prop("Gijón", source_email_id="geo001-not-refused")
        assert self._service(SIERO_UNDER_A_GIJON_ROW).ensure_coordinates(prop) is True
        db.session.commit()

        stored = db.session.get(Property, prop.id)
        assert stored.location_lat is not None
        assert "refused" not in self._stored(prop.id)

    def test_agreement_is_written_when_it_is_earned(self, app):
        prop = _prop("Siero", source_email_id="geo001-earned")
        self._service(SIERO_UNDER_A_GIJON_ROW).ensure_coordinates(prop)
        db.session.commit()

        record = self._stored(prop.id)
        assert record["municipality_check"] == "agreed"
        assert "row_municipality" not in record, "codes only on the interesting one"


class TestReadingRecordsWrittenBefore:
    """201 production records say `agreed` and mean a province agreed."""

    def test_a_legacy_record_is_a_province_verdict_and_an_unchecked_municipality(
        self,
    ):
        assert read_geocoding_checks({"municipality_check": "agreed"}) == {
            "province": "agreed",
            "municipality": CHECK_UNCHECKED,
        }

    def test_the_retired_state_name_is_folded_in_the_reader_not_in_the_table(self):
        """`result_has_no_postcode` is the pre-#371 name of one state; 115
        production records still carry it."""
        assert (
            read_geocoding_checks({"municipality_check": "result_has_no_postcode"})[
                "province"
            ]
            == "result_has_no_province"
        )

    def test_a_current_record_is_read_literally(self):
        assert read_geocoding_checks(
            {"province_check": "agreed", "municipality_check": "contradicted"}
        ) == {"province": "agreed", "municipality": "contradicted"}

    def test_a_record_with_neither_says_nobody_looked(self):
        for record in ({}, {"query": "x"}, None, "not a dict"):
            assert read_geocoding_checks(record) == {
                "province": CHECK_UNCHECKED,
                "municipality": CHECK_UNCHECKED,
            }
