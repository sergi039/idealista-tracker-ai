"""services/cadastre_locate.py, and the pure helpers of
utils/audit_precise_against_cadastre.py, against real Catastro answers
(#558, #559).

`services/cadastre_locate.py` is the second coordinate: given a point, it asks
Catastro's `Consulta_RCCOOR_Distancia` which cadastral parcels lie at or near
it, and compares the asked house number against what came back. Every fixture
here is either a real recorded shape (the no-`lpcd`-key absence, the error
body, the `Consulta_DNPLOC` success payload) or a real production case (row
25's "calle Tarancon, 6" answered on "Av. de Salamanca, 6"; "rambla de la
Libertad" against Catastro's own "RAMBLA DE LA LLIBERTAT").

Nothing here reaches the network: `_fetch_json` and `_get` are monkeypatched
per test, so `tests/network_guard.py` never has anything to refuse.

What is pinned: `-1` is absence, not a number (`street_number`); an answer
with no `lpcd` key is `not_found` while an error body inside a 200 is
`refused`, never the other way around (`parcels_at`); a parcel with an
unreadable distance is dropped rather than defaulted to zero; the five
findings `compare_to_asked_number` can report, including the street-mismatch
guard that exists because of row 25 and does not fire on a mere spelling
difference; `normalize_street`/`cadastral_street`'s accent-folding,
sigla-dropping and number-stopping; `_digits`' duplicate-suffix handling;
`parcel_for_address`'s parcel-not-flat reference and its two absence codes;
`match_street`'s exact-or-nothing behaviour; and the CLI module's `in_cohort`
reasons and `summarise`'s measured/unresolved split.
"""

import requests
import pytest

from services import cadastre_locate
from utils import audit_precise_against_cadastre as audit


class _Response:
    """A `requests.Response` stand-in, matching `tests/test_cadastre_service.py`."""

    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _StubProperty:
    """A `Property` stand-in carrying only what `in_cohort`/`measure` read.

    `services/coordinate_quality.manual_coordinate` reads `enrichment` off the
    record with a bare `getattr`, so a plain object -- no SQLAlchemy model, no
    Flask app, no database -- is a realistic double.
    """

    def __init__(
        self,
        *,
        id=1,
        municipality="Somewhere",
        location_lat=None,
        location_lon=None,
        location_accuracy=None,
        enrichment=None,
    ):
        self.id = id
        self.municipality = municipality
        self.location_lat = location_lat
        self.location_lon = location_lon
        self.location_accuracy = location_accuracy
        self.enrichment = enrichment if enrichment is not None else {}


class TestStreetNumber:
    def test_it_reads_the_structured_field(self):
        entry = {"dt": {"lourb": {"dir": {"pnp": "25"}}}}
        assert cadastre_locate.street_number(entry) == "25"

    def test_minus_one_is_catastros_no_street_number_not_a_number(self):
        # How every rustic parcel comes back -- an absence, reported as one.
        entry = {"dt": {"lourb": {"dir": {"pnp": "-1"}}}}
        assert cadastre_locate.street_number(entry) is None

    def test_a_missing_or_malformed_path_is_none_not_an_error(self):
        for entry in (
            {},
            {"dt": None},
            {"dt": {"lourb": "not a dict"}},
            {"dt": {"lourb": {"dir": "not a dict"}}},
            "not a dict at all",
            None,
        ):
            assert cadastre_locate.street_number(entry) is None


# A minimal but realistic `Consulta_RCCOOR_Distancia` success answer: two
# parcels, deliberately supplied out of distance order.
RCCOOR_TWO_PARCELS = {
    "Consulta_RCCOOR_DistanciaResult": {
        "control": {"cucoor": 1},
        "coordenadas_distancias": {
            "coordd": [
                {
                    "geo": {"xcen": "-0.72", "ycen": "38.10", "srs": "EPSG:4326"},
                    "lpcd": [
                        {
                            "pc": {"pc1": "0139701", "pc2": "YH0103M"},
                            "dis": "18.40",
                            "ldt": "CL TARANCON 8 ROJALES (ALICANTE)",
                            "dt": {"lourb": {"dir": {"pnp": "8"}}},
                        },
                        {
                            "pc": {"pc1": "0139702", "pc2": "YH0103N"},
                            "dis": "0.00",
                            "ldt": "CL TARANCON 6 ROJALES (ALICANTE)",
                            "dt": {"lourb": {"dir": {"pnp": "6"}}},
                        },
                    ],
                }
            ]
        },
    }
}


class TestParsingASuccessAnswer:
    def test_a_real_success_shape_is_sorted_nearest_first_with_the_right_fields(self):
        parcels = cadastre_locate._parse(RCCOOR_TWO_PARCELS)

        assert [p["distance_m"] for p in parcels] == [0.0, 18.4]
        assert isinstance(parcels[0]["distance_m"], float)
        nearest = parcels[0]
        assert nearest["reference"] == "0139702YH0103N"  # pc1 + pc2
        assert nearest["address"] == "CL TARANCON 6 ROJALES (ALICANTE)"  # ldt

    def test_a_parcel_with_an_unreadable_distance_is_dropped_not_defaulted_to_zero(
        self,
    ):
        payload = {
            "Consulta_RCCOOR_DistanciaResult": {
                "control": {"cucoor": 1},
                "coordenadas_distancias": {
                    "coordd": [
                        {
                            "geo": {},
                            "lpcd": [
                                {
                                    "pc": {"pc1": "A", "pc2": "B"},
                                    "dis": "10.0",
                                    "ldt": "X",
                                    "dt": {},
                                },
                                {
                                    "pc": {"pc1": "C", "pc2": "D"},
                                    "dis": "not-a-number",
                                    "ldt": "Y",
                                    "dt": {},
                                },
                            ],
                        }
                    ]
                },
            }
        }
        parcels = cadastre_locate._parse(payload)
        # The unreadable entry is gone, not present with distance 0.0.
        assert len(parcels) == 1
        assert parcels[0]["reference"] == "AB"


class TestParcelsAt:
    def test_no_lpcd_key_at_all_is_not_found_not_an_error(self, monkeypatch):
        """The measured shape for a point in the sea or outside Spain: a
        successful `control` and a coordinate echoed back, with no `lpcd` key
        anywhere in the answer."""
        payload = {
            "Consulta_RCCOOR_DistanciaResult": {
                "control": {"cucoor": 1},
                "coordenadas_distancias": {
                    "coordd": [
                        {"geo": {"xcen": "-7.30", "ycen": "43.80", "srs": "EPSG:4326"}}
                    ]
                },
            }
        }
        monkeypatch.setattr(cadastre_locate, "_fetch_json", lambda url, params: payload)

        result = cadastre_locate.parcels_at(43.80, -7.30)

        assert result["status"] == cadastre_locate.NOT_FOUND

    def test_an_error_body_inside_200_is_a_refusal_never_not_found(self, monkeypatch):
        """This endpoint carries no absence code of its own (`ADDRESS_ABSENCE_CODES`
        is passed as empty for it), so any error body is a refusal -- never read
        as Catastro having looked and found nothing."""
        payload = {
            "Consulta_RCCOOR_DistanciaResult": {
                "control": {"cuerr": 1},
                "lerr": [{"cod": "76", "des": "LA COORDENADA X OBLIGATORIA"}],
            }
        }
        monkeypatch.setattr(cadastre_locate, "_fetch_json", lambda url, params: payload)

        result = cadastre_locate.parcels_at(43.80, -7.30)

        assert result["status"] == cadastre_locate.REFUSED
        assert result["status"] != cadastre_locate.NOT_FOUND


class TestFetchJson:
    """`_fetch_json`'s own reduction of a transport outcome to a state.

    `_get` is patched here rather than `_fetch_json` itself, because what is
    pinned in this class is that reduction -- HTTP status, a `requests`
    exception, a non-JSON body -- and patching `_fetch_json` away would bypass
    the very code being tested.
    """

    def test_a_non_200_status_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            cadastre_locate, "_get", lambda url, params: _Response(status_code=500)
        )
        with pytest.raises(cadastre_locate.CadastreError) as caught:
            cadastre_locate._fetch_json("http://x", {})
        assert caught.value.state == cadastre_locate.REFUSED

    def test_a_request_exception_is_unavailable(self, monkeypatch):
        def boom(url, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(cadastre_locate, "_get", boom)
        with pytest.raises(cadastre_locate.CadastreError) as caught:
            cadastre_locate._fetch_json("http://x", {})
        assert caught.value.state == cadastre_locate.UNAVAILABLE

    def test_non_json_is_malformed(self, monkeypatch):
        monkeypatch.setattr(
            cadastre_locate, "_get", lambda url, params: _Response(payload=None)
        )
        with pytest.raises(cadastre_locate.CadastreError) as caught:
            cadastre_locate._fetch_json("http://x", {})
        assert caught.value.state == cadastre_locate.MALFORMED


class TestCompareToAskedNumber:
    def test_on_asked_parcel_when_the_number_is_at_distance_zero(self):
        parcels = [
            {"number": "6", "distance_m": 0.0, "reference": "REF1", "address": "CL X 6"}
        ]
        result = cadastre_locate.compare_to_asked_number(parcels, "6")
        assert result["finding"] == cadastre_locate.ON_ASKED_PARCEL

    def test_displaced_carries_the_measured_distance(self):
        parcels = [
            {
                "number": "6",
                "distance_m": 12.5,
                "reference": "REF1",
                "address": "CL X 6",
            }
        ]
        result = cadastre_locate.compare_to_asked_number(parcels, "6")
        assert result["finding"] == cadastre_locate.DISPLACED
        assert result["distance_m"] == 12.5

    def test_beyond_neighbours_is_bounded_by_the_furthest_parcel_returned(self):
        # Nearest-first, as `_parse` would hand it over.
        parcels = [
            {"number": "2", "distance_m": 3.0, "reference": "R2", "address": "A2"},
            {"number": "4", "distance_m": 9.5, "reference": "R4", "address": "A4"},
        ]
        result = cadastre_locate.compare_to_asked_number(parcels, "99")
        assert result["finding"] == cadastre_locate.BEYOND_NEIGHBOURS
        assert result["at_least_m"] == 9.5  # the furthest, not the nearest

    def test_no_number_asked_when_asked_is_none_or_empty(self):
        parcels = [{"number": "6", "distance_m": 0.0, "reference": "R", "address": "A"}]
        assert (
            cadastre_locate.compare_to_asked_number(parcels, None)["finding"]
            == cadastre_locate.NO_NUMBER_ASKED
        )
        assert (
            cadastre_locate.compare_to_asked_number(parcels, "")["finding"]
            == cadastre_locate.NO_NUMBER_ASKED
        )

    def test_number_matched_other_street_is_the_real_row_25_case(self):
        """The production case that motivated this finding (#535's second
        disclosed blind spot): "calle Tarancon, 6" was answered on
        "Av. de Salamanca, 6" -- the number matched by coincidence, the street
        did not, and the first run of this measurement called it
        `on_asked_parcel`."""
        parcels = [
            {
                "number": "6",
                "distance_m": 0.0,
                "reference": "REF25",
                "address": "AV SALAMANCA DE 6 ROJALES (ALICANTE)",
            }
        ]
        result = cadastre_locate.compare_to_asked_number(parcels, "6", "calle Tarancon")
        assert result["finding"] == cadastre_locate.NUMBER_MATCHED_OTHER_STREET
        assert result["asked_street"] == "CALLE TARANCON"
        assert result["cadastral_street"] == "SALAMANCA DE"

    def test_a_mere_spelling_difference_does_not_fire_the_street_mismatch(self):
        """ "rambla de la Libertad" against Catastro's own "AV RAMBLA DE LA
        LLIBERTAT ..." share three words including "RAMBLA"; only "LIBERTAD"
        against "LLIBERTAT" differ, by spelling alone. That must read as a
        distance finding, never as `number_matched_other_street`."""
        parcels = [
            {
                "number": "2",
                "distance_m": 3.0,
                "reference": "R",
                "address": (
                    "AV RAMBLA DE LA LLIBERTAT 2 SANT JOAN D'ALACANT (ALICANTE)"
                ),
            }
        ]
        result = cadastre_locate.compare_to_asked_number(
            parcels, "2", "rambla de la Libertad"
        )
        assert result["finding"] == cadastre_locate.DISPLACED
        assert result["distance_m"] == 3.0


class TestNormalizeStreetAndCadastralStreet:
    def test_accents_are_folded(self):
        assert (
            cadastre_locate.normalize_street("Calle de la Peña") == "CALLE DE LA PENA"
        )

    def test_the_sigla_is_dropped_and_the_name_stops_at_the_number(self):
        address = "CL DEAN ANTONIO SALA 9 SANT JOAN D'ALACANT (ALICANTE)"
        assert cadastre_locate.cadastral_street(address) == "DEAN ANTONIO SALA"

    def test_the_article_suffix_comma_form_becomes_two_separate_words(self):
        """Catastro writes a leading article after the street with a comma --
        `"CALZADA,LA"` for "La Calzada". `normalize_street` turns every
        non-alphanumeric character, the comma included, into a space rather
        than moving the article to the front, so the two words survive in
        Catastro's own order. (An earlier version of this normaliser tried
        moving the article and got it wrong on production rows 63, 622 and
        954 -- reported as sharing no word with the street that was asked,
        when the only difference was where Catastro puts the article.)
        """
        address = "CL CALZADA,LA 6(A) LLANES (ASTURIAS)"
        assert cadastre_locate.cadastral_street(address) == "CALZADA LA"

    def test_the_duplicate_suffix_does_not_extend_the_street_name(self):
        # "6(A)" folds to two words, "6" and "A"; the bare "6" still stops the
        # street name, taking the duplicate letter down with it.
        assert (
            cadastre_locate.cadastral_street("CL CALZADA,LA 6(A) LLANES (ASTURIAS)")
            == "CALZADA LA"
        )
        assert (
            "A"
            not in cadastre_locate.cadastral_street(
                "CL CALZADA,LA 6(A) LLANES (ASTURIAS)"
            ).split()
        )


class TestDigitsBehindTheNumberComparison:
    def test_plain_digits_read_as_themselves(self):
        assert cadastre_locate._digits("6") == "6"

    def test_catastros_duplicate_suffix_compares_equal_to_the_bare_number(self):
        # Catastro writes a duplicate as "6(A)" in `ldt"; a query writes "6 A".
        # Comparing the whole token would call those two different houses.
        assert cadastre_locate._digits("6(A)") == "6"
        assert cadastre_locate._digits("6 A") == "6"


# A real `Consulta_DNPLOC` success payload: one flat (`car`/`cc1`/`cc2`) inside
# a parcel, plus the whole plot's own address in `finca`.
DNPLOC_SUCCESS = {
    "consulta_dnplocResult": {
        "control": {"cudnp": 1, "cucons": 4},
        "bico": {
            "bi": {
                "idbi": {
                    "cn": "UR",
                    "rc": {
                        "pc1": "0139702",
                        "pc2": "YH0103N",
                        "car": "0013",
                        "cc1": "F",
                        "cc2": "Y",
                    },
                },
                "ldt": "CL TARANCON 6 Es:1 Pl:00 Pt:24 03170 ROJALES (ALICANTE)",
            },
            "finca": {"ldt": "AV GIJON DE 96  ROJALES (ALICANTE)"},
        },
    }
}


class TestParcelForAddress:
    def test_a_real_success_shape_yields_the_parcel_reference_not_the_flat(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            cadastre_locate, "_fetch_json", lambda url, params: DNPLOC_SUCCESS
        )

        result = cadastre_locate.parcel_for_address("03", "900", "CL", "TARANCON", "6")

        assert result["status"] == cadastre_locate.OK
        # car/cc1/cc2 identify the flat, not the parcel, and are dropped.
        assert result["reference"] == "0139702YH0103N"
        assert len(result["reference"]) == 14
        assert result["plot_address"] == "AV GIJON DE 96  ROJALES (ALICANTE)"

    def test_la_via_no_existe_is_not_found(self, monkeypatch):
        payload = {
            "consulta_dnplocResult": {
                "control": {"cuerr": 1},
                "lerr": [{"cod": "33", "des": "LA VIA NO EXISTE"}],
            }
        }
        monkeypatch.setattr(cadastre_locate, "_fetch_json", lambda url, params: payload)

        result = cadastre_locate.parcel_for_address("03", "900", "CL", "X", "1")

        assert result["status"] == cadastre_locate.NOT_FOUND

    def test_el_numero_no_existe_is_not_found(self, monkeypatch):
        payload = {
            "consulta_dnplocResult": {
                "control": {"cuerr": 1},
                "lerr": [{"cod": "43", "des": "EL NUMERO NO EXISTE"}],
            }
        }
        monkeypatch.setattr(cadastre_locate, "_fetch_json", lambda url, params: payload)

        result = cadastre_locate.parcel_for_address("03", "900", "CL", "X", "999")

        assert result["status"] == cadastre_locate.NOT_FOUND

    def test_another_code_is_refused_not_not_found(self, monkeypatch):
        # 12 -- "LA PROVINCIA NO EXISTE" -- is a malformed request, not a
        # measured fact about the address.
        payload = {
            "consulta_dnplocResult": {
                "control": {"cuerr": 1},
                "lerr": [{"cod": "12", "des": "LA PROVINCIA NO EXISTE"}],
            }
        }
        monkeypatch.setattr(cadastre_locate, "_fetch_json", lambda url, params: payload)

        result = cadastre_locate.parcel_for_address("99", "900", "CL", "X", "1")

        assert result["status"] == cadastre_locate.REFUSED


class TestMatchStreet:
    def test_an_exact_match_returns_the_street(self):
        index = [
            {
                "code": "1",
                "sigla": "CL",
                "name": "Calle Mayor",
                "normalized": cadastre_locate.normalize_street("Calle Mayor"),
            },
            {
                "code": "2",
                "sigla": "CL",
                "name": "Calle Mayor Nueva",
                "normalized": cadastre_locate.normalize_street("Calle Mayor Nueva"),
            },
        ]
        result = cadastre_locate.match_street(index, "Calle Mayor")
        assert result["status"] == cadastre_locate.OK
        assert result["street"] == index[0]

    def test_no_match_is_street_not_matched(self):
        index = [
            {
                "code": "1",
                "sigla": "CL",
                "name": "Calle Mayor",
                "normalized": cadastre_locate.normalize_street("Calle Mayor"),
            }
        ]
        result = cadastre_locate.match_street(index, "Calle Inexistente")
        assert result["status"] == cadastre_locate.STREET_NOT_MATCHED

    def test_two_streets_matching_the_same_are_ambiguous_never_a_pick(self):
        # Two different codes, two different original spellings ("Iglesia,
        # La" and "La Iglesia") that are the same street name in a different
        # article position -- never resolved by choosing one.
        index = [
            {
                "code": "1",
                "sigla": "LG",
                "name": "Iglesia, La",
                "normalized": cadastre_locate.normalize_street("Iglesia, La"),
            },
            {
                "code": "2",
                "sigla": "CL",
                "name": "La Iglesia",
                "normalized": cadastre_locate.normalize_street("La Iglesia"),
            },
        ]
        result = cadastre_locate.match_street(index, "La Iglesia")
        assert result["status"] == cadastre_locate.STREET_AMBIGUOUS
        assert result["candidates"] == index


class TestInCohort:
    def test_a_row_a_person_located_is_out_of_scope(self):
        prop = _StubProperty(
            location_lat=43.5,
            location_lon=-6.0,
            enrichment={
                "location": {
                    "lat": 43.5,
                    "lon": -6.0,
                    "accuracy": "precise",
                    "note": "cadastre_by_address",
                }
            },
        )
        assert audit.in_cohort(prop) == "a person set this location"

    def test_a_row_with_no_coordinate_is_out_of_scope(self):
        prop = _StubProperty(location_lat=None, location_lon=None)
        assert audit.in_cohort(prop) == "no coordinate"

    def test_a_query_naming_no_house_number_is_out_of_scope(self):
        prop = _StubProperty(
            location_lat=43.5,
            location_lon=-6.0,
            enrichment={"geocoding": {"query": "Lugar Vega, Siero"}},
        )
        assert audit.in_cohort(prop) == "the query named no house number"

    def test_a_row_whose_address_check_disagrees_is_out_of_scope(self):
        prop = _StubProperty(
            location_lat=43.5,
            location_lon=-6.0,
            enrichment={
                "geocoding": {
                    "query": "Calle Mayor, 6",
                    "address_components": [
                        {"types": ["street_number"], "long_name": "8"}
                    ],
                }
            },
        )
        reason = audit.in_cohort(prop)
        assert reason is not None
        assert reason.startswith("address_check is")
        assert "agreed" not in reason.split(",")[0]  # it names the disagreement

    def test_a_row_in_the_cohort_is_none(self):
        prop = _StubProperty(
            location_lat=43.5,
            location_lon=-6.0,
            enrichment={
                "geocoding": {
                    "query": "Calle Mayor, 6",
                    "address_components": [
                        {"types": ["street_number"], "long_name": "6"}
                    ],
                }
            },
        )
        assert audit.in_cohort(prop) is None


class TestSummarise:
    def test_a_mixed_run_produces_the_documented_keys(self):
        rows = [
            {"finding": audit.ON_ASKED_PARCEL},
            {"finding": audit.DISPLACED, "distance_m": 12.5},
            {"finding": audit.DISPLACED, "distance_m": 3.0},
            # The first pass could not place this one; the second pass did.
            {
                "finding": audit.BEYOND_NEIGHBOURS,
                "at_least_m": 20.0,
                "resolved_distance_m": 30.0,
            },
            # Same number, wrong street: must not count as `on_asked_parcel`.
            {"finding": audit.NUMBER_MATCHED_OTHER_STREET, "distance_m": 0.0},
            {"finding": audit.NO_NUMBER_ASKED},
            {"finding": None, "cadastre_status": "not_found"},
        ]

        summary = audit.summarise(rows)

        assert summary["rows"] == 7
        # Excludes the one NUMBER_MATCHED_OTHER_STREET row.
        assert summary["on_asked_parcel"] == 1
        # DISPLACED distances and a second-pass `resolved_distance_m` merge
        # into one measured column, sorted.
        assert summary["measured_displacements_m"] == [3.0, 12.5, 30.0]
        assert summary["measured_max_m"] == 30.0
        assert summary["measured_over_25m"] == 1
        # Rows left over: BEYOND_NEIGHBOURS' own at_least_m does not count it
        # as measured once nothing resolved it further -- here it did, so the
        # three left over are NUMBER_MATCHED_OTHER_STREET, NO_NUMBER_ASKED and
        # the cadastre-refusal row.
        assert summary["unresolved_rows"] == 3
        assert summary["findings"][audit.NUMBER_MATCHED_OTHER_STREET] == 1
        assert summary["findings"]["cadastre:not_found"] == 1
        assert set(summary.keys()) == {
            "rows",
            "findings",
            "resolutions",
            "on_asked_parcel",
            "measured_displacements_m",
            "measured_max_m",
            "measured_over_25m",
            "unresolved_rows",
        }

    def test_a_beyond_neighbours_row_nothing_resolved_further_is_unresolved(self):
        """Without a `resolved_distance_m`, a lower bound alone never turns
        into a measurement -- `at_least_m` on its own does not feed
        `measured_displacements_m`."""
        rows = [{"finding": audit.BEYOND_NEIGHBOURS, "at_least_m": 20.0}]

        summary = audit.summarise(rows)

        assert summary["measured_displacements_m"] == []
        assert summary["unresolved_rows"] == 1


class TestPlaceIndex:
    """The join between the coordinate lookup's codes and the address lookup's names."""

    @staticmethod
    def _payloads():
        return {
            cadastre_locate.PROVINCES_URL: {
                "consulta_provincieroResult": {
                    "control": {"cuprov": 2},
                    "provinciero": {
                        "prov": [
                            {"cpine": "03", "np": "ALACANT"},
                            {"cpine": "27", "np": "LUGO"},
                        ]
                    },
                }
            },
            cadastre_locate.MUNICIPALITIES_URL: {
                "consulta_municipieroResult": {
                    "control": {"cumun": 2},
                    "municipiero": {
                        "muni": [
                            {"loine": {"cp": "27", "cm": "19"}, "nm": "FOZ"},
                            {"loine": {"cp": "27", "cm": "18"}, "nm": "A FONSAGRADA"},
                        ]
                    },
                }
            },
        }

    def _index(self, monkeypatch, counter):
        payloads = self._payloads()

        def fake(url, params):
            counter.append(url)
            return payloads[url]

        monkeypatch.setattr(cadastre_locate, "_fetch_json", fake)
        return cadastre_locate.PlaceIndex()

    def test_a_parcels_codes_become_catastros_own_names(self, monkeypatch):
        index = self._index(monkeypatch, [])
        place = index.place_of({"province_code": "27", "municipality_code": "19"})
        assert place == {"province": "LUGO", "municipality": "FOZ"}

    def test_a_leading_zero_does_not_hide_a_province(self, monkeypatch):
        # The provinces index spells Alicante "03" and the coordinate answer
        # spells it "3"; joining on the raw strings found nothing for every
        # province numbered below ten.
        index = self._index(monkeypatch, [])
        assert index.province("3") == "ALACANT"
        assert index.province("03") == "ALACANT"

    def test_each_index_is_fetched_once_however_many_rows_ask(self, monkeypatch):
        calls = []
        index = self._index(monkeypatch, calls)
        for _ in range(5):
            index.place_of({"province_code": "27", "municipality_code": "19"})
        assert calls == [
            cadastre_locate.PROVINCES_URL,
            cadastre_locate.MUNICIPALITIES_URL,
        ]

    def test_an_index_that_could_not_be_fetched_answers_none_not_a_guess(
        self, monkeypatch
    ):
        def refuse(url, params):
            raise cadastre_locate.CadastreError(cadastre_locate.UNAVAILABLE, "down")

        monkeypatch.setattr(cadastre_locate, "_fetch_json", refuse)
        index = cadastre_locate.PlaceIndex()
        assert index.province("27") is None
        assert (
            index.place_of({"province_code": "27", "municipality_code": "19"}) is None
        )


class TestStreetKey:
    """Word order is dropped because Catastro moves the article to the end."""

    def test_the_article_suffix_and_the_query_spelling_share_one_key(self):
        assert cadastre_locate.street_key("RETELA,LA") == cadastre_locate.street_key(
            "La Retela"
        )

    def test_a_leading_street_type_word_is_tried_off_but_not_forced_off(self):
        keys = cadastre_locate._street_keys("calle Tarancon")
        assert cadastre_locate.street_key("TARANCON") in keys
        assert cadastre_locate.street_key("CALLE TARANCON") in keys

    def test_a_type_word_that_is_part_of_the_name_still_matches(self):
        # Catastro's own "AV RAMBLA DE LA LLIBERTAT" is a rambla inside an
        # avenida, so trying the word off must not be the only key.
        index = [
            {
                "code": "1",
                "sigla": "AV",
                "name": "RAMBLA DE LA LIBERTAD",
                "normalized": "RAMBLA DE LA LIBERTAD",
            }
        ]
        assert (
            cadastre_locate.match_street(index, "rambla de la Libertad")["status"]
            == cadastre_locate.OK
        )

    def test_a_preposition_does_not_hide_a_street(self):
        # Catastro indexes the avenue as "CASTRELOS"; the query names it
        # "avenida de Castrelos". The "de" alone left row 1759 unplaced.
        index = [
            {
                "code": "1",
                "sigla": "AV",
                "name": "CASTRELOS",
                "normalized": "CASTRELOS",
            }
        ]
        matched = cadastre_locate.match_street(index, "avenida de Castrelos")
        assert matched["status"] == cadastre_locate.OK
        assert matched["street"]["name"] == "CASTRELOS"

    def test_an_exact_spelling_wins_over_a_word_set_collision(self):
        # Foz indexes one street twice, "RU XOIÑA" and "RU XOIÑA, DA". Their
        # word sets collide, so the relaxed comparison alone called a query
        # that matches one of them character for character ambiguous, and the
        # row the issue named (1734) went unplaced.
        index = [
            {"code": "418", "sigla": "RU", "name": "XOIÑA", "normalized": "XOINA"},
            {
                "code": "311",
                "sigla": "RU",
                "name": "XOIÑA, DA",
                "normalized": "XOINA DA",
            },
        ]
        matched = cadastre_locate.match_street(index, "Calle Xoiña")
        assert matched["status"] == cadastre_locate.OK
        assert matched["street"]["code"] == "418"

    def test_two_genuinely_different_streets_are_still_ambiguous(self):
        index = [
            {"code": "1", "sigla": "CL", "name": "XOIÑA", "normalized": "XOINA"},
            {"code": "2", "sigla": "LG", "name": "XOIÑA", "normalized": "XOINA"},
        ]
        assert (
            cadastre_locate.match_street(index, "Calle Xoiña")["status"]
            == cadastre_locate.STREET_AMBIGUOUS
        )

    def test_a_name_that_is_nothing_but_articles_keeps_them(self):
        # An empty key would match every other empty key.
        assert cadastre_locate.street_key("LA") == ("LA",)

    def test_different_words_never_collide(self):
        assert cadastre_locate.street_key("LA IGLESIA") != cadastre_locate.street_key(
            "LA BARROSA"
        )


class TestResolveAskedAddress:
    """The second pass: what it answers, and what it refuses to answer."""

    @staticmethod
    def _row():
        return {
            "id": 25,
            "query": "calle Tarancon, 6, Lo Marabu, Rojales, Spain",
            "asked": "6",
            "lat": 38.0513343,
            "lon": -0.722589,
            "place": {"province": "ALICANTE", "municipality": "ROJALES"},
        }

    def test_it_measures_to_the_parcels_own_reference_point(self, monkeypatch):
        monkeypatch.setattr(
            audit,
            "streets",
            lambda province, municipality: {
                "status": cadastre_locate.OK,
                "streets": [
                    {
                        "code": "372",
                        "sigla": "CL",
                        "name": "TARANCON",
                        "normalized": "TARANCON",
                    }
                ],
            },
        )
        monkeypatch.setattr(
            audit,
            "parcel_for_address",
            lambda *args: {
                "status": cadastre_locate.OK,
                "reference": "0139702YH0103N",
                "address": "CL TARANCON 6 03170 ROJALES (ALICANTE)",
            },
        )
        monkeypatch.setattr(
            audit,
            "fetch_parcel",
            lambda reference: {"reference_point": {"lat": 38.0538, "lon": -0.7255}},
        )

        out = audit.resolve_asked_address(self._row(), cadastre_locate.PlaceIndex())
        assert out["resolution"] == audit.RESOLVED
        assert out["asked_reference"] == "0139702YH0103N"
        # The stored ROOFTOP is a few hundred metres from the address asked.
        assert 300 < out["resolved_distance_m"] < 400

    def test_an_unmatched_street_is_named_and_never_a_distance(self, monkeypatch):
        monkeypatch.setattr(
            audit,
            "streets",
            lambda province, municipality: {
                "status": cadastre_locate.OK,
                "streets": [
                    {
                        "code": "1",
                        "sigla": "CL",
                        "name": "OTRA",
                        "normalized": "OTRA",
                    }
                ],
            },
        )
        out = audit.resolve_asked_address(self._row(), cadastre_locate.PlaceIndex())
        assert out["resolution"] == cadastre_locate.STREET_NOT_MATCHED
        assert "resolved_distance_m" not in out

    def test_a_row_with_no_place_is_not_looked_up_at_all(self, monkeypatch):
        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("no place means no request")

        monkeypatch.setattr(audit, "streets", explode)
        row = dict(self._row(), place=None)
        out = audit.resolve_asked_address(row, cadastre_locate.PlaceIndex())
        assert "resolved_distance_m" not in out

    def test_a_parcel_without_a_reference_point_is_not_a_zero_distance(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            audit,
            "streets",
            lambda province, municipality: {
                "status": cadastre_locate.OK,
                "streets": [
                    {
                        "code": "372",
                        "sigla": "CL",
                        "name": "TARANCON",
                        "normalized": "TARANCON",
                    }
                ],
            },
        )
        monkeypatch.setattr(
            audit,
            "parcel_for_address",
            lambda *args: {"status": cadastre_locate.OK, "reference": "0139702YH0103N"},
        )
        monkeypatch.setattr(audit, "fetch_parcel", lambda reference: {})
        out = audit.resolve_asked_address(self._row(), cadastre_locate.PlaceIndex())
        assert out["resolution"] == "parcel has no reference point"
        assert "resolved_distance_m" not in out
