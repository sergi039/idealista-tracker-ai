"""`precise` is earned only by an answer to the address that was asked (#535).

`location_accuracy = precise` is Google's ROOFTOP and buys zero slack in
`services/coordinate_quality.py`, so every derived measurement on such a row is
scored as the parcel's. Measured on production 2026-09-02: 152 of the 166
`precise` rows were the geocoder's, not one carries a second coordinate to
check against, and the one ROOFTOP a person checked (row 360) was 2868 m out.
What the stored record can say for free is whether the ROOFTOP answers the
address the query named, and for 23 of the 152 it did not.

Pinned here: the house-number comparison of `services/address_agreement`,
at the geocoder's write and in `utils/audit_precise_accuracy.py`. Every
query and answer below is a production row's own, named by id.

Two blind spots are pinned as *passes*, on purpose, so nobody reads a green
check as verification: a same-number answer somewhere else (360), and a
different street with the same number (25).
"""

import json

import pytest

from app import create_app, db
from models import Property
from services.address_agreement import (
    AGREED,
    DIFFERENT_NUMBER,
    NO_NUMBER_ANSWERED,
    NUMBER_NOT_ASKED,
    answered_house_number,
    earns_precise,
    house_number_agreement,
    house_number_in_formatted,
    query_house_numbers,
)
from services.coordinate_quality import (
    record_manual_coordinate,
    record_portal_coordinate,
)
from services.property_location_service import PropertyLocationService
from tests import setup_test_environment
from utils.audit_precise_accuracy import (
    ACTOR,
    main,
    person_established,
    read_verdict,
    withdraw,
)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


class _Geocoder:
    """Stands in for Google, and records what it was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.queries = []

    def geocode_address(self, query):
        self.queries.append(query)
        return self.answers.pop(0) if self.answers else None


def _answer(
    formatted,
    *,
    postal,
    number=None,
    location_type="ROOFTOP",
    accuracy="precise",
    lat=43.32,
    lng=-8.24,
):
    """A geocoder answer in the shape `utils/geocoding.py` returns.

    The `street_number` component is what the live check reads; the postal
    code keeps the province check agreeing, so nothing here is refused for a
    reason this ticket is not about.
    """
    components = [{"types": ["postal_code"], "long_name": postal}]
    if number is not None:
        components.insert(0, {"types": ["street_number"], "long_name": number})
    return {
        "lat": lat,
        "lng": lng,
        "accuracy": accuracy,
        "location_type": location_type,
        "formatted_address": formatted,
        "types": ["street_address"],
        "address_components": components,
    }


_seq = iter(range(1, 10_000))


def _row(title, municipality, **kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", f"535-{next(_seq)}"),
        title=title,
        municipality=municipality,
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _geocode(row, answer, *, refresh=False):
    service = PropertyLocationService()
    service.geocoding_service = _Geocoder([answer])
    assert service.ensure_coordinates(row, refresh=refresh) is True
    return service.geocoding_service.queries, row.enrichment["geocoding"]


class TestWhatTheQueryAsked:
    @pytest.mark.parametrize(
        "query,expected",
        [
            # 360: the number is there.
            ("Barrio de Prendonés, 1, El Franco, Spain", {"1"}),
            # 1379: the listing gave no number.
            ("venta en calle Fiobre, Bergondo, Spain", set()),
            # 45: the road code's 6 is not a house number; 24 is.
            ("SI-6, 24, Viella-Granda-Meres, Siero, Spain", {"24"}),
            # 246: a letter after the number is not part of it.
            ("Lugar Pite, 3 a, Cambre, Spain", {"3a"}),
            # 412: a number written without a comma is still a number.
            ("Barrio Otero 15, Albandi, Carreño, Spain", {"15"}),
            # 894: nor does a slash or a parenthesis hide one.
            ("Bañugues / Lugar el Pueblo 128 (Gozón), Spain", {"128"}),
            # 661: sin número names no number.
            ("casadoiro s/n, Navia, Spain", set()),
            # A postal code is five digits and never a house number.
            ("15165 Bergondo, Spain", set()),
            # A price fragment is not a house number either.
            ("Finca 1.500, Navia, Spain", set()),
            (None, set()),
            # From the independent review of #556: a number inside a
            # street's name is not a number the query asked for.
            ("Avenida 8 de Marzo, 5, Madrid, Spain", {"5"}),
            ("Calle 2 de Mayo, Gijón, Spain", set()),
            # A floor is not a house number; a range names its first number.
            ("Calle Real, 2 Planta, Gijón, Spain", set()),
            ("Calle Mayor, 12-14, Madrid, Spain", {"1214"}),
        ],
    )
    def test_production_queries(self, query, expected):
        assert query_house_numbers(query) == expected

    def test_a_number_in_the_street_name_does_not_vouch_for_the_answer(self):
        """The reviewer's failing input: house 5 was asked on "8 de Marzo",
        Google answered house 8, and the 8 of the street name must not read
        as agreement."""
        assert (
            house_number_agreement("Avenida 8 de Marzo, 5, Madrid, Spain", "8")
            == DIFFERENT_NUMBER
        )


class TestWhatTheAnswerNamed:
    def test_the_typed_component_is_read(self):
        geo = _answer(
            "Rua Fiobre, 100, 15165 A Coruña, Spain", postal="15165", number="100"
        )
        assert answered_house_number(geo) == "100"

    def test_a_letter_suffix_travels_with_the_number(self):
        geo = _answer(
            "SI-6, 24b, 33199 Fozana, Asturias, Spain", postal="33199", number="24b"
        )
        assert answered_house_number(geo) == "24b"

    def test_an_answer_with_no_components_is_read_from_its_string(self):
        """The reviewer's input: a ROOFTOP carrying no components at all must
        not read as "no number" and keep `precise` on nothing."""
        geo = {
            "accuracy": "precise",
            "formatted_address": "Rua Fiobre, 100, 15165 A Coruña, Spain",
        }
        assert answered_house_number(geo) == "100"
        assert house_number_agreement("calle Fiobre, Bergondo, Spain", "100") == (
            NUMBER_NOT_ASKED
        )

    def test_the_real_adapter_passes_the_components_through(self):
        """Not a stub: `GeocodingService.geocode_address` itself, fed Google's
        payload shape, hands `address_components` and `location_type` to the
        check. The province and municipality checks rest on the same field;
        this is the evidence the review asked for."""
        from unittest.mock import patch

        from utils.geocoding import GeocodingService

        payload = {
            "status": "OK",
            "results": [
                {
                    "formatted_address": "Rua Fiobre, 100, 15165 A Coruña, Spain",
                    "geometry": {
                        "location": {"lat": 43.32, "lng": -8.24},
                        "location_type": "ROOFTOP",
                    },
                    "types": ["street_address"],
                    "address_components": [
                        {"long_name": "100", "types": ["street_number"]},
                        {"long_name": "Rua Fiobre", "types": ["route"]},
                        {"long_name": "15165", "types": ["postal_code"]},
                    ],
                }
            ],
        }

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return payload

        service = GeocodingService()
        service.google_maps_key = "test-key"
        with patch("utils.geocoding.billed_get", return_value=_Response()):
            geo = service.geocode_address("calle Fiobre, Bergondo, Spain")

        assert geo["accuracy"] == "precise"
        assert geo["location_type"] == "ROOFTOP"
        assert answered_house_number(geo) == "100"

    def test_a_compound_number_is_its_leading_digits(self):
        """The reviewer's other failing input: "12 bis" answered for house 14
        used to read as no number at all, and kept `precise`."""
        geo = _answer(
            "Calle Mayor, 12 bis, 28013 Madrid", postal="28013", number="12 bis"
        )
        assert answered_house_number(geo) == "12bis"
        assert house_number_agreement("Calle Mayor, 14, Madrid, Spain", "12bis") == (
            DIFFERENT_NUMBER
        )
        geo = _answer(
            "Calle Mayor, 12-14, 28013 Madrid", postal="28013", number="12-14"
        )
        assert answered_house_number(geo) == "1214"

    def test_sin_numero_names_no_number(self):
        geo = _answer(
            "Villar de Arriba, S/N, 33469 Tamon", postal="33469", number="S/N"
        )
        assert answered_house_number(geo) is None

    def test_no_component_names_no_number(self):
        assert (
            answered_house_number(_answer("Caserio X, 33318", postal="33318")) is None
        )
        assert answered_house_number(None) is None
        assert answered_house_number({"address_components": ["junk"]}) is None

    @pytest.mark.parametrize(
        "formatted,expected",
        [
            ("Rua Fiobre, 100, 15165 A Coruña, Spain", "100"),
            ("SI-6, 24b, 33199 Fozana, Asturias, Spain", "24b"),
            # 246: Google split the letter into its own component.
            ("Lugar, Pite, 3, a, 15181 Cambre, A Coruña, Spain", "3"),
            # 438: S/N is not a number.
            ("Villar de Arriba, S/N, 33469 Tamon, Asturias, Spain", None),
            # 765: a road number and an exit number travel with their words.
            (
                "Calle Alto del Praviano, Nacional 632, s/n, Salida 417, "
                "33126 Soto del Barco, Asturias, Spain",
                None,
            ),
            ("Villaviciosa, Asturias, Spain", None),
            (None, None),
            # Compound numbers, and the words that are not numbers.
            ("Calle Mayor, 12 bis, 28013 Madrid, Spain", "12bis"),
            ("Calle Real, 2 Planta, 33201 Gijón, Spain", None),
        ],
    )
    def test_the_stored_string(self, formatted, expected):
        assert house_number_in_formatted(formatted) == expected


class TestTheVerdict:
    def test_the_four_states(self):
        assert house_number_agreement("calle Fiobre, Bergondo, Spain", "100") == (
            NUMBER_NOT_ASKED
        )
        assert house_number_agreement("Lugar Costenla, 31, Carballo, Spain", "31") == (
            AGREED
        )
        assert house_number_agreement("pillarno - el calello, 83, Spain", "10") == (
            DIFFERENT_NUMBER
        )
        assert house_number_agreement("calle Fiobre, Bergondo, Spain", None) == (
            NO_NUMBER_ANSWERED
        )

    def test_a_suffix_on_both_sides_must_agree(self):
        """The reviewer's input: 24A asked and 24B answered are two houses.
        A suffix on one side only is the same house written twice (rows 45,
        46, 91, 216, 246, 713, 791, 926 on production)."""
        assert house_number_agreement("Calle Mayor, 24A, Madrid, Spain", "24b") == (
            DIFFERENT_NUMBER
        )
        assert house_number_agreement("SI-6, 24, Siero, Spain", "24b") == AGREED
        assert house_number_agreement("Lugar Pite, 3 a, Cambre, Spain", "3") == AGREED
        assert house_number_agreement("Lugar Pazo, 23 C, Abegondo, Spain", "23c") == (
            AGREED
        )

    def test_only_a_positive_finding_withdraws_the_label(self):
        assert earns_precise(AGREED) is True
        assert earns_precise(NO_NUMBER_ANSWERED) is True
        assert earns_precise(NUMBER_NOT_ASKED) is False
        assert earns_precise(DIFFERENT_NUMBER) is False

    def test_the_two_blind_spots_pass_and_are_known(self):
        """360: the same number 2868 m away. 25: the same number on another
        street. Neither is caught here, and only a person's pin catches
        them -- pinned so the check is never read as verification."""
        assert (
            house_number_agreement("Barrio de Prendonés, 1, El Franco, Spain", "1")
            == AGREED
        )
        assert (
            house_number_agreement("calle Tarancon, 6, Lo Marabú, Rojales, Spain", "6")
            == AGREED
        )


class TestTheGeocoderStoresWhatItEarned:
    def test_a_rooftop_to_a_query_that_named_no_number_is_approximate(self, app):
        """Row 1379: "calle Fiobre, Bergondo" -> "Rua Fiobre, 100". The 100 is
        Google's, and the point is kept -- only its worth changes."""
        with app.app_context():
            row = _row("Chalet en venta en calle Fiobre, Bergondo", "Bergondo")
            queries, record = _geocode(
                row,
                _answer(
                    "Rua Fiobre, 100, 15165 A Coruña, Spain",
                    postal="15165",
                    number="100",
                ),
            )
            assert queries[0] == "calle Fiobre, Bergondo, Spain"
            assert row.location_accuracy == "approximate"
            assert float(row.location_lat) == pytest.approx(43.32)
            assert record["accuracy"] == "approximate"
            assert record["answered_accuracy"] == "precise"
            assert record["address_check"] == NUMBER_NOT_ASKED
            assert record["location_type"] == "ROOFTOP"

    def test_a_rooftop_that_answers_the_number_asked_keeps_precise(self, app):
        """Row 1537."""
        with app.app_context():
            row = _row("Casa en venta en Lugar Costenla, 31, Carballo", "Carballo")
            _, record = _geocode(
                row,
                _answer(
                    "Lugar Costenla, 31, 15109, A Coruña, Spain",
                    postal="15109",
                    number="31",
                ),
            )
            assert row.location_accuracy == "precise"
            assert record["accuracy"] == "precise"
            assert record["address_check"] == AGREED
            assert record["location_type"] == "ROOFTOP"
            assert "answered_accuracy" not in record

    def test_a_letter_suffix_is_the_same_number(self, app):
        """Rows 45/46: "SI-6, 24" answered "SI-6, 24b" -- a refinement, not a
        contradiction, and the road code's 6 is not a house number."""
        with app.app_context():
            row = _row("Land in SI-6, 24, Viella-Granda-Meres, Siero", "Siero")
            _, record = _geocode(
                row,
                _answer(
                    "SI-6, 24b, 33199 Fozana, Asturias, Spain",
                    postal="33199",
                    number="24b",
                ),
            )
            assert row.location_accuracy == "precise"
            assert record["address_check"] == AGREED

    def test_a_rooftop_naming_a_different_number_is_approximate(self, app):
        """Row 355: 83 asked, 10 answered."""
        with app.app_context():
            row = _row(
                "Land in pillarno - el calello, 83, Piedras Blancas, Castrillon",
                "Castrillón",
            )
            _, record = _geocode(
                row,
                _answer(
                    "Av. Eysines, 10, 33459 Piedras Blancas, Asturias, Spain",
                    postal="33459",
                    number="10",
                ),
            )
            assert row.location_accuracy == "approximate"
            assert record["address_check"] == DIFFERENT_NUMBER
            assert record["answered_accuracy"] == "precise"

    def test_a_rooftop_naming_no_number_cannot_be_refuted(self, app):
        """A caserío matched by name: nothing to compare, and the label stays.
        Widening past a positive finding is the guard-too-wide mistake #535
        names."""
        with app.app_context():
            row = _row("Land in Caserío Peñacresta, Villaviciosa", "Villaviciosa")
            _, record = _geocode(
                row,
                _answer("Caserio Peñacresta, 33318, Asturias, Spain", postal="33318"),
            )
            assert row.location_accuracy == "precise"
            assert record["address_check"] == NO_NUMBER_ANSWERED

    def test_an_approximate_answer_is_recorded_with_its_check_and_left_alone(self, app):
        with app.app_context():
            row = _row("Land in Llaranes, Avilés", "Avilés")
            _, record = _geocode(
                row,
                _answer(
                    "Llaranes, 33460 Avilés, Asturias, Spain",
                    postal="33460",
                    location_type="GEOMETRIC_CENTER",
                    accuracy="approximate",
                ),
            )
            assert row.location_accuracy == "approximate"
            assert record["location_type"] == "GEOMETRIC_CENTER"
            assert record["address_check"] == NO_NUMBER_ANSWERED
            assert "answered_accuracy" not in record

    def test_the_same_number_somewhere_else_is_the_disclosed_blind_spot(self, app):
        """Row 360: "Barrio de Prendonés, 1" -> "Tr.ª de Prendonés, 1", 2868 m
        apart. The number agrees, the check passes it, and only the owner's
        pin fixed the row."""
        with app.app_context():
            row = _row("Land in Barrio de Prendonés, 1, El Franco", "El Franco")
            _, record = _geocode(
                row,
                _answer(
                    "Tr.ª de Prendonés, 1, 33746 La Caridad, Asturias, Spain",
                    postal="33746",
                    number="1",
                ),
            )
            assert row.location_accuracy == "precise"
            assert record["address_check"] == AGREED

    def test_a_withdrawn_precise_does_not_displace_a_portal_pin(self, app):
        """The even-trade rule (#393) reads the label AFTER the check: an
        unearned ROOFTOP is `approximate` against the pin's `approximate`,
        so the pin stays -- and the record still says what Google answered,
        with the check that kept it out."""
        pin_lat, pin_lon = "43.5500000", "-5.9500000"
        with app.app_context():
            row = _row(
                "Terreno en venta en Llaranes, Avilés",
                "Avilés",
                location_lat=pin_lat,
                location_lon=pin_lon,
                location_accuracy="approximate",
                enrichment=record_portal_coordinate(
                    None, source="fotocasa", lat=pin_lat, lon=pin_lon
                ),
            )
            _, record = _geocode(
                row,
                _answer(
                    "C. Falsa, 12, 33460 Avilés, Asturias, Spain",
                    postal="33460",
                    number="12",
                    lat=43.51,
                    lng=-5.91,
                ),
                refresh=True,
            )
            assert float(row.location_lat) == pytest.approx(43.55)
            assert float(row.location_lon) == pytest.approx(-5.95)
            assert row.location_accuracy == "approximate"
            assert record["kept"] == "fotocasa coordinate"
            # Provenance: what Google said, and why it did not count.
            assert record["answered_accuracy"] == "precise"
            assert record["address_check"] == NUMBER_NOT_ASKED


# ---------------------------------------------------------------- the audit --


def _stored(query, formatted, *, column="precise", record="precise", extra=None):
    """A row as the geocoder left it before #535: the label in the column,
    the query and the answer in the record, and no components."""
    enrichment = {
        "geocoding": {
            "query": query,
            "formatted_address": formatted,
            "accuracy": record,
        }
    }
    enrichment.update(extra or {})
    return _row(
        query,
        None,
        location_lat="43.3200000",
        location_lon="-8.2400000",
        location_accuracy=column,
        enrichment=enrichment,
    )


REFUTED = (
    "venta en calle Fiobre, Bergondo, Spain",
    "Rua Fiobre, 100, 15165 A Coruña, Spain",
)
KEPT = (
    "Lugar Costenla, 31, Carballo, Spain",
    "Lugar Costenla, 31, 15109, A Coruña, Spain",
)


class TestTheAuditOfStoredRows:
    def test_a_refuted_record_is_withdrawn_and_an_agreeing_one_kept(self, app):
        with app.app_context():
            refuted = read_verdict(_stored(*REFUTED))
            kept = read_verdict(_stored(*KEPT))
            assert (refuted["action"], refuted["why"]) == ("withdraw", NUMBER_NOT_ASKED)
            assert (kept["action"], kept["why"]) == ("keep", AGREED)

    def test_a_row_a_person_located_is_skipped_whatever_shape_the_finding_took(
        self, app
    ):
        with app.app_context():
            hand_set = _stored(
                *REFUTED,
                extra=record_manual_coordinate(
                    None,
                    lat="43.3200000",
                    lon="-8.2400000",
                    accuracy="precise",
                    note="parcel from the cadastre",
                    source="cadastre",
                ),
            )
            adhoc = _stored(
                *REFUTED, extra={"coordinate_provenance": {"method": "cadastre"}}
            )
            on_parcel = _stored(
                *REFUTED, extra={"cadastre": {"reference_point": [43.32, -8.24]}}
            )
            elsewhere = _stored(
                *REFUTED, extra={"cadastre": {"reference_point": [43.40, -8.10]}}
            )
            assert person_established(hand_set) == "hand-set location"
            assert person_established(adhoc) == "ad-hoc provenance block"
            assert person_established(on_parcel) == (
                "standing on the cadastre reference point"
            )
            assert person_established(elsewhere) is None
            for row in (hand_set, adhoc, on_parcel):
                assert read_verdict(row)["action"] == "skip"
            assert read_verdict(elsewhere)["action"] == "withdraw"

    def test_a_label_the_geocoder_did_not_write_is_skipped(self, app):
        with app.app_context():
            importer = _stored(*REFUTED, record="approximate")
            kept_pin = _stored(*REFUTED, extra=None)
            kept_pin.enrichment = {
                "geocoding": {**kept_pin.enrichment["geocoding"], "kept": "pin"}
            }
            not_precise = _stored(*REFUTED, column="approximate")
            assert read_verdict(importer)["why"] == "the label is not the geocoder's"
            assert read_verdict(kept_pin)["why"] == "the label is not the geocoder's"
            assert read_verdict(not_precise)["why"] == "not precise"

    def test_a_dry_run_writes_nothing(self, app):
        with app.app_context():
            refuted = _stored(*REFUTED)
            assert main([]) == 0
            db.session.expire_all()
            stored = db.session.get(Property, refuted.id)
            assert stored.location_accuracy == "precise"
            assert "precise_withdrawn" not in stored.enrichment["geocoding"]

    def test_apply_needs_a_snapshot(self, app):
        with app.app_context():
            _stored(*REFUTED)
            with pytest.raises(SystemExit):
                main(["--apply"])

    def test_apply_withdraws_and_restore_puts_it_back(self, app, tmp_path):
        snapshot = tmp_path / "precise_535.json"
        with app.app_context():
            refuted = _stored(*REFUTED)
            kept = _stored(*KEPT)

            assert main(["--apply", "--snapshot", str(snapshot)]) == 0

            db.session.expire_all()
            withdrawn = db.session.get(Property, refuted.id)
            assert withdrawn.location_accuracy == "approximate"
            record = withdrawn.enrichment["geocoding"]
            assert record["accuracy"] == "approximate"
            assert record["answered_accuracy"] == "precise"
            assert record["address_check"] == NUMBER_NOT_ASKED
            assert record["precise_withdrawn"]["by"] == ACTOR
            assert db.session.get(Property, kept.id).location_accuracy == "precise"
            assert [r["id"] for r in json.loads(snapshot.read_text())] == [refuted.id]

            assert main(["--restore", str(snapshot)]) == 0

            db.session.expire_all()
            restored = db.session.get(Property, refuted.id)
            assert restored.location_accuracy == "precise"
            assert "precise_withdrawn" not in restored.enrichment["geocoding"]

    def test_ids_narrow_the_scope(self, app, tmp_path):
        snapshot = tmp_path / "s.json"
        with app.app_context():
            refuted = _stored(*REFUTED)
            other = _stored(*REFUTED)
            assert (
                main(["--ids", str(other.id), "--apply", "--snapshot", str(snapshot)])
                == 0
            )
            db.session.expire_all()
            assert db.session.get(Property, refuted.id).location_accuracy == "precise"
            assert db.session.get(Property, other.id).location_accuracy == "approximate"

    def test_withdraw_is_idempotent(self, app):
        with app.app_context():
            refuted = _stored(*REFUTED)
            assert withdraw(refuted) is True
            assert withdraw(refuted) is False
