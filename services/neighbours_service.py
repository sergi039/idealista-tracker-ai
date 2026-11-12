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

from shapely.geometry import MultiPolygon, Polygon

from services import cadastre_service as cadastre

GML = "{http://www.opengis.net/gml/3.2}"
WFS = "{http://www.opengis.net/wfs/2.0}"
XLINK = "{http://www.w3.org/1999/xlink}"
MAX_FEATURES = 5000


def _shape(polygons):
    try:
        shapes = [Polygon(rings[0], rings[1:]) for rings in polygons]
        result = MultiPolygon(shapes)
        if result.is_empty or not result.is_valid or not math.isfinite(result.area):
            raise ValueError("invalid surface")
        return result
    except (ValueError, TypeError, IndexError) as exc:
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
                href = element.get(XLINK + "href", "")
                for key, values in parse_qs(urlsplit(href).query).items():
                    if key.lower() == "refcat":
                        refs.update(
                            cadastre.parcel_reference(cadastre.normalize_reference(r))
                            for r in values
                            if cadastre.normalize_reference(r)
                        )
            if local == "reference" and cadastre.normalize_reference(element.text):
                refs.add(
                    cadastre.parcel_reference(
                        cadastre.normalize_reference(element.text)
                    )
                )
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
