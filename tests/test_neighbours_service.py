"""Real parcel/footprint calibration, refusal boundaries and locked writes."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
from shapely.geometry import box, MultiPolygon
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()
from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services import (  # noqa: E402
    cadastre_service as c,
    cadastre_locate as locate,
    neighbours_service as n,
    taste_descriptors as d,
)
from services.enrichment_write import EnrichmentWriteContractError  # noqa: E402
from services.property_enrichment_service import PropertyEnrichmentService  # noqa: E402
from utils import backfill_neighbours as cli  # noqa: E402
from utils import http as h  # noqa: E402

# Catastro's live answers for the owner's two references (rows 969 and 1282),
# trimmed to the buildings within 100 m, the parcel's own building and two or
# three beyond the radius. Trimming removes only features farther than the
# kept ones, so every measured count and distance is the live answer's.
DATA = Path(__file__).parent / "data" / "neighbours"
REF = "4463719NH1946S"


def prop(**kwargs):
    fields = dict(
        id=969,
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
        municipality="Miño",
        description="",
        property_category="house",
        area=100,
        plot_area=1000,
        area_type="built",
        attributes={},
        environment={},
        travel={},
    )
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def payload(p, **kwargs):
    result = dict(
        status="ok",
        subject=n.subject(p),
        source="catastro_inspire_buildings",
        reference=REF,
        epsg=25829,
        searched_m=100,
        measured_at="2026-11-04T13:00:00Z",
        buildings_within_50m=0,
        buildings_within_100m=0,
        nearest_building_m=None,
        agricultural_buildings_within_100m=0,
        industrial_buildings_within_100m=0,
        unknown_use_buildings_within_100m=0,
    )
    result.update(kwargs)
    return result


def _measure_reference(row, reference):
    parcel = n._shape(
        c._parse_gml((DATA / f"{row}_parcel.gml").read_text(), 25829)["polygons"]
    )
    buildings = n.parse_buildings((DATA / f"{row}_buildings.gml").read_text(), 25829)
    return n.measure_buildings(parcel, reference, buildings)


@pytest.mark.parametrize(
    "row,reference,within100,nearest",
    [(969, REF, 7, 17.8646942317), (1282, "15044A01901483", 11, 3.7759133525)],
)
def test_owner_reference_calibration_counts(row, reference, within100, nearest):
    measured = _measure_reference(row, reference)
    assert measured == dict(
        buildings_within_50m=3,
        buildings_within_100m=within100,
        nearest_building_m=pytest.approx(nearest),
        agricultural_buildings_within_100m=0,
        industrial_buildings_within_100m=0,
        unknown_use_buildings_within_100m=0,
    )


@pytest.mark.parametrize(
    "row,reference,within100",
    [(969, REF, 7), (1282, "15044A01901483", 11)],
)
def test_owner_reference_calibration_keeps_privacy_unknown(row, reference, within100):
    measured = _measure_reference(row, reference)
    p = prop(cadastral_reference=reference)
    p.enrichment["neighbours"] = payload(p, reference=reference, **measured)
    descriptor = d.build_descriptor(p)
    assert descriptor["aspects"]["neighbor_privacy"][0]["status"] == "unknown"
    assert descriptor["aspects"]["nearby_buildings"][0]["value"] == within100


def test_distance_is_from_entire_parcel_excluding_own_not_just_centroid():
    parcel = box(0, 0, 1000, 10)
    buildings = [
        dict(parcels={REF}, uses={"1_residential"}, shape=box(1, 1, 10, 5)),
        dict(parcels={"other"}, uses={"2_agriculture"}, shape=box(1049, 0, 1055, 5)),
        dict(parcels={"another"}, uses={"3_industrial"}, shape=box(-100, 0, -99, 2)),
        dict(parcels={"touching"}, uses=set(), shape=box(1000, 0, 1004, 5)),
    ]
    result = n.measure_buildings(parcel, REF, buildings)
    assert result == dict(
        buildings_within_50m=2,
        buildings_within_100m=3,
        nearest_building_m=0,
        agricultural_buildings_within_100m=1,
        industrial_buildings_within_100m=1,
        unknown_use_buildings_within_100m=1,
    )


@pytest.mark.parametrize(
    "row,reference,features", [(969, REF, 10), (1282, "15044A01901483", 14)]
)
def test_owner_fixture_parses_every_building(row, reference, features):
    buildings = n.parse_buildings((DATA / f"{row}_buildings.gml").read_text(), 25829)
    assert len(buildings) == features
    assert len({b["id"] for b in buildings}) == features
    assert any(reference in b["parcels"] for b in buildings)
    assert all(b["shape"].area > 0 for b in buildings)


def test_multipart_and_holes_are_preserved():
    element = ET.fromstring(
        '<root xmlns:g="http://www.opengis.net/gml/3.2"><g:Polygon><g:exterior><g:LinearRing><g:posList>0 0 10 0 10 10 0 10 0 0</g:posList></g:LinearRing></g:exterior><g:interior><g:LinearRing><g:posList>2 2 8 2 8 8 2 8 2 2</g:posList></g:LinearRing></g:interior></g:Polygon><g:Polygon><g:exterior><g:LinearRing><g:posList>20 0 30 0 30 10 20 10 20 0</g:posList></g:LinearRing></g:exterior></g:Polygon></root>'
    )
    shape = n._shape(c.surface_polygons(element))
    assert shape.area == 164
    assert shape.distance(box(4, 4, 5, 5)) == 2
    assert shape.distance(box(31, 1, 32, 2)) == 1


def _square(x, y, size=1):
    return [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]


@pytest.mark.parametrize(
    "patches,area,parts",
    [
        # Patches tiling one footprint share an edge: one rectangle, area 2.
        (
            [
                [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
                [[(1, 0), (2, 0), (2, 1), (1, 1), (1, 0)]],
            ],
            2,
            1,
        ),
        # Touching in one point stays two parts of one footprint.
        ([[_square(0, 0)], [_square(1, 1)]], 2, 2),
        # Overlapping patches cover their union, never the sum of areas.
        ([[_square(0, 0, 2)], [_square(1, 0, 2)]], 6, 1),
        # A holed patch beside an adjacent one keeps its hole.
        ([[_square(0, 0, 10), _square(2, 2, 6)], [_square(10, 0, 10)]], 164, 1),
    ],
)
def test_patches_of_one_footprint_are_one_valid_shape(patches, area, parts):
    shape = n._shape(patches)
    assert shape.is_valid
    assert shape.area == area
    assert len(shape.geoms) == parts


@pytest.mark.parametrize(
    "patches",
    [
        [],
        # A self-crossing patch is refused, even beside a sound adjacent one.
        [[[(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]], [_square(1, 0)]],
        # An open ring is refused here too: stored polygons reach this check.
        [[[(0, 0), (1, 0), (1, 1), (0, 1)]], [_square(1, 0)]],
        [[[[0, 0], [1, 0], [1, 1], [0, 1]]]],
        # A patch with no area.
        [[[(0, 0), (1, 0), (2, 0), (0, 0)]], [_square(1, 0)]],
        [[[(0, 0), (1, 0), (1, float("nan")), (0, 1), (0, 0)]]],
    ],
)
def test_broken_patch_still_refuses_the_feature(patches):
    with pytest.raises(c.CadastreError, match="invalid polygon surface"):
        n._shape(patches)


def test_adjacent_buildings_tiled_by_patches_stay_two_buildings():
    root = ET.fromstring((DATA / "969_buildings.gml").read_text())
    for extra in list(root)[2:]:
        root.remove(extra)
    for index, member in enumerate(root):
        building = member[0]
        building.set(f"{n.GML}id", f"ES.TEST.{index}")
        patches = building.find(f".//{n.GML}patches")
        assert patches is not None
        template = patches[0]
        for patch in list(patches):
            patches.remove(patch)
        for left in (2 * index, 2 * index + 1):
            patch = deepcopy(template)
            pos = patch.find(f".//{n.GML}posList")
            assert pos is not None
            pos.text = " ".join(f"{v:g}" for xy in _square(left, 0) for v in xy)
            pos.set("count", "5")
            patches.append(patch)
    buildings = n.parse_buildings(ET.tostring(root, encoding="unicode"), 25829)
    assert [b["id"] for b in buildings] == ["ES.TEST.0", "ES.TEST.1"]
    assert buildings[0]["shape"].equals(box(0, 0, 2, 1))
    assert buildings[1]["shape"].equals(box(2, 0, 4, 1))


@pytest.mark.parametrize(
    "body",
    [
        "<html/>",
        "<ExceptionReport/>",
        '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" numberMatched="1" numberReturned="0"/>',
        '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" numberMatched="unknown" numberReturned="0"/>',
        '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" numberMatched="²" numberReturned="0"/>',
        '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0"/>',
        '<FeatureCollection xmlns="http://www.opengis.net/gml/3.2" next="page2"/>',
        '<FeatureCollection xmlns="http://www.opengis.net/gml/3.2"/>',
    ],
)
def test_incomplete_or_refused_collection_is_not_absence(body):
    with pytest.raises(c.CadastreError):
        n.parse_buildings(body, 25829)


def test_empty_complete_collection_is_a_measured_absence():
    assert (
        n.parse_buildings(
            '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" numberMatched="0" numberReturned="0"/>',
            25829,
        )
        == []
    )


def test_wrong_crs_and_duplicate_ids_refuse():
    body = (DATA / "969_buildings.gml").read_text()
    with pytest.raises(c.CadastreError):
        n.parse_buildings(body, 25830)
    root = ET.fromstring(body)
    root.append(deepcopy(root[0]))
    with pytest.raises(c.CadastreError):
        n.parse_buildings(ET.tostring(root, encoding="unicode"), 25829)


def test_precise_requires_unique_zero_distance_parcel(monkeypatch):
    p = prop(cadastral_reference=None)
    monkeypatch.setattr(
        locate,
        "parcels_at",
        lambda *_: dict(status="ok", parcels=[dict(reference=REF, distance_m=0)]),
    )
    assert n._resolve_parcel(p)["reference"] == REF
    monkeypatch.setattr(
        locate,
        "parcels_at",
        lambda *_: dict(status="ok", parcels=[dict(reference=REF, distance_m=2)]),
    )
    assert n._resolve_parcel(p)["status"] == "no_parcel"
    for kept in (123, REF.lower()):  # unreadable or unnormalised: no conflict
        p = prop(enrichment={"cadastre": {"reference": kept}})
        assert n._resolve_parcel(p) == {"status": "ok", "reference": REF}


def test_privacy_circle_never_reaches_coordinate_lookup(monkeypatch):
    p = prop(cadastral_reference=None, location_accuracy="approximate")
    mock = Mock(side_effect=AssertionError("privacy circle lookup"))
    monkeypatch.setattr(locate, "parcels_at", mock)
    assert n.NeighboursService().measure(p)["status"] == "no_parcel"
    mock.assert_not_called()


def test_exact_address_can_place_an_approximate_row(monkeypatch):
    p = prop(
        cadastral_reference=None,
        location_accuracy="approximate",
        enrichment={"geocoding": {"query": "Calle Real, 8, Miño"}},
    )
    monkeypatch.setattr(n, "_row_province", lambda _: "15")
    monkeypatch.setattr(locate.PlaceIndex, "province", lambda *_: "A CORUÑA")
    monkeypatch.setattr(
        locate,
        "streets",
        lambda *_: {
            "status": "ok",
            "streets": [{"sigla": "CL", "name": "REAL", "code": "1"}],
        },
    )
    found = Mock(return_value={"status": "ok", "reference": REF})
    monkeypatch.setattr(locate, "parcel_for_address", found)
    monkeypatch.setattr(
        locate, "parcels_at", Mock(side_effect=AssertionError("privacy circle"))
    )
    assert n._resolve_parcel(p)["reference"] == REF
    found.assert_called_once_with(
        "A CORUÑA", "Miño", "CL", "REAL", "8", require_unique_parcel=True
    )


def test_address_multiple_parcels_never_selects_first(monkeypatch):
    def entry(reference):
        return {"rc": {"pc1": reference[:7], "pc2": reference[7:]}}

    monkeypatch.setattr(
        locate,
        "_fetch_json",
        lambda *_: {
            "consulta_dnplocResult": {
                "lrcdnp": {"rcdnp": [entry(REF), entry("15044A01901483")]}
            }
        },
    )
    assert (
        locate.parcel_for_address(
            "P", "M", "CL", "REAL", 8, require_unique_parcel=True
        )["status"]
        == c.MALFORMED
    )


G = 'xmlns:g="http://www.opengis.net/gml/3.2"'
SQUARE = "0 0 10 0 10 10 0 10 0 0"
HOLE = "2 2 8 2 8 8 2 8 2 2"


def _ring(pos=SQUARE, attrs=""):
    return f"<g:LinearRing><g:posList{attrs}>{pos}</g:posList></g:LinearRing>"


def _polygon(*boundaries):
    return f"<root {G}><g:Polygon>{''.join(boundaries)}</g:Polygon></root>"


@pytest.mark.parametrize(
    "gml",
    [
        # Two exteriors: the second must never be read as a hole (area 64).
        _polygon(
            f"<g:exterior>{_ring()}</g:exterior>",
            f"<g:exterior>{_ring(HOLE)}</g:exterior>",
        ),
        _polygon(f"<g:interior>{_ring(HOLE)}</g:interior>"),
        _polygon(f"<g:exterior>{_ring()}{_ring(HOLE)}</g:exterior>"),
        _polygon(
            f"<g:exterior><g:LinearRing><g:posList>{SQUARE}</g:posList>"
            f"<g:posList>{HOLE}</g:posList></g:LinearRing></g:exterior>"
        ),
        _polygon("<g:exterior><g:LinearRing/></g:exterior>"),
        _polygon("<g:exterior/>"),
        _polygon(f"<g:exterior>{_ring('')}</g:exterior>"),
        _polygon(f"<g:exterior>{_ring(SQUARE + ' 7')}</g:exterior>"),
        _polygon(
            f"<g:exterior>{_ring(SQUARE.replace('10 0', '1_0 0', 1))}</g:exterior>"
        ),
        _polygon(
            f"<g:exterior>{_ring(SQUARE.replace('10 0', '١٠ 0', 1))}</g:exterior>"
        ),
        _polygon("<g:exterior>" + _ring(attrs=" count='4'") + "</g:exterior>"),
        _polygon("<g:exterior>" + _ring(attrs=" count='²'") + "</g:exterior>"),
        _polygon("<g:exterior>" + _ring(attrs=" srsDimension='3'") + "</g:exterior>"),
        f"<root {G}><g:MultiSurface><g:surfaceMember><g:Polygon><g:exterior>{_ring()}"
        f"</g:exterior></g:Polygon><g:Polygon><g:exterior>{_ring(HOLE)}</g:exterior>"
        "</g:Polygon></g:surfaceMember></g:MultiSurface></root>",
    ],
)
def test_surface_of_unexpected_cardinality_or_form_is_refused(gml):
    with pytest.raises(c.CadastreError) as refused:
        c.surface_polygons(ET.fromstring(gml))
    assert refused.value.state == c.MALFORMED


def test_declared_point_count_that_agrees_is_read():
    gml = _polygon("<g:exterior>" + _ring(attrs=" count='5'") + "</g:exterior>")
    assert n._shape(c.surface_polygons(ET.fromstring(gml))).area == 100


def _first_building_with(edit):
    root = ET.fromstring((DATA / "969_buildings.gml").read_text())
    building = root[0][0]
    link = next(e for e in building.iter() if e.tag.endswith("}cadastralParcels"))
    edit(building, link)
    return ET.tostring(root, encoding="unicode")


@pytest.mark.parametrize(
    "edit",
    [
        lambda b, link: link.set(f"{n.XLINK}href", "https://x/?refcat="),
        lambda b, link: link.set(f"{n.XLINK}href", "https://x/?refcat=not-a-parcel"),
        lambda b, link: link.set(f"{n.XLINK}href", "https://x/?QUERIE_ID=GetParcel"),
        lambda b, link: link.attrib.pop(f"{n.XLINK}href"),
        lambda b, link: ET.SubElement(b, "reference").__setattr__("text", "n/a"),
    ],
)
def test_unreadable_parcel_link_refuses_rather_than_drops(edit):
    with pytest.raises(c.CadastreError) as refused:
        n.parse_buildings(_first_building_with(edit), 25829)
    assert refused.value.state == c.MALFORMED


def test_nil_parcel_link_is_an_omitted_link():
    def nil(_building, link):
        link.attrib.pop(f"{n.XLINK}href")
        link.set(n.XSI_NIL, "true")

    parsed = n.parse_buildings(_first_building_with(nil), 25829)
    assert parsed[0]["parcels"] == set()
    assert all(b["parcels"] for b in parsed[1:])


def _rc(reference):
    return {"rc": {"pc1": reference[:7], "pc2": reference[7:]}}


def _address_answer(monkeypatch, result):
    monkeypatch.setattr(
        locate, "_fetch_json", lambda *_: {"consulta_dnplocResult": result}
    )
    return locate.parcel_for_address(
        "P", "M", "CL", "REAL", 8, require_unique_parcel=True
    )


@pytest.mark.parametrize(
    "result",
    [
        {"bico": {"bi": [_rc("4463719NH1946S"), None]}},
        {"bico": {"bi": [_rc(REF), "4463719NH1946S"]}},
        {"bico": {"bi": [_rc(REF), 7]}},
        {"bico": {"bi": [_rc(REF), {"rc": {"pc1": "4463719"}}]}},
        {"bico": {"bi": []}},
        {"bico": {}},
        {"bico": [_rc(REF)]},
        {"lrcdnp": {"rcdnp": [_rc(REF), None]}},
        {"lrcdnp": {"rcdnp": None}},
        {"lrcdnp": [_rc(REF)]},
        {"bico": {"bi": _rc(REF)}, "lrcdnp": {"rcdnp": _rc(REF)}},
        {"bi": _rc(REF)},
    ],
)
def test_address_answer_of_unexpected_form_is_refused(monkeypatch, result):
    assert _address_answer(monkeypatch, result)["status"] == c.MALFORMED


@pytest.mark.parametrize(
    "result",
    [
        {"bico": {"bi": _rc(REF)}},
        {"lrcdnp": {"rcdnp": [_rc(REF), _rc(REF)]}},
        {"lrcdnp": {"rcdnp": _rc(REF)}},
    ],
)
def test_address_naming_one_parcel_is_read(monkeypatch, result):
    answer = _address_answer(monkeypatch, result)
    assert (answer["status"], answer["reference"]) == (c.OK, REF)


@pytest.mark.parametrize("cached", [None, "junk", {"status": "ok"}])
def test_measure_uses_shared_transport_full_buffer_and_single_attempt(
    monkeypatch, cached
):
    p = prop()
    monkeypatch.setattr(
        n,
        "_metric_parcel",
        lambda _: (MultiPolygon([box(1000, 2000, 1010, 2020)]), 25829),
    )
    monkeypatch.setattr(c, "_cache_get", lambda _: cached)
    monkeypatch.setattr(c, "_cache_set", lambda *_: None)
    calls = []
    monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
    monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)

    def transport(url, **kwargs):
        # A box without its CRS is read in the service's default CRS.
        assert kwargs["params"]["bbox"].endswith(",urn:ogc:def:crs:EPSG::25829")
        calls.append((url, kwargs, c.CATASTRO_GATE._interval()))
        return SimpleNamespace(
            status_code=200,
            text='<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" numberMatched="0" numberReturned="0"/>',
        )

    monkeypatch.setattr(c.requests, "get", transport)
    result = n.NeighboursService().measure(p)
    assert result["status"] == "ok" and result["buildings_within_100m"] == 0
    assert len(calls) == 1
    assert (
        calls[0][1]["params"]["bbox"]
        == "900.0,1900.0,1110.0,2120.0,urn:ogc:def:crs:EPSG::25829"
    )
    assert calls[0][2] == 12
    assert c.CATASTRO_GATE._interval() == 1


@pytest.mark.parametrize(
    "status, body",
    [
        (500, ""),
        (200, '<FeatureCollection xmlns="http://www.opengis.net/gml/3.2"/>'),
        (200, (DATA / "969_buildings.gml").read_text()),  # far from box(0..10)
    ],
)
def test_refusal_never_retries_or_becomes_zero(monkeypatch, status, body):
    monkeypatch.setattr(n, "_metric_parcel", lambda _: (box(0, 0, 10, 10), 25829))
    monkeypatch.setattr(c, "_cache_get", lambda _: None)
    monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
    monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)
    monkeypatch.setattr(c, "_cache_set", Mock(side_effect=AssertionError))
    transport = Mock(return_value=SimpleNamespace(status_code=status, text=body))
    monkeypatch.setattr(c.requests, "get", transport)
    result = n.NeighboursService().measure(prop())
    assert result["status"] == "unavailable" and "buildings_within_100m" not in result
    assert transport.call_count == 1


def test_answer_reaching_past_the_box_is_measured(monkeypatch):
    # One fixture building lies wholly outside the box, as Catastro returns.
    gml = c._parse_gml((DATA / "969_parcel.gml").read_text(), 25829)
    parcel = n._shape(gml["polygons"])
    monkeypatch.setattr(n, "_metric_parcel", lambda _: (parcel, 25829))
    monkeypatch.setattr(c, "_cache_get", lambda _: None)
    monkeypatch.setattr(c, "_cache_set", lambda *_: None)
    monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
    monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)
    text = (DATA / "969_buildings.gml").read_text()
    answer = SimpleNamespace(status_code=200, text=text)
    monkeypatch.setattr(c.requests, "get", Mock(return_value=answer))
    result = n.NeighboursService().measure(prop())
    assert (result["status"], result["buildings_within_100m"]) == ("ok", 7)


EMPTY_COLLECTION = (
    '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0"'
    ' numberMatched="0" numberReturned="0"/>'
)


def _no_cache_no_gate(monkeypatch):
    monkeypatch.setattr(c, "_cache_get", lambda _: None)
    monkeypatch.setattr(c, "_cache_set", Mock(side_effect=AssertionError))
    monkeypatch.setattr(c.CATASTRO_GATE, "wait", lambda: 0)
    monkeypatch.setattr(c.CATASTRO_GATE, "mark", lambda: None)


@pytest.mark.parametrize(
    "row",
    [
        # Each path reaches Catastro through a different client: the parcel
        # outline (cadastre_service), the coordinate lookup and the address
        # indexes (cadastre_locate).
        dict(),
        dict(cadastral_reference=None),
        dict(
            cadastral_reference=None,
            location_accuracy="approximate",
            enrichment={"geocoding": {"query": "Rúa Maior 5, Miño"}},
        ),
    ],
    ids=["parcel_outline", "coordinate_lookup", "address_index"],
)
def test_a_spent_budget_starts_no_catastro_request(monkeypatch, row):
    _no_cache_no_gate(monkeypatch)
    monkeypatch.setattr(n, "_row_province", lambda _: "15")
    transport = Mock(side_effect=AssertionError("a request was started"))
    monkeypatch.setattr(c.requests, "get", transport)
    with h.lookup_budget(0):
        result = n.NeighboursService().measure(prop(**row))
    assert transport.call_count == 0
    assert result["status"] == "unavailable"
    assert result["reason"] == n.BUDGET_EXHAUSTED and result["detail"]
    assert "buildings_within_100m" not in result


def test_a_source_refusal_inside_the_budget_keeps_its_own_reason(monkeypatch):
    _no_cache_no_gate(monkeypatch)
    monkeypatch.setattr(n, "_metric_parcel", lambda _: (box(0, 0, 10, 10), 25829))
    answer = SimpleNamespace(status_code=500, text="")
    monkeypatch.setattr(c.requests, "get", Mock(return_value=answer))
    with h.lookup_budget(300):
        result = n.NeighboursService().measure(prop())
    assert (result["status"], result["reason"]) == ("unavailable", "HTTP 500")


@pytest.mark.parametrize("left, timeout", [(5.0, 5.0), (300.0, c.REQUEST_TIMEOUT_S)])
def test_a_request_gets_no_more_than_the_budget_left(monkeypatch, left, timeout):
    # A frozen clock: whatever the budget has left is exactly the timeout.
    monkeypatch.setattr(
        h, "time", SimpleNamespace(monotonic=lambda: 1000.0, sleep=lambda _: None)
    )
    _no_cache_no_gate(monkeypatch)
    monkeypatch.setattr(c, "_cache_set", lambda *_: None)
    monkeypatch.setattr(
        n, "_metric_parcel", lambda _: (box(1000, 2000, 1010, 2020), 25829)
    )
    answer = SimpleNamespace(status_code=200, text=EMPTY_COLLECTION)
    transport = Mock(return_value=answer)
    monkeypatch.setattr(c.requests, "get", transport)
    with h.lookup_budget(left):
        result = n.NeighboursService().measure(prop())
    assert result["status"] == "ok"
    assert transport.call_args.kwargs["timeout"] == timeout


def test_subject_stable_for_sql_numeric_roundtrip_and_stale_on_change():
    p = prop()
    old = n.subject(p)
    p.location_lat = Decimal("43.5000000")
    p.location_lon = Decimal("-8.5000000")
    assert n.subject(p) == old
    p.enrichment["neighbours"] = payload(p)
    assert n.read_verdict(p)
    p.location_accuracy = "approximate"
    assert n.read_verdict(p) == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("buildings_within_50m", True),
        ("nearest_building_m", 5),
        ("unknown_use_buildings_within_100m", 1),
        ("epsg", 4326),
        ("source", "photo"),
        ("reference", 123),
        ("epsg", 25829.0),
        ("measured_at", 1),
    ],
)
def test_malformed_stored_measurement_never_supports_descriptor(field, value):
    p = prop()
    p.enrichment["neighbours"] = payload(p, **{field: value})
    assert n.read_verdict(p) == {}


def test_unknown_use_is_not_agricultural_absence():
    p = prop()
    p.enrichment["neighbours"] = payload(
        p,
        buildings_within_100m=1,
        nearest_building_m=80,
        unknown_use_buildings_within_100m=1,
    )
    row = d.build_descriptor(p)["aspects"]["agricultural_context"][0]
    assert row["status"] == "unknown"


def test_only_measured_neighbours_change_descriptor_input():
    p = prop()
    before = d.input_fingerprint(p)
    p.enrichment["neighbours"] = {"status": "unavailable"}
    assert d.input_fingerprint(p) == before
    p.enrichment["neighbours"] = payload(p)
    assert d.input_fingerprint(p) != before


# Stored JSON a reader must refuse, never raise on (#663: 10**400 overflowed).
JUNK = {
    "10**400": 10**400,
    "-1": -1,
    "True": True,
    "str": "3",
    "nan": float("nan"),
    "inf": float("inf"),
    "json 1e999": json.loads("1e999"),
    "Decimal NaN": Decimal("NaN"),
    "None": None,
    "list": [1],
}


@pytest.mark.parametrize("value", list(JUNK.values()), ids=list(JUNK))
@pytest.mark.parametrize(
    "field",
    [
        "buildings_within_50m",
        "buildings_within_100m",
        "agricultural_buildings_within_100m",
        "industrial_buildings_within_100m",
        "unknown_use_buildings_within_100m",
        "nearest_building_m",
        "epsg",
    ],
)
def test_stored_number_of_wrong_kind_or_range_is_not_a_measurement(field, value):
    p = prop()
    near = dict(buildings_within_50m=1, buildings_within_100m=1, nearest_building_m=10)
    p.enrichment["neighbours"] = payload(p, **near)
    assert n.read_verdict(p)
    p.enrichment["neighbours"] = payload(p, **{**near, field: value})
    assert n.read_verdict(p) == {}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_writer_refreshes_under_lock_and_preserves_concurrent_sibling(app, monkeypatch):
    p = Property(
        source_email_id="neighbours-lock",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    db.session.add(p)
    db.session.commit()
    service = n.NeighboursService()

    def measure(row):
        result = payload(row)
        db.session.execute(
            text("UPDATE properties SET enrichment=:e WHERE id=:i"),
            {"e": json.dumps({"sea": {"status": "ok"}}), "i": row.id},
        )
        return result

    monkeypatch.setattr(service, "measure", measure)
    refresh = db.session.refresh
    seen = []

    def track(row, **kwargs):
        seen.append(kwargs)
        return refresh(row, **kwargs)

    monkeypatch.setattr(db.session, "refresh", track)
    service.enrich(p, commit=True)
    assert p.enrichment["sea"] == {"status": "ok"}
    assert p.enrichment["neighbours"]["status"] == "ok"
    assert seen == [{"with_for_update": True}]


def test_writer_drops_measurement_when_origin_changes_under_lock(app, monkeypatch):
    p = Property(
        source_email_id="neighbours-move",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    db.session.add(p)
    db.session.commit()
    service = n.NeighboursService()

    def measure(row):
        result = payload(row)
        db.session.execute(
            text("UPDATE properties SET location_lat=43.6 WHERE id=:i"), {"i": row.id}
        )
        return result

    monkeypatch.setattr(service, "measure", measure)
    result = service.enrich(p, commit=True)
    assert (
        result["status"] == "unavailable"
        and result["reason"] == "subject_changed_during_lookup"
    )
    assert n.read_verdict(p) == {}


def test_dirty_session_refuses_before_network(app, monkeypatch):
    p = Property(source_email_id="neighbours-dirty", enrichment={})
    db.session.add(p)
    db.session.commit()
    p.title = "pending"
    service = n.NeighboursService()
    mock = Mock()
    monkeypatch.setattr(service, "measure", mock)
    with pytest.raises(EnrichmentWriteContractError):
        service.enrich(p, commit=True)
    mock.assert_not_called()
    db.session.rollback()


def test_advisory_runs_neighbours_with_own_commit_and_free_ingestion_does_not(
    monkeypatch,
):
    service = PropertyEnrichmentService(neighbours_service=Mock())
    monkeypatch.setattr(
        "services.property_enrichment_service.advertiser.enrich", lambda *_a, **_k: None
    )
    monkeypatch.setattr(service, "enrich_free_sources", lambda *_a, **_k: None)
    service._advisory_pass(prop(), use_ai=False, with_pool=False)
    service.neighbours_service.enrich.assert_called_once()
    assert service.neighbours_service.enrich.call_args.kwargs == {"commit": True}


@pytest.mark.parametrize(
    "raw", ["", "0", "1,-2", ",".join(str(i) for i in range(1, 22))]
)
def test_cli_refuses_unscoped_or_bulk_ids(raw):
    with pytest.raises(Exception):
        cli.parse_ids(raw)


def test_cli_dry_run_writes_plan_without_api_calls(app, monkeypatch, tmp_path):
    p = Property(source_email_id="neighbours-cli", enrichment={})
    db.session.add(p)
    db.session.commit()
    monkeypatch.setattr(cli, "create_app", lambda: app)
    network = Mock(side_effect=AssertionError("dry run network"))
    monkeypatch.setattr(n.NeighboursService, "enrich", network)
    plan = tmp_path / "plan.json"
    cli.main(["--ids", str(p.id), "--dry-run", "--plan", str(plan)])
    assert json.loads(plan.read_text())["rows"][0]["id"] == p.id
    assert p.enrichment == {}
    network.assert_not_called()


def test_photo_neighbours_remain_claims_with_incomparable_vocabulary():
    from services.taste_recommendation import _value_relationship

    p = prop()
    visual = dict(
        schema_version=1,
        property_fingerprint=d.input_fingerprint(p),
        input_fingerprint="a" * 64,
        visual_observations=[
            dict(
                aspect_id=aspect,
                value=value,
                status="supported",
                evidence=dict(
                    source_kind="photo",
                    source_id="photo:0",
                    image_sha256="b" * 64,
                    image_index=0,
                ),
                confidence=0.9,
                limitation="Facade only; off-frame buildings are unknown.",
            )
            for aspect, value in [
                ("neighbor_privacy", "neighbours_visible"),
                ("nearby_buildings", "few_visible"),
            ]
        ],
    )
    descriptor = d.build_descriptor(p, visual_input=visual)
    assert descriptor["aspects"]["neighbor_privacy"][0]["status"] == "claimed"
    assert descriptor["aspects"]["nearby_buildings"][0]["status"] == "claimed"
    assert (
        _value_relationship("neighbours_visible", "low", "neighbor_privacy")
        == "incomparable"
    )
    assert (
        _value_relationship("7", "several_visible", "nearby_buildings")
        == "incomparable"
    )


def test_refusal_preserves_only_same_subject_success(app, monkeypatch):
    p = Property(
        source_email_id="neighbours-refusal",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    db.session.add(p)
    db.session.commit()
    p.enrichment = {"neighbours": payload(p), "sea": {"status": "ok"}}
    db.session.commit()
    service = n.NeighboursService()
    monkeypatch.setattr(
        service,
        "measure",
        lambda row: dict(
            status="unavailable", subject=n.subject(row), reason="refused"
        ),
    )
    result = service.enrich(p, commit=True)
    assert result["status"] == "ok"
    assert p.enrichment["sea"] == {"status": "ok"}
    p.location_lat = 43.6
    db.session.commit()
    result = service.enrich(p, commit=True)
    assert result["status"] == "unavailable"
    assert n.read_verdict(p) == {}


def test_gate_policy_is_context_local():
    from contextvars import Context

    with c.shortlist_requests():
        assert c.CATASTRO_GATE._interval() == 12
        assert Context().run(c.CATASTRO_GATE._interval) == 1
    assert c.CATASTRO_GATE._interval() == 1


def test_metric_parcel_uses_own_reference_point_and_refuses_wrong_reference(
    monkeypatch,
):
    mapped = dict(status="ok", reference_point=(43.5, -8.5))
    measured = c._parse_gml((DATA / "969_parcel.gml").read_text(), 25829)
    measured["status"] = "ok"
    calls = []

    def source(fetch, *, cache_key):
        calls.append(cache_key)
        return mapped if len(calls) % 2 else measured

    monkeypatch.setattr(c, "_source", source)
    parcel, epsg = n._metric_parcel(REF)
    assert epsg == 25829 and parcel.area == pytest.approx(1616.24565)
    for key, bad in [("reference", "15044A01901483"), ("reference", 123)]:
        measured[key] = bad
        with pytest.raises(c.CadastreError):
            n._metric_parcel(REF)
    measured.update(reference=REF, declared_area_m2="x")
    with pytest.raises(c.CadastreError, match="unreadable"):
        n._metric_parcel(REF)
    mapped["reference_point"] = "43,-8"
    with pytest.raises(c.CadastreError):
        n._metric_parcel(REF)


PARCEL_JUNK = [
    pytest.param(where, value, id=f"{where}-{name}")
    for where, extra in (("lat", 91), ("lon", 181), ("declared", None))
    for name, value in [
        *((k, v) for k, v in JUNK.items() if k not in ("-1", "None")),
        ("out of range", extra),
    ]
    if value is not None or where != "declared"  # no declared area is allowed
]


@pytest.mark.parametrize("where,value", PARCEL_JUNK)
def test_parcel_number_of_wrong_kind_or_range_is_refused(monkeypatch, where, value):
    point = {"lat": 43.5, "lon": -8.5, where: value}
    mapped = dict(status="ok", reference_point=(point["lat"], point["lon"]))
    measured = c._parse_gml((DATA / "969_parcel.gml").read_text(), 25829)
    measured["status"] = "ok"
    if where == "declared":
        measured["declared_area_m2"] = value
    answers = iter([mapped, measured])
    monkeypatch.setattr(c, "_source", lambda fetch, *, cache_key: next(answers))
    with pytest.raises(c.CadastreError):
        n._metric_parcel(REF)


def test_building_cap_and_open_ring_are_refused(monkeypatch):
    monkeypatch.setattr(n, "MAX_FEATURES", 10)
    with pytest.raises(c.CadastreError):
        n.parse_buildings((DATA / "969_buildings.gml").read_text(), 25829)
    ring = ET.fromstring(
        '<root xmlns:g="http://www.opengis.net/gml/3.2"><g:Polygon><g:exterior><g:LinearRing><g:posList>0 0 10 0 10 10 0 10</g:posList></g:LinearRing></g:exterior></g:Polygon></root>'
    )
    with pytest.raises(c.CadastreError):
        c.surface_polygons(ring)


def test_cli_apply_requires_plan_and_refuses_changed_baseline(
    app, monkeypatch, tmp_path
):
    with pytest.raises(SystemExit):
        cli.main(["--ids", "1", "--apply"])
    p = Property(source_email_id="neighbours-plan", enrichment={})
    db.session.add(p)
    db.session.commit()
    monkeypatch.setattr(cli, "create_app", lambda: app)
    plan = tmp_path / "plan.json"
    cli.main(["--ids", str(p.id), "--plan", str(plan)])
    p.location_accuracy = "approximate"
    db.session.commit()
    network = Mock(side_effect=AssertionError("stale plan network"))
    monkeypatch.setattr(n.NeighboursService, "enrich", network)
    with pytest.raises(SystemExit):
        cli.main(["--ids", str(p.id), "--apply", "--plan", str(plan)])
    network.assert_not_called()


def test_shortlist_plan_compare_is_under_lock_after_network(app, monkeypatch):
    p = Property(
        source_email_id="neighbours-plan-race",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    db.session.add(p)
    db.session.commit()
    service = n.NeighboursService()
    concurrent = payload(
        p, buildings_within_50m=1, buildings_within_100m=1, nearest_building_m=10
    )

    def measure(row):
        result = payload(row)
        db.session.execute(
            text("UPDATE properties SET enrichment=:e WHERE id=:i"),
            {"e": json.dumps({"neighbours": concurrent}), "i": row.id},
        )
        # Commit emulates the other writer independently of the writer's rollback.
        db.session.commit()
        return result

    monkeypatch.setattr(service, "measure", measure)
    with pytest.raises(ValueError, match="plan changed during lookup"):
        service.enrich(p, commit=True, expected_previous=None)
    assert p.enrichment["neighbours"] == concurrent


def test_planned_subject_is_compared_under_lock_after_network(app, monkeypatch):
    """The subject moves after the pre-request check: the lookup measures the
    moved row and agrees with it, and the previous block (None) still matches.
    Only the planned subject, asked under the lock, refuses the write."""
    p = Property(
        source_email_id="neighbours-plan-subject-race",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    db.session.add(p)
    db.session.commit()
    planned = n.subject(p)
    service = n.NeighboursService()

    def measure(row):
        db.session.execute(
            text("UPDATE properties SET cadastral_reference=:r WHERE id=:i"),
            {"r": "9999999NH9999S", "i": row.id},
        )
        db.session.commit()
        return payload(row)  # measured on the moved row, as the real lookup would

    monkeypatch.setattr(service, "measure", measure)
    with pytest.raises(ValueError, match="plan changed during lookup"):
        service.enrich(p, commit=True, expected_previous=None, expected_subject=planned)
    db.session.expire_all()
    assert "neighbours" not in (db.session.get(Property, p.id).enrichment or {})


def test_cli_apply_refuses_a_subject_changed_between_plan_rows(
    app, monkeypatch, tmp_path
):
    """The preflight compares the whole plan once; a later row can still move
    while an earlier one is measured. Its subject must be asked again, before
    any request and under the row lock, not merely its previous block (None
    for a row never measured matches whatever was moved)."""
    rows = [
        Property(
            source_email_id=f"neighbours-plan-rows-{i}",
            cadastral_reference=REF,
            location_lat=43.5,
            location_lon=-8.5,
            location_accuracy="precise",
            enrichment={},
        )
        for i in range(2)
    ]
    db.session.add_all(rows)
    db.session.commit()
    first, second = (row.id for row in rows)
    monkeypatch.setattr(cli, "create_app", lambda: app)
    plan = tmp_path / "plan.json"
    cli.main(["--ids", f"{first},{second}", "--plan", str(plan)])
    measured = []

    def measure(self, row):
        measured.append(row.id)
        if row.id == first:
            # Another transaction moves the second row's parcel after the
            # preflight passed and while the first row is being measured.
            db.session.execute(
                text("UPDATE properties SET cadastral_reference=:r WHERE id=:i"),
                {"r": "9999999NH9999S", "i": second},
            )
            db.session.commit()
        return payload(row)

    monkeypatch.setattr(n.NeighboursService, "measure", measure)
    with pytest.raises(ValueError, match="plan changed during lookup"):
        cli.main(["--ids", f"{first},{second}", "--apply", "--plan", str(plan)])
    assert measured == [first]
    db.session.expire_all()
    assert db.session.get(Property, first).enrichment["neighbours"]["status"] == "ok"
    assert "neighbours" not in (db.session.get(Property, second).enrichment or {})


def test_mixed_known_and_unknown_uses_cannot_support_agricultural_absence():
    result = n.measure_buildings(
        box(0, 0, 10, 10),
        REF,
        [
            dict(
                parcels={"other"},
                shape=box(20, 0, 22, 2),
                uses={"1_residential", "unknown_use"},
            )
        ],
    )
    assert result["unknown_use_buildings_within_100m"] == 1
    assert result["agricultural_buildings_within_100m"] == 0


def test_unknown_collection_container_does_not_mean_no_buildings():
    with pytest.raises(c.CadastreError):
        n.parse_buildings(
            '<FeatureCollection xmlns="http://www.opengis.net/gml/3.2"><featureMembers/></FeatureCollection>',
            25829,
        )


def test_parcel_crs_code_must_match_exactly():
    wrong = (DATA / "969_parcel.gml").read_text().replace('/25829"', '/258290"')
    with pytest.raises(c.CadastreError):
        c._parse_gml(wrong, 25829)


@pytest.mark.parametrize("unsupported", ["Rectangle", "TriangulatedSurface"])
def test_unsupported_surface_part_refuses_the_collection(unsupported):
    root = ET.fromstring((DATA / "969_buildings.gml").read_text())
    surface = root.find(f".//{n.GML}Surface")
    assert surface is not None
    parent = (
        root.find(f".//{n.GML}patches") if unsupported == "Rectangle" else root[0][0]
    )
    assert parent is not None
    ET.SubElement(parent, f"{n.GML}{unsupported}")
    with pytest.raises(c.CadastreError, match="unsupported or empty surface part"):
        n.parse_buildings(ET.tostring(root, encoding="unicode"), 25829)


def test_empty_use_does_not_disappear_beside_a_known_use():
    root = ET.fromstring((DATA / "969_buildings.gml").read_text())
    building = root[0][0]
    use_tag = building.tag.rsplit("}", 1)[0] + "}currentUse"
    for use in building.findall(use_tag):
        building.remove(use)
    ET.SubElement(building, use_tag).text = "1_residential"
    ET.SubElement(
        building, use_tag, {"{http://www.w3.org/2001/XMLSchema-instance}nil": "true"}
    )
    parsed = n.parse_buildings(ET.tostring(root, encoding="unicode"), 25829)
    assert parsed[0]["uses"] == {"1_residential", ""}


def test_sixth_enrich_press_in_a_minute_is_refused(app, monkeypatch):
    """One press can now spend up to six Catastro requests on the neighbour walk.

    Behavioural, like the cadastre route's own test: a sixth press must not
    reach the handler at all. A missing row keeps the handler off the network.
    """
    from app import limiter

    monkeypatch.setattr(limiter, "enabled", True)
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", False)
    limiter.reset()
    client = app.test_client()
    statuses = [
        client.post("/api/property/999999/enrich", json={}).status_code
        for _ in range(6)
    ]
    limiter.reset()
    assert statuses[:5] == [404] * 5, statuses
    assert statuses[5] == 429, statuses


@pytest.mark.parametrize("measured_before", [True, False])
def test_a_clock_refusal_never_replaces_a_measurement(
    app, monkeypatch, measured_before
):
    p = Property(
        source_email_id="neighbours-clock",
        cadastral_reference=REF,
        location_lat=43.5,
        location_lon=-8.5,
        location_accuracy="precise",
        enrichment={},
    )
    db.session.add(p)
    db.session.commit()
    previous = payload(p) if measured_before else None
    if previous:
        p.enrichment = {"neighbours": previous}
        db.session.commit()
    _no_cache_no_gate(monkeypatch)
    transport = Mock(side_effect=AssertionError("a request was started"))
    monkeypatch.setattr(c.requests, "get", transport)
    with h.lookup_budget(0):
        n.NeighboursService().enrich(p, commit=True)
    db.session.expire_all()
    stored = db.session.get(Property, p.id).enrichment["neighbours"]
    if measured_before:
        assert stored == previous
    else:
        assert (stored["status"], stored["reason"]) == (
            "unavailable",
            n.BUDGET_EXHAUSTED,
        )
    assert transport.call_count == 0
