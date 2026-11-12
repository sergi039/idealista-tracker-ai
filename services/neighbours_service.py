"""Shortlist-only parcel-to-building measurements from Catastro (#570).

No ingestion/pool sweep: an Enrich press or explicitly scoped --ids runs this
reader. The transport, retries, cache and rate reservations belong to the
existing cadastre client. Geometry is measured in the parcel's own metric CRS.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from shapely.errors import ShapelyError
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from services import cadastre_locate as locate, cadastre_service as cadastre
from services.address_agreement import query_house_numbers
from services.coordinate_quality import is_precise
from services.enrichment_write import check_writable, locked_write
from services.property_location_service import _row_province
from utils.http import lookup_budget

BUILDINGS_URL = "https://ovc.catastro.meh.es/INSPIRE/wfsBU.aspx"
GML = "{http://www.opengis.net/gml/3.2}"
WFS = "{http://www.opengis.net/wfs/2.0}"
XLINK = "{http://www.w3.org/1999/xlink}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
MAX_FEATURES = 5000
MAX_BBOX_AREA_M2 = 4_000_000
_UNSET = object()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) else None


def _real(value):
    """A finite number; a bool, a string or NaN is not one."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if ok and math.isfinite(value) else None


def subject(prop: Any) -> str:
    """Invalidate when the parcel, origin, accuracy or asked address changes."""
    enrichment = _dict(getattr(prop, "enrichment", None))
    payload = [
        cadastre.normalize_reference(getattr(prop, "cadastral_reference", None)),
        _dict(enrichment.get("cadastre")).get("reference"),
        _coordinate(getattr(prop, "location_lat", None)),
        _coordinate(getattr(prop, "location_lon", None)),
        getattr(prop, "location_accuracy", None),
        _dict(enrichment.get("geocoding")).get("query"),
        getattr(prop, "municipality", None),
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _coordinate(value):
    try:
        value = float(value)
        return f"{value:.7f}" if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


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
        if value is not None and (
            not re.fullmatch(r"[0-9]+", value) or int(value) != len(members)
        ):
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


def measure_buildings(parcel, reference: str, buildings: list[dict]) -> dict:
    """Distance between full polygons, never listing point or parcel centroid."""
    neighbours = []
    for building in buildings:
        if reference in building["parcels"]:
            continue
        # Some native features omit the parcel link. Geometry wholly on the
        # subject parcel is still the property's own building. A building
        # touching it from another parcel remains a zero-distance neighbour.
        if not building["parcels"] and parcel.covers(building["shape"]):
            continue
        distance = parcel.distance(building["shape"])
        if distance <= 100:
            neighbours.append((distance, building))
    uses = {"agricultural": 0, "industrial": 0, "unknown": 0}
    for _, building in neighbours:
        unknown = not building["uses"]
        agricultural = False
        industrial = False
        for use in building["uses"]:
            if use in {"2_agriculture", "agriculture", "agricultural"}:
                agricultural = True
            elif use in {"3_industrial", "industrial"}:
                industrial = True
            elif use in {
                "1_residential",
                "4_1_office",
                "4_2_retail",
                "4_3_publicServices",
                "4_4_ancillary",
                "residential",
            }:
                pass
            else:
                unknown = True
        uses["agricultural"] += agricultural
        uses["industrial"] += industrial
        uses["unknown"] += unknown
    return {
        "buildings_within_50m": sum(d <= 50 for d, _ in neighbours),
        "buildings_within_100m": len(neighbours),
        # Beyond 100m was not searched; null is never a measured far distance.
        "nearest_building_m": min((d for d, _ in neighbours), default=None),
        "agricultural_buildings_within_100m": uses["agricultural"],
        "industrial_buildings_within_100m": uses["industrial"],
        "unknown_use_buildings_within_100m": uses["unknown"],
    }


def _resolve_parcel(prop):
    stored = _dict(_dict(getattr(prop, "enrichment", None)).get("cadastre"))
    column = cadastre.normalize_reference(
        _text(getattr(prop, "cadastral_reference", None))
    )
    kept = cadastre.normalize_reference(_text(stored.get("reference")))
    if (
        column
        and kept
        and (cadastre.parcel_reference(column) != cadastre.parcel_reference(kept))
    ):
        return {"status": "unavailable", "reason": "conflicting_parcel_references"}
    if column or kept:
        return {"status": "ok", "reference": cadastre.parcel_reference(column or kept)}
    # A portal's 500m privacy circle is NEVER a coordinate lookup origin.
    if not is_precise(getattr(prop, "location_accuracy", None)):
        return _resolve_address(prop)
    answer = locate.parcels_at(
        getattr(prop, "location_lat", None), getattr(prop, "location_lon", None)
    )
    if answer.get("status") == cadastre.NOT_FOUND:
        return {"status": "no_parcel", "reason": "no_parcel_at_coordinate"}
    if answer.get("status") != cadastre.OK:
        return {"status": "unavailable", "reason": "parcel_lookup_failed"}
    parcels = answer["parcels"]
    on_point = {
        cadastre.normalize_reference(p.get("reference"))
        for p in parcels
        if p.get("distance_m") == 0
    }
    on_point.discard(None)
    if len(on_point) == 1:
        return {"status": "ok", "reference": cadastre.parcel_reference(on_point.pop())}
    return _resolve_address(prop)


def _resolve_address(prop):
    """An exact Catastro street and number; never the privacy-circle centre."""
    query = _text(
        _dict(_dict(getattr(prop, "enrichment", None)).get("geocoding")).get("query")
    )
    numbers = query_house_numbers(query)
    province_code = _row_province(prop)
    if len(numbers) != 1 or not province_code:
        return {"status": "no_parcel", "reason": "parcel_not_established"}
    number = next(iter(numbers))
    street = str(query).split(",")[0].strip()
    street = re.sub(rf"\s+{re.escape(number)}\s*$", "", street).strip()
    province = locate.PlaceIndex().province(province_code)
    if not province:
        return {"status": "unavailable", "reason": "province_index_unavailable"}
    municipality = getattr(prop, "municipality", "")
    index = locate.streets(province, municipality)
    if index.get("status") != "ok":
        return {"status": "unavailable", "reason": "street_index_unavailable"}
    matched = locate.match_street(index["streets"], street)
    if matched.get("status") != "ok":
        return {"status": "no_parcel", "reason": matched.get("status")}
    found = matched["street"]
    answer = locate.parcel_for_address(
        province,
        municipality,
        found["sigla"],
        found["name"],
        number,
        require_unique_parcel=True,
    )
    if answer.get("status") == "ok":
        return {
            "status": "ok",
            "reference": cadastre.parcel_reference(answer["reference"]),
        }
    return {
        "status": "no_parcel"
        if answer.get("status") == cadastre.NOT_FOUND
        else "unavailable",
        "reason": "address_not_placed",
    }


def _metric_parcel(reference):
    mapped = cadastre._source(
        lambda: cadastre._fetch_outline(reference, 4326),
        cache_key=cadastre._cache_key(reference, 4326, "outline"),
    )
    point = mapped.get("reference_point") if mapped.get("status") == "ok" else None
    point = [_real(n) for n in point] if isinstance(point, (list, tuple)) else []
    epsg = (
        cadastre.metric_epsg_for(*point)
        if len(point) == 2 and None not in point
        else None
    )
    if epsg is None:
        raise cadastre.CadastreError(
            cadastre.UNAVAILABLE, "no supported parcel metric CRS"
        )
    measured = cadastre._source(
        lambda: cadastre._fetch_outline(reference, epsg),
        cache_key=cadastre._cache_key(reference, epsg, "neighbours_outline_v1"),
    )
    if measured.get("status") != "ok":
        raise cadastre.CadastreError(cadastre.UNAVAILABLE, "parcel outline unavailable")
    returned = cadastre.normalize_reference(_text(measured.get("reference")))
    if not returned or cadastre.parcel_reference(returned) != reference:
        raise cadastre.CadastreError(cadastre.MALFORMED, "different parcel returned")
    parcel = _shape(measured.get("polygons", []))
    declared = measured.get("declared_area_m2")
    if declared is not None and _real(declared) is None:
        raise cadastre.CadastreError(cadastre.MALFORMED, "unreadable parcel area")
    if not cadastre.area_agrees(parcel.area, declared):
        raise cadastre.CadastreError(cadastre.MALFORMED, "parcel area mismatch")
    return parcel, epsg


class NeighboursService:
    def measure(self, prop):
        origin = subject(prop)
        result = {
            "subject": origin,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "source": "catastro_inspire_buildings",
        }
        try:
            with cadastre.shortlist_requests(), lookup_budget(90):
                resolved = _resolve_parcel(prop)
                result.update(resolved)
                if resolved["status"] != "ok":
                    return result
                reference = resolved["reference"]
                parcel, epsg = _metric_parcel(reference)
                left, bottom, right, top = parcel.bounds
                bbox = [left - 100, bottom - 100, right + 100, top + 100]
                if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > MAX_BBOX_AREA_M2:
                    raise cadastre.CadastreError(
                        cadastre.UNAVAILABLE, "parcel exceeds bounded building query"
                    )
                key = cadastre._cache_key(reference, epsg, "neighbours_buildings_v1")
                # A cached entry is used only if it reads as this measurement.
                cached = cadastre._cache_get(key)
                hit = {**result, **cached} if isinstance(cached, dict) else {}
                if (
                    _checked(hit, origin)
                    and hit["reference"] == reference
                    and hit["epsg"] == epsg
                ):
                    return hit
                response = cadastre._get(
                    BUILDINGS_URL,
                    {
                        "service": "wfs",
                        "version": "2.0.0",
                        "request": "GetFeature",
                        "typenames": "BU.BUILDING",
                        "srsname": f"EPSG::{epsg}",
                        "bbox": ",".join(str(n) for n in bbox),
                    },
                )
                if response.status_code != 200:
                    raise cadastre.CadastreError(
                        cadastre.REFUSED, f"HTTP {response.status_code}"
                    )
                buildings = parse_buildings(response.text, epsg)
                payload = {
                    "status": "ok",
                    "reference": reference,
                    "epsg": epsg,
                    "searched_m": 100,
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    **measure_buildings(parcel, reference, buildings),
                }
                cadastre._cache_set(key, payload)
                return {**result, **payload}
        except (cadastre.CadastreError, requests.RequestException) as exc:
            return {**result, "status": "unavailable", "reason": str(exc)}

    def enrich(self, prop, *, commit=True, expected_previous=_UNSET):
        locked = check_writable(prop, commit)
        fetched = self.measure(prop)
        with locked_write(prop, locked=locked, commit=commit):
            enrichment = dict(prop.enrichment or {})
            if expected_previous is not _UNSET and (
                enrichment.get("neighbours") != expected_previous
                or fetched.get("subject") != subject(prop)
            ):
                raise ValueError("shortlist plan changed during lookup; dry-run again")
            previous = _dict(enrichment.get("neighbours"))
            if fetched.get("subject") != subject(prop):
                fetched = {
                    "status": "unavailable",
                    "subject": subject(prop),
                    "reason": "subject_changed_during_lookup",
                }
            if (
                fetched.get("status") == "unavailable"
                and previous.get("status") == "ok"
                and previous.get("subject") == subject(prop)
            ):
                return previous
            enrichment["neighbours"] = fetched
            prop.enrichment = enrichment
        return fetched


def read_verdict(prop):
    block = _dict(_dict(getattr(prop, "enrichment", None)).get("neighbours"))
    return _checked(block, subject(prop))


def _checked(block, origin):
    if (
        block.get("status") != "ok"
        or block.get("subject") != origin
        or block.get("source") != "catastro_inspire_buildings"
        or block.get("searched_m") != 100
    ):
        return {}
    counts = [
        block.get(k)
        for k in (
            "buildings_within_50m",
            "buildings_within_100m",
            "agricultural_buildings_within_100m",
            "industrial_buildings_within_100m",
            "unknown_use_buildings_within_100m",
        )
    ]
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
        return {}
    if counts[0] > counts[1] or any(n > counts[1] for n in counts[2:]):
        return {}
    nearest = block.get("nearest_building_m")
    if counts[1] == 0:
        if nearest is not None:
            return {}
    elif (
        isinstance(nearest, bool)
        or not isinstance(nearest, (int, float))
        or not math.isfinite(nearest)
        or not 0 <= nearest <= 100
        or (nearest <= 50) != (counts[0] > 0)
    ):
        return {}
    if (
        cadastre.normalize_reference(_text(block.get("reference"))) is None
        or type(block.get("epsg")) is not int
        or block.get("epsg") not in (25829, 25830, 25831)
        or not _text(block.get("measured_at"))
    ):
        return {}
    return block
