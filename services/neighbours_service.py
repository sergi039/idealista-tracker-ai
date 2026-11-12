"""Shortlist-only parcel-to-building measurements from Catastro (#570).

No ingestion/pool sweep: an Enrich press or explicitly scoped --ids runs this
reader. The transport, retries, cache and rate reservations belong to the
existing cadastre client. Geometry is measured in the parcel's own metric CRS.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit

from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from services import cadastre_service as cadastre

GML = "{http://www.opengis.net/gml/3.2}"
WFS = "{http://www.opengis.net/wfs/2.0}"
XLINK = "{http://www.w3.org/1999/xlink}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
MAX_FEATURES = 5000


def _parcel_key(raw):
    reference = cadastre.normalize_reference(raw)
    if not reference:
        raise cadastre.CadastreError(cadastre.MALFORMED, "unreadable parcel reference")
    return cadastre.parcel_reference(reference)


def _linked_parcels(element):
    """Every parcel a link names. A nil link is an omitted one; a link that
    names no readable parcel is unreadable, never "no parcel"."""
    href = element.get(XLINK + "href")
    if href is None and element.get(XSI_NIL) == "true" and not len(element):
        return set()
    query = parse_qs(urlsplit(href or "").query)
    values = [v for k, vs in query.items() if k.lower() == "refcat" for v in vs]
    if not values:
        raise cadastre.CadastreError(cadastre.MALFORMED, "parcel link names no parcel")
    return {_parcel_key(value) for value in values}


def _patch(rings):
    if any(len(ring) < 4 or tuple(ring[0]) != tuple(ring[-1]) for ring in rings):
        raise ValueError("open polygon ring")
    patch = Polygon(rings[0], rings[1:])
    if patch.is_empty or not patch.is_valid or not patch.area > 0:
        raise ValueError("invalid polygon patch")
    return patch


def _shape(polygons):
    """One footprint from one feature's patches, every patch checked alone.

    GML tiles a footprint with patches that share edges; as a MultiPolygon
    they are invalid although each is sound, so the footprint is their union.
    Called once per feature, so neighbouring buildings never merge.
    """
    try:
        patches = [_patch(rings) for rings in polygons]
        if not patches:
            raise ValueError("no polygon patch")
        result = unary_union(patches)
        if isinstance(result, Polygon):
            result = MultiPolygon([result])
        if (
            not isinstance(result, MultiPolygon)
            or result.is_empty
            or not result.is_valid
            or not math.isfinite(result.area)
        ):
            raise ValueError("invalid surface")
        return result
    except (ValueError, TypeError, IndexError, ShapelyError) as exc:
        raise cadastre.CadastreError(
            cadastre.MALFORMED, "invalid polygon surface"
        ) from exc


def _xml(text):
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise cadastre.CadastreError(cadastre.MALFORMED, "not XML") from exc


def _check_crs(element, epsg):
    names = [e.get("srsName") for e in element.iter() if e.get("srsName")]
    if not names or any(not re.search(rf"(?:/|:)({epsg})$", n) for n in names):
        raise cadastre.CadastreError(cadastre.MALFORMED, "unexpected metric CRS")


def parse_buildings(text: str, epsg: int) -> list[dict]:
    """Reject partial/exception/HTML replies rather than invent empty counts.

    Catastro's native gml:FeatureCollection omits WFS counts. Its documented
    limit is 5000; reaching that cap is incomplete. Where WFS counts are
    provided they must both be exact and agree with the parsed collection.
    """
    root = _xml(text)
    if root.tag not in {GML + "FeatureCollection", WFS + "FeatureCollection"}:
        raise cadastre.CadastreError(cadastre.MALFORMED, "not a FeatureCollection")
    if root.get("next") or any(e.tag.endswith("Exception") for e in root.iter()):
        raise cadastre.CadastreError(
            cadastre.MALFORMED, "partial or refused collection"
        )
    members = root.findall(GML + "featureMember") + root.findall(WFS + "member")
    if any(
        child.tag not in {GML + "featureMember", WFS + "member", GML + "boundedBy"}
        for child in root
    ):
        raise cadastre.CadastreError(
            cadastre.MALFORMED, "unsupported collection content"
        )
    if len(members) >= MAX_FEATURES:
        raise cadastre.CadastreError(cadastre.MALFORMED, "building feature cap reached")
    for name in ("numberMatched", "numberReturned"):
        value = root.get(name)
        if value is not None and (not value.isdigit() or int(value) != len(members)):
            raise cadastre.CadastreError(
                cadastre.MALFORMED, "partial building collection"
            )
    if root.tag == WFS + "FeatureCollection" and any(
        root.get(n) is None for n in ("numberMatched", "numberReturned")
    ):
        raise cadastre.CadastreError(cadastre.MALFORMED, "missing WFS coverage counts")
    buildings = []
    seen = set()
    for member in members:
        elements = list(member)
        if len(elements) != 1 or elements[0].tag.rsplit("}", 1)[-1] != "Building":
            raise cadastre.CadastreError(cadastre.MALFORMED, "not a building feature")
        building = elements[0]
        identifier = building.get(GML + "id")
        if not identifier or identifier in seen:
            raise cadastre.CadastreError(
                cadastre.MALFORMED, "missing or duplicate building id"
            )
        seen.add(identifier)
        _check_crs(building, epsg)
        refs = set()
        uses = set()
        for element in building.iter():
            local = element.tag.rsplit("}", 1)[-1]
            if local == "cadastralParcels":
                refs.update(_linked_parcels(element))
            if local == "reference":
                refs.add(_parcel_key(element.text))
            if local == "currentUse":
                raw = (element.text or element.get(XLINK + "href") or "").rsplit(
                    "/", 1
                )[-1]
                uses.add(raw.strip())
        buildings.append(
            {
                "id": identifier,
                "parcels": refs,
                "uses": uses,
                "shape": _shape(cadastre.surface_polygons(building)),
            }
        )
    return buildings
