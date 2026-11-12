"""Catastro building collections: what a complete answer is, and what is not."""

from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from shapely.geometry import box

from tests import setup_test_environment

setup_test_environment()
from services import (  # noqa: E402
    cadastre_service as c,
    cadastre_locate as locate,
    neighbours_service as n,
)

# Catastro's live answers for the owner's two references (rows 969 and 1282),
# trimmed to the buildings within 100 m, the parcel's own building and two or
# three beyond the radius. Trimming removes only features farther than the
# kept ones, so every measured count and distance is the live answer's.
DATA = Path(__file__).parent / "data" / "neighbours"
REF = "4463719NH1946S"


@pytest.mark.parametrize("row,reference,features", [(969, REF, 10)])
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
        '<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0"/>',
        '<FeatureCollection xmlns="http://www.opengis.net/gml/3.2" next="page2"/>',
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


def test_gate_policy_is_context_local():
    from contextvars import Context

    with c.shortlist_requests():
        assert c.CATASTRO_GATE._interval() == 12
        assert Context().run(c.CATASTRO_GATE._interval) == 1
    assert c.CATASTRO_GATE._interval() == 1


def test_building_cap_and_open_ring_are_refused(monkeypatch):
    monkeypatch.setattr(n, "MAX_FEATURES", 10)
    with pytest.raises(c.CadastreError):
        n.parse_buildings((DATA / "969_buildings.gml").read_text(), 25829)
    ring = ET.fromstring(
        '<root xmlns:g="http://www.opengis.net/gml/3.2"><g:Polygon><g:exterior><g:LinearRing><g:posList>0 0 10 0 10 10 0 10</g:posList></g:LinearRing></g:exterior></g:Polygon></root>'
    )
    with pytest.raises(c.CadastreError):
        c.surface_polygons(ring)


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
