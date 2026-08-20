"""The cadastre client, against the answers Catastro really gave (#430).

Every fixture in `tests/data/catastro_*` is a recorded live response, taken on
2026-08-20: the Bayas parcel (property 774) in EPSG:4326 and in EPSG:25829, an
urban parcel in 4326, the `Consulta_DNPRC` payload for the same reference, and
**two real refusals** -- both delivered as `200 OK` with the failure in the
body, which is the whole reason this module never reads a status code as an
answer.

What is pinned here is what would otherwise be rediscovered the expensive way:
that a refusal is not an absence of parcel, that the request budget is exact
because there are no retries, that the CRS is chosen from the parcel's own
reference point and checked against the response, and that the declared area
catches a parse error but says nothing about the zone.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import cadastre_service
from tests import setup_test_environment

DATA = Path(__file__).parent / "data"

PARCEL_4326 = (DATA / "catastro_parcel_33016A00300153_4326.gml").read_text(
    encoding="latin-1"
)
PARCEL_25829 = (DATA / "catastro_parcel_33016A00300153_25829.gml").read_text(
    encoding="latin-1"
)
# The same parcel fetched in the *neighbouring* zone. Bayas sits at -6.027, so
# 25829 is its zone and 25830 is not -- and this file is what proves the
# `srsName` check does something, because its area agrees to within 0.01% and
# the area cross-check therefore cannot tell the two apart.
PARCEL_WRONG_ZONE = (
    DATA / "catastro_parcel_33016A00300153_wrong_zone_25830.gml"
).read_text(encoding="latin-1")
URBAN_4326 = (DATA / "catastro_parcel_9872023VH5797S_4326.gml").read_text(
    encoding="latin-1"
)
DNPRC_OK = json.loads(
    (DATA / "catastro_dnprc_33016A003001530001HQ.json").read_text(encoding="utf-8")
)
DNPRC_BAD_LENGTH = json.loads(
    (DATA / "catastro_dnprc_unknown_reference.json").read_text(encoding="utf-8")
)
DNPRC_MALFORMED = json.loads(
    (DATA / "catastro_dnprc_no_such_parcel.json").read_text(encoding="utf-8")
)

BAYAS = "33016A003001530001HQ"


class _Response:
    def __init__(self, *, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestTheReference:
    def test_it_takes_the_three_lengths_the_services_take(self):
        assert cadastre_service.normalize_reference(BAYAS) == BAYAS
        assert (
            cadastre_service.normalize_reference("33016A00300153") == "33016A00300153"
        )
        assert cadastre_service.normalize_reference("9872023VH5797S0001WX") is not None

    def test_it_tidies_what_a_person_pastes(self):
        assert cadastre_service.normalize_reference(" 33016a0030015300 01hq ") == BAYAS
        assert cadastre_service.normalize_reference("33016A0030015300-01HQ") == BAYAS

    def test_it_refuses_what_is_not_one(self):
        for text in ("", None, "hello", "1234", "33016A0030015300 01HQ EXTRA"):
            assert cadastre_service.normalize_reference(text) is None

    def test_the_wfs_gets_the_documented_fourteen(self):
        # The live service accepts twenty as well -- verified -- but the
        # documented contract is fourteen and undocumented tolerance is not a
        # contract.
        assert cadastre_service.parcel_reference(BAYAS) == "33016A00300153"


class TestTheZone:
    def test_it_follows_the_utm_meridians(self):
        # Bayas sits at -6.027, three kilometres west of the 29/30 boundary,
        # so it is genuinely a zone-29 parcel even though Asturias is usually
        # spoken of as zone 30.
        assert cadastre_service.metric_epsg_for(43.5746, -6.0271) == 25829
        assert cadastre_service.metric_epsg_for(43.36, -5.84) == 25830
        assert cadastre_service.metric_epsg_for(39.57, 2.65) == 25831

    def test_the_canaries_have_no_metric_zone_here(self):
        # REGCAN95, not ETRS89, and the WFS document lists no 4082/4083 output.
        # Asking for 25830 would return numbers from a datum the islands do not
        # use -- Catastro reprojects whatever it is asked for.
        assert cadastre_service.metric_epsg_for(28.1, -15.43) is None


class TestParsingTheOutline:
    def test_it_reads_the_real_parcel(self):
        parsed = cadastre_service._parse_gml(PARCEL_25829, 25829)
        assert parsed["declared_area_m2"] == 6193
        assert len(parsed["rings"]) == 1
        assert len(parsed["rings"][0]) == 45

    def test_it_reads_the_reference_point_as_lat_lon(self):
        parsed = cadastre_service._parse_gml(PARCEL_4326, 4326)
        lat, lon = parsed["reference_point"]
        # Catastro's 4326 output is lat lon, not lon lat. Reading it the other
        # way puts this parcel in the Indian Ocean.
        assert 43.5 < lat < 43.6
        assert -6.1 < lon < -6.0

    def test_a_response_in_another_crs_is_malformed_and_not_measured(self):
        # The service reprojects into whatever it is asked for and says so in
        # `srsName`; a mismatch means the numbers are not what the metrics
        # would be computed against.
        with pytest.raises(cadastre_service.CadastreError) as caught:
            cadastre_service._parse_gml(PARCEL_4326, 25830)
        assert caught.value.state == cadastre_service.MALFORMED

    def test_an_empty_collection_is_the_one_measured_negative(self):
        empty = (
            '<?xml version="1.0"?><FeatureCollection '
            'xmlns="http://www.opengis.net/wfs/2.0" numberReturned="0">'
            "</FeatureCollection>"
        )
        with pytest.raises(cadastre_service.CadastreError) as caught:
            cadastre_service._parse_gml(empty, 4326)
        assert caught.value.state == cadastre_service.NOT_FOUND

    def test_a_truncated_coordinate_list_is_refused(self):
        first = PARCEL_25829.split("<gml:posList", 1)[1].split(">", 1)[1].split("<")[0]
        broken = PARCEL_25829.replace(first, first.rsplit(" ", 1)[0])
        with pytest.raises(cadastre_service.CadastreError) as caught:
            cadastre_service._parse_gml(broken, 25829)
        assert caught.value.state == cadastre_service.MALFORMED

    def test_the_neighbouring_zone_is_caught_by_srsname_and_not_by_the_area(self):
        """The two checks, and which one does which job.

        This file is the same parcel in 25830 while the code asked for 25829.
        Its computed area is 6193.5 against a declared 6193 -- inside the 1%
        tolerance -- so the area cross-check passes it happily. Only the
        response's own `srsName` says the numbers are not in the CRS that was
        requested.
        """
        parsed = cadastre_service._parse_gml(PARCEL_WRONG_ZONE, 25830)
        metrics = cadastre_service.shape_metrics(parsed["rings"])
        assert cadastre_service.area_agrees(
            metrics["area_m2"], parsed["declared_area_m2"]
        )

        with pytest.raises(cadastre_service.CadastreError) as caught:
            cadastre_service._parse_gml(PARCEL_WRONG_ZONE, 25829)
        assert caught.value.state == cadastre_service.MALFORMED


class TestTheShapeMetrics:
    def test_they_reproduce_the_numbers_that_rejected_774(self):
        parsed = cadastre_service._parse_gml(PARCEL_25829, 25829)
        metrics = cadastre_service.shape_metrics(parsed["rings"])

        # Computed from the real outline, in metres, against the figures in
        # the ticket: 6,193 m2, a 120 x 146 m box it fills 0.35 of, and a
        # Polsby-Popper of 0.30 against ~0.79 for a square.
        assert abs(metrics["area_m2"] - 6193) < 2
        assert 0.30 <= metrics["bbox_fill_ratio"] <= 0.40
        assert 0.25 <= metrics["polsby_popper"] <= 0.35
        assert metrics["vertices"] == 44

    def test_a_regular_parcel_scores_the_other_way(self):
        square = [[(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)]]
        metrics = cadastre_service.shape_metrics(square)
        assert metrics["area_m2"] == 10000.0
        assert metrics["bbox_fill_ratio"] == 1.0
        assert 0.77 <= metrics["polsby_popper"] <= 0.80

    def test_there_is_no_largest_inscribed_square(self):
        """Deliberately absent, and it stays absent until it is defensible.

        A grid over the axis-aligned case underestimates a parcel whose long
        side runs diagonally, by an amount nobody has bounded, and a number
        that decides a purchase must not be an approximation nobody labelled.
        774's own 27 x 27 m figure lives in the owner's rejection reason, as
        the sentence it was.
        """
        metrics = cadastre_service.shape_metrics(
            [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]]
        )
        assert "largest_inscribed_square_m" not in metrics


class TestTheAreaCrossCheck:
    def test_it_catches_a_parse_error(self):
        assert not cadastre_service.area_agrees(3100.0, 6193.0)

    def test_it_does_not_catch_the_neighbouring_zone(self):
        """Measured, and the reason `srsName` is checked separately.

        The same parcel computes 6193.5 m2 in EPSG:25830 and 6192.8 in 25829,
        against a declared 6193. Even a plainly wrong zone -- 25831, which
        returns a negative easting -- lands 1.17% out. So this is an
        area-consistency check and never a CRS check.
        """
        assert cadastre_service.area_agrees(6192.8, 6193.0)
        assert cadastre_service.area_agrees(6193.5, 6193.0)

    def test_the_tolerance_has_a_floor_for_small_parcels(self):
        # areaValue is an integer, so on a 30 m2 garage one square metre is 3%.
        assert cadastre_service.area_agrees(30.9, 30.0)
        assert not cadastre_service.area_agrees(35.0, 30.0)

    def test_no_declared_area_is_not_a_failure(self):
        assert cadastre_service.area_agrees(6193.0, None)


class TestTheAttributes:
    def test_it_reads_the_real_payload(self):
        parsed = cadastre_service.parse_attributes(DNPRC_OK)
        assert parsed["class"] == "UR"
        assert parsed["municipality"] == "CASTRILLON"
        assert parsed["poligono"] == 3
        assert parsed["parcela"] == 153
        assert parsed["paraje"] == "TRUEVANO"
        assert parsed["subparcels"][0]["use"].startswith("PRADO")

    def test_the_class_comes_from_the_payload_and_not_the_reference_shape(self):
        # 33016A... is a rustic-format reference and this bien is URBANO.
        # Inferring the class from the reference would have said the opposite.
        assert BAYAS.startswith("33016A")
        assert cadastre_service.parse_attributes(DNPRC_OK)["class"] == "UR"

    def test_a_refusal_inside_a_200_is_a_refusal(self):
        for payload in (DNPRC_BAD_LENGTH, DNPRC_MALFORMED):
            with pytest.raises(cadastre_service.CadastreError) as caught:
                cadastre_service.parse_attributes(payload)
            # Neither of these is "there is no such parcel": both are Catastro
            # rejecting the request. Recording them as `not_found` would write
            # a fact about the world from a fact about our own typing.
            assert caught.value.state == cadastre_service.MALFORMED

    def test_a_missing_parcel_is_not_found(self):
        payload = {
            "consulta_dnprcResult": {
                "control": {"cuerr": 1},
                "lerr": [{"cod": "9", "des": "LA REFERENCIA CATASTRAL NO EXISTE"}],
            }
        }
        with pytest.raises(cadastre_service.CadastreError) as caught:
            cadastre_service.parse_attributes(payload)
        assert caught.value.state == cadastre_service.NOT_FOUND


def _responses(*, mapped=PARCEL_4326, metric=PARCEL_25829, attributes=DNPRC_OK):
    """A fake transport that answers the three calls in the order they happen."""
    calls = []

    def fake_get(url, params, **kwargs):
        calls.append((url, dict(params)))
        if "wfsCP" in url:
            if params.get("srsname") == "EPSG::4326":
                if isinstance(mapped, Exception):
                    raise mapped
                return _Response(text=mapped)
            if isinstance(metric, Exception):
                raise metric
            return _Response(text=metric)
        if isinstance(attributes, Exception):
            raise attributes
        return _Response(payload=attributes)

    return fake_get, calls


class TestTheRun:
    @pytest.fixture(autouse=True)
    def _no_cache(self):
        # Every one of these fetches a reference the others also use; a cache
        # hit would make the request-budget assertions meaningless.
        with (
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            yield

    def _run(self, **kwargs):
        fake_get, calls = _responses(**kwargs)
        with patch.object(cadastre_service, "_get", side_effect=fake_get):
            return cadastre_service.fetch_parcel(BAYAS), calls

    def test_three_requests_and_not_one_more(self):
        block, calls = self._run()
        # The whole point of `max_attempts=1`: this number is exact, so the
        # route's 5/minute really does cap Catastro at fifteen a minute.
        assert len(calls) == 3
        assert block["run_state"] == cadastre_service.RUN_OK

    def test_the_metric_zone_comes_from_the_parcels_own_reference_point(self):
        _, calls = self._run()
        metric_call = [
            params
            for url, params in calls
            if params.get("srsname") not in (None, "EPSG::4326")
        ]
        assert metric_call, calls
        # -6.027 is west of the meridian, so 29 and not the 30 the province is
        # usually spoken of as.
        assert metric_call[0]["srsname"] == "EPSG::25829"

    def test_it_measures_the_parcel(self):
        block, _ = self._run()
        assert abs(block["geometry"]["area_m2"] - 6193) < 2
        assert block["geometry"]["epsg"] == 25829
        assert block["attributes"]["paraje"] == "TRUEVANO"
        assert block["outline_4326"][0][0] == [-6.026617, 43.574905]

    def test_a_refused_attribute_call_degrades_the_run_and_keeps_the_outline(self):
        import requests

        block, _ = self._run(attributes=requests.ConnectionError("down"))
        # Advisory, not decisive: the shape metrics are what this is for.
        assert block["run_state"] == cadastre_service.RUN_DEGRADED
        assert block["geometry"]["area_m2"] > 0
        assert block["sources"]["attributes"]["status"] == cadastre_service.UNAVAILABLE

    def test_a_refused_outline_makes_the_run_unavailable(self):
        import requests

        block, calls = self._run(mapped=requests.ConnectionError("down"))
        assert block["run_state"] == cadastre_service.RUN_UNAVAILABLE
        assert "geometry" not in block
        # And the metric call is not attempted at all: without the map copy
        # there is no reference point, and guessing the zone from the
        # listing's own coordinate would measure the village centre.
        assert len([1 for url, _ in calls if "wfsCP" in url]) == 1

    def test_an_area_that_disagrees_is_malformed_rather_than_a_measurement(self):
        # Half the outline removed: the remaining ring is a real polygon, so
        # only the declared area can tell that something was lost.
        first = PARCEL_25829.split("<gml:posList", 1)[1].split(">", 1)[1].split("<")[0]
        corner = " ".join(first.split()[:8])
        truncated = PARCEL_25829.replace(first, corner)
        block, _ = self._run(metric=truncated)
        assert (
            block["sources"]["metric_geometry"]["status"] == cadastre_service.MALFORMED
        )
        assert "geometry" not in block
        assert block["run_state"] == cadastre_service.RUN_UNAVAILABLE

    def test_only_a_measured_source_is_cached(self):
        import requests

        writes = []
        with (
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(
                cadastre_service,
                "_cache_set",
                side_effect=lambda k, v: writes.append(k),
            ),
        ):
            fake_get, _ = _responses(attributes=requests.ConnectionError("down"))
            with patch.object(cadastre_service, "_get", side_effect=fake_get):
                cadastre_service.fetch_parcel(BAYAS)
        # A refusal is not a fact about the parcel, so caching it would answer
        # the next press with a failure nobody re-checked.
        assert not any("attributes" in key for key in writes)
        assert any("outline" in key for key in writes)


class TestWritingItDown:
    @pytest.fixture
    def app(self):
        setup_test_environment()
        application = create_app()
        application.config["TESTING"] = True
        application.config["WTF_CSRF_ENABLED"] = False
        with application.app_context():
            db.create_all()
            yield application
            db.session.remove()
            db.drop_all()

    @pytest.fixture
    def prop(self, app):
        profile = SearchProfile(name="Asturias", is_active=True, is_default=True)
        db.session.add(profile)
        db.session.commit()
        row = Property(
            source_email_id="bayas",
            title="Bayas, Castrillón",
            search_profile_id=profile.id,
        )
        db.session.add(row)
        db.session.commit()
        return row

    def test_the_column_and_the_block_are_written_together(self, app, prop):
        fake_get, _ = _responses()
        with (
            patch.object(cadastre_service, "_get", side_effect=fake_get),
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            cadastre_service.apply_to_property(prop, BAYAS)

        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert stored.cadastral_reference == BAYAS
        assert stored.enrichment["cadastre"]["attributes"]["paraje"] == "TRUEVANO"

    def test_a_refusal_never_overwrites_what_was_measured(self, app, prop):
        import requests

        with (
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            fake_get, _ = _responses()
            with patch.object(cadastre_service, "_get", side_effect=fake_get):
                cadastre_service.apply_to_property(prop, BAYAS)

            fake_get, _ = _responses(attributes=requests.ConnectionError("down"))
            with patch.object(cadastre_service, "_get", side_effect=fake_get):
                block = cadastre_service.apply_to_property(prop, BAYAS)

        # The refused source keeps the answer somebody already has, and says
        # in `sources` that this run did not fetch it -- the #98 split, per
        # source rather than per run.
        assert block["attributes"]["paraje"] == "TRUEVANO"
        assert block["sources"]["attributes"]["status"] == cadastre_service.UNAVAILABLE
        assert block["sources"]["attributes"]["kept_previous"] is True

    def test_a_different_parcel_replaces_everything(self, app, prop):
        with (
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            fake_get, _ = _responses()
            with patch.object(cadastre_service, "_get", side_effect=fake_get):
                cadastre_service.apply_to_property(prop, BAYAS)

            other = "9872023VH5797S0001WX"
            fake_get, _ = _responses(
                mapped=URBAN_4326,
                metric=URBAN_4326,
                attributes={
                    "consulta_dnprcResult": {
                        "control": {"cuerr": 1},
                        "lerr": [{"cod": "9", "des": "no"}],
                    }
                },
            )
            with patch.object(cadastre_service, "_get", side_effect=fake_get):
                block = cadastre_service.apply_to_property(prop, other)

        # Nothing of the old parcel may survive into a block describing a new
        # one -- that would be a measurement of somewhere else.
        assert block["reference"] == other
        assert "attributes" not in block


class TestTheRoute:
    @pytest.fixture
    def app(self):
        setup_test_environment()
        application = create_app()
        application.config["TESTING"] = True
        application.config["WTF_CSRF_ENABLED"] = False
        with application.app_context():
            db.create_all()
            yield application
            db.session.remove()
            db.drop_all()

    @pytest.fixture
    def prop(self, app):
        profile = SearchProfile(name="Asturias", is_active=True, is_default=True)
        db.session.add(profile)
        db.session.commit()
        row = Property(
            source_email_id="bayas", title="Bayas", search_profile_id=profile.id
        )
        db.session.add(row)
        db.session.commit()
        return row

    def test_it_records_and_measures(self, app, prop):
        client = app.test_client()
        fake_get, _ = _responses()
        with (
            patch.object(cadastre_service, "_get", side_effect=fake_get),
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            response = client.post(
                f"/properties/{prop.id}/cadastre",
                data={"cadastral_reference": BAYAS.lower()},
            )
        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Property, prop.id).cadastral_reference == BAYAS

    def test_a_reference_that_is_not_one_makes_no_request(self, app, prop):
        client = app.test_client()
        with patch.object(cadastre_service, "_get") as transport:
            response = client.post(
                f"/properties/{prop.id}/cadastre",
                data={"cadastral_reference": "not a reference"},
            )
        assert response.status_code == 302
        transport.assert_not_called()
        db.session.expire_all()
        assert db.session.get(Property, prop.id).cadastral_reference is None

    def test_a_refusal_still_records_the_reference(self, app, prop):
        import requests

        client = app.test_client()
        fake_get, _ = _responses(mapped=requests.ConnectionError("down"))
        with (
            patch.object(cadastre_service, "_get", side_effect=fake_get),
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            client.post(
                f"/properties/{prop.id}/cadastre", data={"cadastral_reference": BAYAS}
            )
        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        # The parcel this listing sits on is a fact about the listing whether
        # or not Catastro answered this minute.
        assert stored.cadastral_reference == BAYAS

    def test_clearing_makes_no_request_and_removes_the_block(self, app, prop):
        client = app.test_client()
        fake_get, _ = _responses()
        with (
            patch.object(cadastre_service, "_get", side_effect=fake_get),
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            client.post(
                f"/properties/{prop.id}/cadastre", data={"cadastral_reference": BAYAS}
            )

        with patch.object(cadastre_service, "_get") as transport:
            client.post(
                f"/properties/{prop.id}/cadastre", data={"cadastral_reference": ""}
            )
        transport.assert_not_called()

        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert stored.cadastral_reference is None
        # The measurement goes with it: a parcel this listing no longer claims
        # is a description of somewhere else.
        assert "cadastre" not in (stored.enrichment or {})

    def test_the_page_renders_the_block(self, app, prop):
        client = app.test_client()
        fake_get, _ = _responses()
        with (
            patch.object(cadastre_service, "_get", side_effect=fake_get),
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            client.post(
                f"/properties/{prop.id}/cadastre", data={"cadastral_reference": BAYAS}
            )

        response = client.get(f"/properties/{prop.id}")
        body = response.get_data(as_text=True)
        # This route degrades by redirect, so a 200 is itself an assertion.
        assert response.status_code == 200
        assert "An error occurred while loading property details" not in body
        assert 'id="cadastral-parcel"' in body
        assert BAYAS in body
        assert "TRUEVANO" in body

    def test_the_sixth_press_in_a_minute_is_refused(self, app, prop):
        """It reaches a third party that bans an IP for ten days on abuse.

        Behavioural rather than introspective: what matters is that a sixth
        press does not leave, and reading the decorator back would pass over a
        limiter that was disabled somewhere else. The budget this bounds is
        three outbound requests per press, so five presses is fifteen requests
        a minute against an endpoint that publishes no limit at all.
        """
        from app import limiter

        limiter.enabled = True
        limiter.reset()
        client = app.test_client()
        fake_get, calls = _responses()

        statuses = []
        with (
            patch.object(cadastre_service, "_get", side_effect=fake_get),
            patch.object(cadastre_service, "_cache_get", return_value=None),
            patch.object(cadastre_service, "_cache_set"),
        ):
            for _ in range(6):
                statuses.append(
                    client.post(
                        f"/properties/{prop.id}/cadastre",
                        data={"cadastral_reference": BAYAS},
                    ).status_code
                )

        assert statuses[:5] == [302] * 5, statuses
        assert statuses[5] == 429, statuses
        # And the refused press really did not reach Catastro.
        assert len(calls) == 15, len(calls)
