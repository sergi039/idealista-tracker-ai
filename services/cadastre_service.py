"""The parcel behind a listing, from the Spanish cadastre (#430).

A cadastral reference is the most load-bearing fact a listing can carry: with
it the parcel's outline, its planning class and its surface become checkable,
and the shape of the outline is what actually decided property 774 -- the plot
fills 0.35 of its bounding box and the owner wants a regular one. Until now
that reference had nowhere to live and the geometry was fetched by hand.

**Two free, keyless endpoints, both verified live on 2026-08-20** against
`33016A003001530001HQ` (Bayas, property 774), answering in 0.15--0.28 s:

* the INSPIRE WFS `GetParcel` stored query, for the outline. The parameter
  really is spelled `STOREDQUERIE_ID` -- that is Catastro's own typo, not one
  here.
* `Consulta_DNPRC` on the OVC WCF service, for the class, the polígono/parcela
  locator, the paraje and the rustic subparcels. Its parameter is `RefCat`; the
  older ASMX endpoint spells the same thing `RC`, and sending one name to the
  other host returns a 200 carrying an error rather than an HTTP failure.

**Nothing here trusts an HTTP status.** Every Catastro error arrives as
`200 OK` with the failure in the body: `{"consulta_dnprcResult": {"control":
{"cuerr": 1}, "lerr": [{"cod": "3", ...}]}}` for the JSON service, and an empty
feature collection for the WFS. Both real refusals are in `tests/data/`.

**Three requests per press, and no retries.** The interactive path runs with
`max_attempts=1`: a press is one attempt per endpoint, and the retry is the
owner pressing again, which they can see the result of. Catastro publishes no
numeric rate limit and does document an ~10-day IP ban for abuse, so the
arithmetic has to be exact rather than approximately bounded -- three outbound
requests per uncached press, and the route's own `5/minute` limit caps that at
fifteen. A cached reference costs nothing. `CATASTRO_GATE` paces, the breaker
stops a broken loop, but it is dropping the retries that makes the number true.

**Six states per source, and only one of them is a measured negative.**
`not_found` means Catastro answered and there is no such parcel. Everything
else -- `refused`, `unavailable`, `malformed`, `unsupported_metric_crs` -- is an
absence of measurement: never cached, never scored, and never written over a
previous success. That split is #98's, and the run state is reduced from the
three sources the #153 way: the metric outline is **decisive** (the shape
metrics are the whole reason this exists), the map outline and the attributes
are advisory, so a metric success with a map refusal is `degraded` and stays
representable.

**The CRS is chosen, recorded, and checked.** The WFS reprojects into whatever
zone it is asked for -- including the wrong one, silently: asking for 25831 on
an Asturian parcel returns a negative easting and an area 1.17% out. So the
zone comes from the parcel's own `cp:referencePoint`, the EPSG code is stored
beside the metrics, and the response's `srsName` must echo what was requested.
The declared `cp:areaValue` is compared against the computed polygon area at
`max(1 m², 1%)` -- that catches a dropped ring, a truncated `posList` or the
wrong units, and it does **not** prove the CRS: measured, the neighbouring zone
agrees to within 0.01%. Two different checks for two different mistakes.
"""

from __future__ import annotations

import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import requests

from utils.http import HTTP_USER_AGENT, RateGate, request_with_retries

logger = logging.getLogger(__name__)

WFS_URL = "http://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx"
DNPRC_URL = (
    "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
    "COVCCallejero.svc/json/Consulta_DNPRC"
)

# Catastro publishes no requests-per-second figure anywhere; what it publishes
# is that abuse earns an IP ban of "generally ten days". One second between
# calls is conservative against an undocumented limit, and an interactive press
# never waits at all because the gate is idle between them.
CATASTRO_MIN_INTERVAL_S = 1.0
CATASTRO_GATE = RateGate(CATASTRO_MIN_INTERVAL_S, name="catastro")

REQUEST_TIMEOUT_S = 20

# 30 days: a parcel boundary changes when somebody splits or merges land, which
# is not a thing that happens between two presses of a button.
CACHE_TIMEOUT_S = 30 * 24 * 3600

# --- the states -------------------------------------------------------------

OK = "ok"
NOT_FOUND = "not_found"
REFUSED = "refused"
UNAVAILABLE = "unavailable"
MALFORMED = "malformed"
NOT_APPLICABLE = "not_applicable"
UNSUPPORTED_METRIC_CRS = "unsupported_metric_crs"

# The one state that means "Catastro looked and there is nothing". Every other
# non-ok state is an absence of measurement and must never be cached, scored,
# or written over an answer somebody already has.
MEASURED_STATES = (OK, NOT_FOUND)

RUN_OK = "ok"
RUN_DEGRADED = "degraded"
RUN_UNAVAILABLE = "unavailable"

# --- reference handling -----------------------------------------------------

_REFERENCE_RE = re.compile(r"^[0-9A-Z]{14}(?:[0-9A-Z]{4}(?:[0-9A-Z]{2})?)?$")


class CadastreError(RuntimeError):
    """A refusal, raised so it can never be read as a computed negative."""

    def __init__(self, state: str, detail: str = ""):
        super().__init__(detail or state)
        self.state = state
        self.detail = detail


def normalize_reference(raw: Optional[str]) -> Optional[str]:
    """A cadastral reference, or None if that is not what this is.

    Accepts the three lengths the services accept -- 14 (the parcel), 18 and 20
    (a unit within it) -- after dropping the spaces and dots people paste along
    with them. Case is folded up because both services answer on upper case and
    the reference is printed that way on every document.
    """
    text = re.sub(r"[\s.\-]", "", (raw or "")).upper()
    if not text or not _REFERENCE_RE.match(text):
        return None
    return text


def parcel_reference(reference: str) -> str:
    """The 14-character parcel key the WFS stored query is documented for.

    A full 20-character reference is accepted by the live service too, which
    was verified -- but the documented contract is 14, and relying on
    undocumented tolerance is how a client breaks on a Tuesday.
    """
    return reference[:14]


def sec_viewer_url(reference: str) -> str:
    """Where a human looks the same parcel up, for the link on the page."""
    return (
        "https://www1.sedecatastro.gob.es/CYCBienInmueble/OVCConCiud.aspx"
        f"?RefC={reference}&del=&mun="
    )


# --- CRS --------------------------------------------------------------------

# ETRS89 / UTM, the zones Catastro serves for the mainland and the Balearics.
_UTM_ZONES = ((-6.0, 25829), (0.0, 25830), (180.0, 25831))

# The Canaries are REGCAN95, not ETRS89, and the WFS document lists no
# 4082/4083 output. Asking for 25830 there would return numbers -- Catastro
# reprojects whatever it is asked for -- and they would be metres from a datum
# the islands do not use. So the metric copy is refused and the map outline is
# kept, which is the honest half.
_CANARY_BOX = (27.0, 30.0, -19.0, -13.0)


def metric_epsg_for(lat: float, lon: float) -> Optional[int]:
    """The projected CRS to measure this parcel in, or None if there is none."""
    south, north, west, east = _CANARY_BOX
    if south <= lat <= north and west <= lon <= east:
        return None
    for edge, epsg in _UTM_ZONES:
        if lon < edge:
            return epsg
    return 25831


# --- geometry ---------------------------------------------------------------

_GML_NS = "{http://www.opengis.net/gml/3.2}"
_CP_NS = "{http://inspire.ec.europa.eu/schemas/cp/4.0}"


def _parse_gml(text: str, expected_epsg: int) -> Dict[str, Any]:
    """One parcel out of a WFS `GetParcel` response.

    Raises rather than returns on anything unusable, so a caller cannot read a
    refusal as an outline.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CadastreError(MALFORMED, f"not XML: {exc}") from exc

    parcel = root.find(f".//{_CP_NS}CadastralParcel")
    if parcel is None:
        # An empty FeatureCollection is Catastro answering "no such parcel" --
        # the one measured negative on this path.
        raise CadastreError(NOT_FOUND, "no CadastralParcel in the response")

    pos_lists = parcel.findall(f".//{_GML_NS}posList")
    if not pos_lists:
        raise CadastreError(MALFORMED, "a parcel with no coordinates")

    srs_names = {
        element.get("srsName") for element in parcel.iter() if element.get("srsName")
    }
    if not any(str(expected_epsg) in (name or "") for name in srs_names):
        # The service reprojects into whatever it is asked for and says so in
        # the response; a mismatch means the numbers are not in the CRS the
        # metrics would be computed against.
        raise CadastreError(
            MALFORMED, f"asked for EPSG:{expected_epsg}, got {sorted(srs_names)}"
        )

    rings: List[List[Tuple[float, float]]] = []
    for pos_list in pos_lists:
        numbers = [float(value) for value in (pos_list.text or "").split()]
        if len(numbers) < 8 or len(numbers) % 2:
            raise CadastreError(MALFORMED, "a ring with an odd coordinate count")
        rings.append(list(zip(numbers[0::2], numbers[1::2])))

    declared = parcel.find(f"{_CP_NS}areaValue")
    reference = parcel.find(f"{_CP_NS}nationalCadastralReference")
    point = parcel.find(f".//{_CP_NS}referencePoint//{_GML_NS}pos")
    reference_point = None
    if point is not None and point.text:
        values = [float(value) for value in point.text.split()]
        if len(values) == 2:
            # The reference point comes back in 4326, where Catastro's axis
            # order is lat lon.
            reference_point = (values[0], values[1])

    return {
        "rings": rings,
        "declared_area_m2": float(declared.text)
        if declared is not None and declared.text
        else None,
        "reference": (reference.text or "").strip() if reference is not None else None,
        "reference_point": reference_point,
    }


def _ring_area(ring: List[Tuple[float, float]]) -> float:
    """Shoelace area of a closed ring, in the units its coordinates are in."""
    total = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _ring_perimeter(ring: List[Tuple[float, float]]) -> float:
    total = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def shape_metrics(rings: List[List[Tuple[float, float]]]) -> Dict[str, Any]:
    """What the outline is like, from coordinates already in metres.

    Two ratios and the box, because both ratios are dimensionless and therefore
    survive the UTM scale factor untouched:

    * `bbox_fill_ratio` -- how much of its own bounding box the parcel fills.
      774 fills 0.35 of a 120 x 146 m box, which is what "L-shaped with a neck"
      looks like as a number.
    * `polsby_popper` -- 4*pi*area / perimeter^2. A circle is 1, a square about
      0.785, 774 is 0.30.

    The largest inscribed square is deliberately **not** here. A grid over the
    axis-aligned case is not an algorithm -- it underestimates a parcel whose
    long side runs diagonally, by an amount nobody has bounded -- and a number
    that decides a purchase should not be an approximation nobody labelled.
    """
    outer = max(rings, key=_ring_area)
    area = _ring_area(outer)
    perimeter = _ring_perimeter(outer)
    xs = [point[0] for point in outer]
    ys = [point[1] for point in outer]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    box = width * height

    return {
        "area_m2": round(area, 1),
        "perimeter_m": round(perimeter, 1),
        "bbox_m": {"we": round(width, 1), "ns": round(height, 1)},
        "bbox_fill_ratio": round(area / box, 3) if box else None,
        "polsby_popper": round(4 * math.pi * area / (perimeter**2), 3)
        if perimeter
        else None,
        "vertices": len(outer) - 1,
        "rings": len(rings),
    }


def area_agrees(computed: float, declared: Optional[float]) -> bool:
    """Does our arithmetic match the area Catastro declares for the parcel?

    A parser check and nothing more. It catches a dropped ring, a truncated
    coordinate list and the wrong units, all of which move the number a long
    way. It does **not** check the CRS: measured on 774, the neighbouring UTM
    zone computes 6192.8 m² against a declared 6193, and even a plainly wrong
    zone lands 1.17% out. `srsName` is what checks the CRS.

    The tolerance is `max(1 m², 1%)` because `areaValue` is an integer: on a
    30 m² garage a whole square metre is 3%.
    """
    if declared is None:
        return True
    return abs(computed - declared) <= max(1.0, declared * 0.01)


# --- transport --------------------------------------------------------------


def _get(url: str, params: Dict[str, str]) -> requests.Response:
    """One request. One attempt. The retry is the owner pressing again."""
    return request_with_retries(
        requests.get,
        url,
        params=params,
        headers={"User-Agent": HTTP_USER_AGENT},
        timeout=REQUEST_TIMEOUT_S,
        max_attempts=1,
        gate=CATASTRO_GATE,
        logger=logger,
    )


def _fetch_outline(reference: str, epsg: int) -> Dict[str, Any]:
    params = {
        "service": "wfs",
        "version": "2",
        "request": "getfeature",
        "STOREDQUERIE_ID": "GetParcel",
        "refcat": parcel_reference(reference),
        "srsname": f"EPSG::{epsg}",
    }
    try:
        response = _get(WFS_URL, params)
    except requests.RequestException as exc:
        raise CadastreError(UNAVAILABLE, str(exc)) from exc
    if response.status_code != 200:
        raise CadastreError(REFUSED, f"HTTP {response.status_code}")
    return _parse_gml(response.text, epsg)


def _fetch_attributes(reference: str) -> Dict[str, Any]:
    try:
        response = _get(DNPRC_URL, {"RefCat": reference})
    except requests.RequestException as exc:
        raise CadastreError(UNAVAILABLE, str(exc)) from exc
    if response.status_code != 200:
        raise CadastreError(REFUSED, f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise CadastreError(MALFORMED, "not JSON") from exc
    return parse_attributes(payload)


def parse_attributes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a `Consulta_DNPRC` answer worth storing.

    Every error is delivered inside a 200 with `control.cuerr` set and the
    reason in `lerr`, so the status code says nothing and this is where a
    refusal is actually detected. Two of them are in `tests/data/`, taken from
    the live service.
    """
    result = payload.get("consulta_dnprcResult") or {}
    control = result.get("control") or {}
    if control.get("cuerr"):
        errors = result.get("lerr") or []
        first = errors[0] if errors else {}
        code = str(first.get("cod") or "")
        detail = first.get("des") or "refused"
        # 9 is "the reference does not exist" -- Catastro looked. Everything
        # else on this path is a malformed request or a service problem, which
        # is not a fact about the parcel.
        raise CadastreError(
            NOT_FOUND if code == "9" else MALFORMED, f"{code}: {detail}"
        )

    bico = result.get("bico") or {}
    bi = bico.get("bi") or {}
    if not bi:
        # A bare 14-character reference returns a LIST of the units on the
        # parcel rather than one record. Storing the first would be picking a
        # flat out of a block at random.
        if result.get("lrcdnp"):
            raise CadastreError(
                NOT_APPLICABLE, "the reference names a parcel with several units"
            )
        raise CadastreError(MALFORMED, "no bien inmueble in the response")

    idbi = bi.get("idbi") or {}
    dt = bi.get("dt") or {}
    locs = (dt.get("locs") or {}).get("lous") or {}
    rustic = locs.get("lorus") or {}
    cpp = rustic.get("cpp") or {}
    debi = bi.get("debi") or {}

    # `lspr` is a list of subparcels in the JSON service and an object
    # wrapping `spr` in the XML one -- measured against both. Reading only the
    # second shape drops every subparcel silently, which on a rustic parcel is
    # most of what the document says.
    raw_subparcels = bico.get("lspr")
    if isinstance(raw_subparcels, dict):
        raw_subparcels = raw_subparcels.get("spr")

    subparcels = []
    for entry in _as_list(raw_subparcels):
        description = entry.get("dspr") or {}
        subparcels.append(
            {
                "code": entry.get("cspr"),
                "use_code": description.get("ccc"),
                "use": description.get("dcc"),
                "intensity": description.get("ip"),
                "area_m2": _as_float(description.get("ssp")),
            }
        )

    return {
        # UR / RU, per unit. Never inferred from the reference's shape:
        # verified on 774, a rustic-numbered parcel holds an urban bien.
        "class": idbi.get("cn"),
        "province": dt.get("np"),
        "municipality": dt.get("nm"),
        "ine_code": f"{(dt.get('loine') or {}).get('cp', '')}{(dt.get('loine') or {}).get('cm', '')}"
        or None,
        "poligono": _as_int(cpp.get("cpo")),
        "parcela": _as_int(cpp.get("cpa")),
        "paraje": rustic.get("npa"),
        "use": debi.get("luso"),
        "built_area_m2": _as_float(debi.get("sfc")),
        "description": bi.get("ldt"),
        "subparcels": subparcels,
    }


def _as_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --- the run ----------------------------------------------------------------


def _cache_key(reference: str, epsg: Optional[int], what: str) -> str:
    # The version suffix is the escape hatch a shared cache needs: a poisoned
    # entry now outlives the process that fetched it, and bumping this is
    # cheaper than reasoning about which references are wrong.
    return f"catastro_{what}_{reference}_{epsg or 'na'}_v1"


def _cache_get(key: str):
    """A cache that cannot be reached is a miss, never an error."""
    from utils.cache import cache

    try:
        return cache.get(key)
    except Exception as exc:
        logger.debug("Cadastre cache read skipped: %s", exc)
        return None


def _cache_set(key: str, value: Any) -> None:
    from utils.cache import cache

    try:
        cache.set(key, value, timeout=CACHE_TIMEOUT_S)
    except Exception as exc:
        logger.debug("Cadastre cache write skipped: %s", exc)


def _source(fetch, *, cache_key: Optional[str] = None) -> Dict[str, Any]:
    """Run one source, reducing whatever happens to a state plus its payload.

    Only a *measured* outcome is cached. A refusal is not a fact about the
    parcel, so caching it would answer the next press with a failure nobody
    re-checked -- #98 inside a cache.
    """
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return dict(cached, cached=True)

    try:
        payload = fetch()
    except CadastreError as exc:
        return {"status": exc.state, "detail": exc.detail}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Cadastre source failed: %s", exc, exc_info=True)
        return {"status": UNAVAILABLE, "detail": str(exc)}

    result = {"status": OK, **payload}
    if cache_key:
        _cache_set(cache_key, result)
    return result


def _run_state(metric: str, mapped: str, attributes: str) -> str:
    """One verdict out of three sources, decisive and advisory (#153's shape).

    The metric outline is decisive: the shape metrics are what this feature is
    for, and a run that did not produce them did not do what it was asked. The
    map outline and the attributes are advisory -- a listing whose polygon was
    measured is better off with `degraded` than with a refusal, and collapsing
    the two would report failure for a run that produced the number the owner
    wanted.
    """
    if metric != OK:
        return RUN_UNAVAILABLE
    if mapped == OK and attributes == OK:
        return RUN_OK
    return RUN_DEGRADED


def fetch_parcel(reference: str) -> Dict[str, Any]:
    """Everything Catastro will say about one reference. No database, no Flask.

    Three requests at most, in this order, because the first one supplies the
    reference point the second one needs to pick its zone:

    1. the outline in EPSG:4326 -- the map copy, and the reference point;
    2. the outline in the parcel's own UTM zone -- the metric copy;
    3. the attributes.

    The zone cannot be chosen before step 1 without a coordinate for the
    listing, and the listing's coordinate is not the parcel's -- for 532 of the
    located rows it is a locality centroid (#358). The parcel's own reference
    point is the only one that is certainly inside it.
    """
    normalized = normalize_reference(reference)
    if not normalized:
        raise CadastreError(MALFORMED, f"not a cadastral reference: {reference!r}")

    mapped = _source(
        lambda: _fetch_outline(normalized, 4326),
        cache_key=_cache_key(normalized, 4326, "outline"),
    )

    point = mapped.get("reference_point") if mapped.get("status") == OK else None
    epsg = metric_epsg_for(*point) if point else None

    if mapped.get("status") != OK:
        # Without the map copy there is no reference point, so there is no zone
        # to ask for. Reported as unavailable rather than guessed from the
        # listing's own coordinate, which may be a village centre.
        metric = {"status": UNAVAILABLE, "detail": "no reference point to pick a zone"}
    elif epsg is None:
        metric = {
            "status": UNSUPPORTED_METRIC_CRS,
            "detail": "outside the ETRS89 UTM zones Catastro serves",
        }
    else:
        metric = _source(
            lambda: _fetch_outline(normalized, epsg),
            cache_key=_cache_key(normalized, epsg, "outline"),
        )

    geometry: Dict[str, Any] = {}
    if metric.get("status") == OK:
        metrics = shape_metrics(metric["rings"])
        declared = metric.get("declared_area_m2")
        if not area_agrees(metrics["area_m2"], declared):
            metric = {
                "status": MALFORMED,
                "detail": (
                    f"computed {metrics['area_m2']} m2 against a declared {declared} m2"
                ),
            }
        else:
            geometry = {
                **metrics,
                "epsg": epsg,
                "declared_area_m2": declared,
            }

    attributes = _source(
        lambda: _fetch_attributes(normalized),
        cache_key=_cache_key(normalized, None, "attributes"),
    )

    block: Dict[str, Any] = {
        "reference": normalized,
        "sources": {
            "metric_geometry": _status_only(metric),
            "map_geometry": _status_only(mapped),
            "attributes": _status_only(attributes),
        },
        "run_state": _run_state(
            metric.get("status"), mapped.get("status"), attributes.get("status")
        ),
        "viewer_url": sec_viewer_url(normalized),
    }
    if geometry:
        block["geometry"] = geometry
    if mapped.get("status") == OK:
        block["outline_4326"] = [
            [[round(lon, 7), round(lat, 7)] for lat, lon in ring]
            for ring in mapped["rings"]
        ]
        if mapped.get("reference_point"):
            block["reference_point"] = {
                "lat": mapped["reference_point"][0],
                "lon": mapped["reference_point"][1],
            }
    if attributes.get("status") == OK:
        block["attributes"] = {
            key: value for key, value in attributes.items() if key != "status"
        }
    return block


def _status_only(source: Dict[str, Any]) -> Dict[str, Any]:
    out = {"status": source.get("status")}
    if source.get("detail"):
        out["detail"] = source["detail"]
    if source.get("cached"):
        out["cached"] = True
    return out


ENRICHMENT_KEY = "cadastre"


def apply_to_property(
    prop: Any, reference: str, *, commit: bool = True
) -> Dict[str, Any]:
    """Fetch the parcel and record it, holding the row for the write.

    The network happens **outside** the lock and the block is read and written
    **inside** it: `enrichment` is one JSON column, so every write is a
    read-modify-write over all of it, and a run that decided what to keep from
    the copy its own session loaded is #339's defect exactly. The measurement
    here takes seconds, which is the interval that mattered there.

    A refusal never overwrites a previous success: the states are separated for
    that reason, and this is the only place the rule can be enforced, because
    it is the only place that can see both.
    """
    from services.enrichment_write import check_writable, locked_write

    normalized = normalize_reference(reference)
    if not normalized:
        raise CadastreError(MALFORMED, f"not a cadastral reference: {reference!r}")

    fetched = fetch_parcel(normalized)

    locked = check_writable(prop, commit)
    with locked_write(prop, locked=locked, commit=commit):
        enrichment = dict(prop.enrichment or {})
        previous = enrichment.get(ENRICHMENT_KEY) or {}
        merged = _merge(previous, fetched)
        enrichment[ENRICHMENT_KEY] = merged
        prop.enrichment = enrichment
        # The column and the block are written under the same lock: the
        # reference is what every later check keys on, and a row whose column
        # and block name different parcels is worse than either alone.
        prop.cadastral_reference = normalized
    return merged


def _merge(previous: Dict[str, Any], fetched: Dict[str, Any]) -> Dict[str, Any]:
    """Keep what was measured before when this run could not measure it.

    Per source, not per run: a press that gets the outline and loses the
    attributes should keep the attributes somebody already has, and say in
    `sources` that this run did not fetch them.
    """
    if previous.get("reference") != fetched.get("reference"):
        # A different parcel entirely -- nothing of the old one applies.
        return fetched

    merged = dict(fetched)
    for key, source_name in (
        ("geometry", "metric_geometry"),
        ("outline_4326", "map_geometry"),
        ("attributes", "attributes"),
    ):
        state = (fetched.get("sources") or {}).get(source_name, {}).get("status")
        if state not in MEASURED_STATES and key in previous:
            merged[key] = previous[key]
            merged["sources"][source_name] = dict(
                merged["sources"][source_name], kept_previous=True
            )
    if "reference_point" not in merged and "reference_point" in previous:
        merged["reference_point"] = previous["reference_point"]
    return merged
