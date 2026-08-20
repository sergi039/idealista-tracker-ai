"""Hazardous neighbours for a property, from OpenStreetMap, free (#437).

Property 793 is a 1,273 m² plot advertised as a "quiet environment surrounded
by nature". A cement works stands 1.1 km away, a coal yard 1.6 km, a bank of
LPG spheres 1.8 km and a coal-fired power station 2.1 km, and until this module
existed `/properties/793` said none of it: net income, population,
supermarkets, hospitals, beaches and drive times, and nothing at all about what
is a kilometre downwind. The only trace anywhere on the page was a listing
photograph in which the Repsol spheres happen to be visible on the horizon.

That is #98 in the one place it costs most. **A buyer reading a page that says
nothing concludes there is nothing there, and the page never claimed to have
looked.** So the four states are the whole feature and not a detail of it:

* `ok` -- the scan ran and something qualifies;
* `none_within_radius` -- the scan ran and nothing does. A *measurement*;
* `unavailable` -- Overpass refused. Never rendered as a clean neighbourhood,
  never cached, and never allowed to overwrite an earlier measurement;
* `no_coordinates` -- there is no point to measure from.

Four more rules, each imported rather than reinvented:

**What counts as a hazard lives in `services/hazard_rules.py`**, the sibling of
`services/place_rules.py`. This module never looks at a tag; it measures,
groups and stores what that table admits.

**An approximate coordinate cannot support a 1.1 km claim.** 532 of the 725
located rows are a locality centroid (#358), including 793's own, and
`services/coordinate_quality.py` grants those 5 km of slack. So a stored
measurement is restated on read by `read_verdict`, exactly as
`sea_distance_service.parcel_measurement` restates its own: a precise row gets
one number twice, an approximate one gets a band, and no surface ever prints a
point distance from a centroid.

**The bearing is recorded and never interpreted.** Whether 1.1 km matters
depends on the wind and there is no free per-listing wind rose here, so the
block carries `bearing_deg` and its cardinal and stops. Writing "downwind" into
a measurement field would be an inference wearing a measurement's clothes,
which is the STATUS-002 mistake.

**Every distance is straight-line and the block says so.** Air and noise
travel in straight lines; a drive time would cost Distance Matrix elements and
answer a different question.

What OSM cannot answer is named on the page rather than left for the reader to
assume away: actual emissions (PRTR-España publishes those), measured air
quality (Asturias runs a station named *Xivares* inside this very
urbanisation), and a plant that is approved but not yet built.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Float, and_, cast, func, or_
from sqlalchemy.orm.attributes import flag_modified

from services import hazard_rules
from services.coordinate_quality import (
    coordinate_slack_m,
    distance_bounds_m,
    normalize_accuracy,
)
from services.enrichment_origin import ORIGIN_TOLERANCE_DEG, origin_of, origins_agree
from services.enrichment_write import check_writable, locked_write
from utils.cache import cache_enrichment_data, get_cached_enrichment_data

logger = logging.getLogger(__name__)

ENRICHMENT_KEY = "hazards"

STATUS_OK = "ok"
STATUS_NONE = "none_within_radius"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_COORDINATES = "no_coordinates"
# Not stored values: two things `read_verdict` can answer that no writer ever
# writes. Named with the rest so every state it can return is findable in one
# place. `STATUS_MISSING` is "there is no block"; `STATUS_STALE_ORIGIN` is
# "there is one, and it describes a point this listing has since moved away
# from" -- a scan is cheap and a measurement of the wrong place is not a
# measurement of this one.
STATUS_MISSING = "missing_hazards"
STATUS_STALE_ORIGIN = "stale_origin"

# A status the data can be trusted for. It survives a later refusal as
# last-known-good, for the reason `sea_distance_service` keeps its own: a
# cement works does not move, and replacing a measurement with "the network was
# down" loses the only thing anybody looked up.
MEASURED_STATUSES = (STATUS_OK, STATUS_NONE)

# What a rerun could improve. `no_coordinates` is not here -- re-asking
# Overpass will never invent a coordinate, and a row that gains one is a
# different row (the geocoder writes `location_accuracy`, and the backfill's
# scope only ever holds rows that have a coordinate at all).
RETRYABLE_STATUSES = frozenset({STATUS_UNAVAILABLE})

SOURCE = "openstreetmap"
DISTANCE_BASIS = "straight_line"

# A month, like the preset candidates: this is a claim about the ground, and
# the expensive part of a bulk run is the 5 s Overpass gate in front of it.
_CACHE_TTL_S = 60 * 60 * 24 * 30
# The cache holds the *elements*, not the verdict, so changing the rules table
# does not need a bump here -- only changing the query does.
_CACHE_KEY = "hazard_scan_v1"

# How many facilities the block carries, and how many OSM elements it names
# per facility. Both are disclosed rather than silently applied: the counts
# beside them say how many there really were.
#
# The list is kept **nearest first** rather than worst first, and that is what
# makes the cut safe rather than merely tidy: everything dropped is further
# away than everything kept, so the scorer -- which takes the worst item --
# cannot have the answer hidden from it by an item it never saw, except in the
# case where more than `MAX_ITEMS` facilities are nearer than that one, at
# which point the listing is inside an industrial estate and the score is 0
# whichever of them wins. Ordering by severity would break exactly that
# guarantee: a nuisance next door would push an emitter off the end.
MAX_ITEMS = 20
MAX_ELEMENTS_PER_ITEM = 6

# Two qualifying elements with no operator and no name -- two LNG tanks in one
# farm, say -- are one thing on the ground. They cannot be keyed, so they are
# clustered by position instead. 500 m is a facility's own footprint here: the
# Repsol compound at property 793 is ~350 m across and the Musel tank farms
# ~400 m. Anything wider would start merging neighbours in a *polígono*, which
# is the direction that hides a hazard rather than duplicating one.
CLUSTER_M = 500

# How far apart two elements of the *same operator* may be and still be one
# plant. A key on its own is not a place: `operator=Enagás` names a national
# gas transporter, and two of its installations 5.6 km apart in different
# directions are two hazards, not one with a misleadingly near distance
# (review, 2026-08-20). Measured on the committed fixture, the real facilities
# span far less than this: ArcelorMittal's tip, acería, turbines and stacks
# sit within 1346 m of each other, the Parque de Carbones within 1255 m,
# Exolum's Musel terminals within 722 m, Repsol's fifteen elements within
# 336 m. 2 km keeps every one of them whole with room to spare.
FACILITY_SPAN_M = 2_000

_CARDINALS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line metres. The same formula `osm_places` uses."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from the origin to the feature, degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def cardinal(bearing: Optional[float]) -> Optional[str]:
    """The 16-point compass name for a bearing, or None for no bearing."""
    if bearing is None:
        return None
    return _CARDINALS[int((float(bearing) % 360.0) / 22.5 + 0.5) % 16]


def _coordinate(value: Any, limit: float) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or abs(number) > limit:
        return None
    return number


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _element_point(element: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    centre = (
        element.get("center") if isinstance(element.get("center"), dict) else element
    )
    lat = _coordinate(centre.get("lat"), 90.0)
    lon = _coordinate(centre.get("lon"), 180.0)
    if lat is None or lon is None:
        return None
    return lat, lon


def fetch_elements(
    enrichment_service: Any, lat: float, lon: float
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Any]]:
    """Every candidate element around this point, from cache or Overpass.

    A module function rather than a method because it is this feature's one
    network seam, and a seam a test can hold is worth more than a tidier
    class: `tests/conftest.py` points it at "Overpass replied and there is
    nothing here" for every suite that does not care, exactly as it already
    does for `services.osm_places.lookup_candidates`.

    The transport itself stays where the hard rule in CLAUDE.md puts it --
    `EnrichmentService._overpass_elements`, which owns the gate, the
    User-Agent and the three refusals Overpass delivers (#144). Nothing here
    is a second client.

    A refusal is returned and **never** cached: a cached refusal would go on
    answering "nothing nearby" for a month, which is the very thing the
    refusal exists to prevent.
    """
    cached = get_cached_enrichment_data(lat, lon, _CACHE_KEY)
    if isinstance(cached, dict) and isinstance(cached.get("elements"), list):
        return cached, None

    query = hazard_rules.overpass_query(lat, lon)
    elements, failure = enrichment_service._overpass_elements(query)
    if failure is not None:
        return None, failure

    trimmed: List[Dict[str, Any]] = []
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        point = _element_point(element)
        if point is None:
            continue
        tags = element.get("tags")
        trimmed.append(
            {
                "type": element.get("type"),
                "id": element.get("id"),
                "lat": point[0],
                "lon": point[1],
                "tags": tags if isinstance(tags, dict) else {},
            }
        )
    # `returned` is the count Overpass *answered with*, not the count that
    # survived the trim above -- an element with no readable centre is dropped
    # here, and counting after that would let a scan that hit the server-side
    # cap read as one that did not. The cap is the only way this feature can
    # quietly show a short list, so the number that detects it has to be the
    # raw one, and it has to survive into the cache.
    payload = {"elements": trimmed, "returned": len(elements or [])}
    try:
        cache_enrichment_data(lat, lon, _CACHE_KEY, payload, timeout=_CACHE_TTL_S)
    except Exception:
        logger.warning("Could not cache hazard scan for %s,%s", lat, lon, exc_info=True)
    return payload, None


def _clustered(
    members: List[Dict[str, Any]], radius_m: float, *, same_kind: bool
) -> List[List[Dict[str, Any]]]:
    """Split elements into discs of `radius_m`, nearest to the property first.

    Membership is measured against the cluster's **anchor** and never against
    any member: chaining member to member would let a line of tanks 400 m
    apart walk a "facility" across several kilometres, and the distance the
    item then reports would describe one end of it. A disc is a bound; a chain
    is not. Running nearest-first is what makes the anchor the member closest
    to the property, which is the distance the block goes on to report.
    """
    clusters: List[List[Dict[str, Any]]] = []
    for candidate in sorted(members, key=lambda member: member["distance_m"]):
        for cluster in clusters:
            anchor = cluster[0]
            if same_kind and anchor["kind"] != candidate["kind"]:
                continue
            if (
                haversine_m(
                    anchor["lat"], anchor["lon"], candidate["lat"], candidate["lon"]
                )
                <= radius_m
            ):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
    return clusters


class HazardService:
    """Scans for hazardous neighbours and stores `enrichment["hazards"]`."""

    def __init__(self, enrichment_service: Optional[Any] = None):
        if enrichment_service is None:
            from services.enrichment_service import EnrichmentService

            enrichment_service = EnrichmentService()
        self.enrichment_service = enrichment_service

    # -- discovery ---------------------------------------------------------

    # -- measurement -------------------------------------------------------

    def measure(self, lat: float, lon: float) -> Dict[str, Any]:
        """What qualifies around this point, grouped into facilities.

        Pure: it knows the point it was handed and nothing about the row, so
        the slack arithmetic and the provenance belong to `enrich` and
        `read_verdict`. That split is what lets a re-geocoded row be restated
        without a second Overpass call.
        """
        answer, failure = fetch_elements(self.enrichment_service, lat, lon)
        if failure is not None:
            return {
                "status": STATUS_UNAVAILABLE,
                "reason": getattr(failure, "reason", "unavailable"),
                "searched_m": hazard_rules.SEARCH_RADIUS_M,
            }

        elements = answer.get("elements") or []
        # `out ... N` truncates in whatever order the server produced, so a
        # scan that reached the cap has not necessarily seen the nearest
        # things last -- it has simply not seen everything. Saying so is the
        # only honest option; silently shortening the list is the defect.
        truncated = int(answer.get("returned") or 0) >= hazard_rules.ELEMENT_LIMIT

        qualifying: List[Dict[str, Any]] = []
        for element in elements:
            verdict = hazard_rules.classify(element.get("tags"))
            if verdict is None:
                continue
            distance = haversine_m(lat, lon, element["lat"], element["lon"])
            if distance > hazard_rules.SEARCH_RADIUS_M:
                # A cached answer taken for a neighbouring point inside the
                # same 11 m cache cell can reach a metre or two further; a
                # candidate past the radius this block claims to have searched
                # would make `searched_m` a lie.
                continue
            qualifying.append(
                {
                    "tags": element.get("tags") or {},
                    "lat": element["lat"],
                    "lon": element["lon"],
                    "distance_m": distance,
                    "kind": verdict.kind,
                    "severity": verdict.severity,
                    "evidence": verdict.evidence,
                    "osm_type": element.get("type"),
                    "osm_id": element.get("id"),
                }
            )

        items = self._group(qualifying, lat, lon)
        return {
            "status": STATUS_OK if items else STATUS_NONE,
            "searched_m": hazard_rules.SEARCH_RADIUS_M,
            "truncated": truncated,
            "item_count": len(items),
            "items": items[:MAX_ITEMS],
            "candidates_seen": int(answer.get("returned") or 0),
            "qualifying_elements": len(qualifying),
        }

    @staticmethod
    def _group(
        qualifying: List[Dict[str, Any]], lat: float, lon: float
    ) -> List[Dict[str, Any]]:
        """One entry per facility, nearest first.

        Keyed elements group by `hazard_rules.facility_key` after
        `hazard_rules.merge_keys` has folded a facility's name into its
        operator. Keyless ones -- an unnamed tank with `content=LNG` says what
        it holds and nothing about who holds it -- cluster with the nearest
        keyless neighbour of the same kind inside `CLUSTER_M`.
        """
        keyed: Dict[str, List[Dict[str, Any]]] = {}
        keyless: List[Dict[str, Any]] = []
        for candidate in qualifying:
            key = hazard_rules.facility_key(candidate["tags"])
            if key:
                keyed.setdefault(key, []).append(candidate)
            else:
                keyless.append(candidate)

        canonical = hazard_rules.merge_keys(keyed.keys())
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for key, members in keyed.items():
            groups.setdefault(canonical.get(key, key), []).extend(members)

        clusters: List[List[Dict[str, Any]]] = []
        # A shared key says who runs it, never where it is, so each key group
        # is still split by distance -- one operator's two sites are two
        # hazards, and reporting them as one would give the far one the near
        # one's distance and bearing.
        for members in groups.values():
            clusters.extend(_clustered(members, FACILITY_SPAN_M, same_kind=False))
        # A keyless element has said what it holds and nothing about who
        # holds it, so position is all there is to group it by -- and a
        # tighter radius, since nothing but proximity connects them.
        clusters.extend(_clustered(keyless, CLUSTER_M, same_kind=True))

        items = [HazardService._item(cluster, lat, lon) for cluster in clusters]
        items.sort(key=lambda item: item["origin_distance_m"])
        return items

    @staticmethod
    def _item(members: List[Dict[str, Any]], lat: float, lon: float) -> Dict[str, Any]:
        """One facility, from the elements OSM maps it as."""
        members = sorted(
            members,
            key=lambda m: (hazard_rules.severity_rank(m["severity"]), m["distance_m"]),
        )
        nearest = min(members, key=lambda m: m["distance_m"])
        severity = members[0]["severity"]

        names = []
        for member in members:
            name = member["tags"].get("name")
            if name and name not in names:
                names.append(name)
        operators = [
            m["tags"].get("operator") for m in members if m["tags"].get("operator")
        ]
        # One name is the facility's own; several mean OSM has mapped the
        # parts separately (*Vertedero ArcelorMittal*, *Acería de Veriña*,
        # *Turbina A*, *Turbina B*), and then the operator is what a reader
        # recognises. With neither, the kind is what the surfaces render.
        if len(names) == 1:
            label = names[0]
        elif operators:
            label = operators[0]
        elif names:
            label = names[0]
        else:
            label = None

        kinds: List[str] = []
        evidence: List[str] = []
        for member in members:
            if member["kind"] not in kinds:
                kinds.append(member["kind"])
            if member["evidence"] not in evidence:
                evidence.append(member["evidence"])

        return {
            "name": label,
            "names": names[:MAX_ELEMENTS_PER_ITEM],
            "kind": members[0]["kind"],
            "kinds": kinds,
            "severity": severity,
            "evidence": evidence[:MAX_ELEMENTS_PER_ITEM],
            "origin_distance_m": int(round(nearest["distance_m"])),
            "bearing_deg": round(
                bearing_deg(lat, lon, nearest["lat"], nearest["lon"]), 1
            ),
            "lat": nearest["lat"],
            "lon": nearest["lon"],
            # Only elements that can actually be linked back to OSM: a
            # missing type or id would render as a broken URL, and the
            # template's job is not to guess what a half-read element was.
            "elements": [
                {"type": str(member["osm_type"]), "id": member["osm_id"]}
                for member in members[:MAX_ELEMENTS_PER_ITEM]
                if member.get("osm_type") and member.get("osm_id") is not None
            ],
            "element_count": len(members),
        }

    # -- storage -----------------------------------------------------------

    def enrich(self, prop: Any, commit: bool = False) -> Dict[str, Any]:
        """Measure and store `enrichment["hazards"]`.

        The order is the one `services/enrichment_write.py` owns and every
        writer of this column obeys since #339: validate the caller **before**
        the lookup, take the row **after** it, read the stored block from the
        locked row rather than from the copy this session loaded.
        """
        locked = check_writable(prop, commit)

        lat = _coordinate(getattr(prop, "location_lat", None), 90.0)
        lon = _coordinate(getattr(prop, "location_lon", None), 180.0)
        accuracy = normalize_accuracy(getattr(prop, "location_accuracy", None))
        now_iso = datetime.now(timezone.utc).isoformat()

        if lat is None or lon is None:
            with locked_write(prop, locked=locked, commit=commit):
                previous = self._stored(prop)
                # A row can lose its coordinate: `refresh=True` clears it
                # before geocoding and a refusal leaves it cleared (#393), and
                # `enrich_property` then runs the free pass on a row with no
                # point at all. Overwriting here would delete a measurement
                # that cost a round trip -- and, because `no_coordinates` is
                # not retryable, take the row out of the backfill's scope for
                # good. So it is kept, exactly as a network refusal is
                # (review, 2026-08-20).
                if previous.get("status") in MEASURED_STATUSES:
                    payload = {
                        **previous,
                        "last_attempt_status": STATUS_NO_COORDINATES,
                        "last_attempt_at": now_iso,
                    }
                else:
                    payload = {
                        "status": STATUS_NO_COORDINATES,
                        "source": SOURCE,
                        "distance_basis": DISTANCE_BASIS,
                        "updated_at": now_iso,
                        "last_attempt_status": STATUS_NO_COORDINATES,
                        "last_attempt_at": now_iso,
                    }
                self._store(prop, payload)
            return payload

        measurement = self.measure(lat, lon)

        with locked_write(prop, locked=locked, commit=commit):
            previous = self._stored(prop)

            if (
                measurement["status"] == STATUS_UNAVAILABLE
                and previous.get("status") in MEASURED_STATUSES
            ):
                # A refusal never overwrites a measurement. The scan is free
                # and the next run repeats it; what cannot be recovered is the
                # answer this row already had.
                payload = {
                    **previous,
                    "last_attempt_status": STATUS_UNAVAILABLE,
                    "last_attempt_at": now_iso,
                    "last_attempt_reason": measurement.get("reason"),
                }
            else:
                payload = {
                    **measurement,
                    "source": SOURCE,
                    "distance_basis": DISTANCE_BASIS,
                    "origin": {"lat": lat, "lon": lon},
                    "origin_accuracy": accuracy,
                    "slack_m": coordinate_slack_m(accuracy),
                    "updated_at": now_iso,
                    "last_attempt_status": measurement["status"],
                    "last_attempt_at": now_iso,
                }
            self._store(prop, payload)
        return payload

    @staticmethod
    def _stored(prop: Any) -> Dict[str, Any]:
        """The block as the *locked* row holds it. Never read before the lock."""
        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        block = enrichment.get(ENRICHMENT_KEY)
        return block if isinstance(block, dict) else {}

    @staticmethod
    def _store(prop: Any, payload: Dict[str, Any]) -> None:
        """Assign the block. The caller holds the lock and owns the commit."""
        # `enrichment` is a plain JSON column, not a MutableDict: mutating the
        # nested dict in place would never reach the UPDATE.
        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        enrichment[ENRICHMENT_KEY] = payload
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


def read_verdict(prop: Any) -> Dict[str, Any]:
    """The stored scan, restated as a claim about *this parcel*.

    One home for the slack arithmetic, for the two reasons
    `sea_distance_service.parcel_measurement` gives and a third of this
    block's own:

    * a row measured before it was re-geocoded carries a `slack_m` that no
      longer matches its accuracy, in either direction;
    * a row measured from a centroid must never print "1.1 km", because the
      parcel is somewhere in a 5 km disc around the point that was measured;
    * and the band is *per item*, so a facility 300 m from the centroid and
      one 5.9 km from it are two different claims about the parcel and have
      to be restated separately.

    A precise row gets `min == max == origin_distance_m` and comes out as the
    plain measurement it always was; nothing about that path is special-cased.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    stored = enrichment.get(ENRICHMENT_KEY)
    accuracy = normalize_accuracy(getattr(prop, "location_accuracy", None))
    slack = coordinate_slack_m(accuracy)
    base: Dict[str, Any] = {
        "origin_accuracy": accuracy,
        "slack_m": slack,
        "approximate_origin": bool(slack),
        "distance_basis": DISTANCE_BASIS,
        "source": SOURCE,
        "measured": False,
        "flagged": False,
        "items": [],
        "item_count": 0,
        "high_count": 0,
        "nearest": None,
        "searched_m": None,
        "guaranteed_m": None,
        "truncated": False,
        "updated_at": None,
        "last_attempt_status": None,
    }

    if not isinstance(stored, dict):
        return {**base, "status": STATUS_MISSING}

    # The restatement below re-applies the row's *current* slack to a distance
    # measured from a point recorded at scan time -- which is right only while
    # that is still where the listing is. `utils/refresh_property_accuracy.py`
    # and a `refresh=True` enrich both move the coordinate without touching
    # this block, and an approximate row upgraded to `precise` that way had
    # its centroid-measured distance printed as an exact one (review,
    # 2026-08-20). `origins_agree` answers None when either side is
    # unreadable, and None is not a move -- only an explicit disagreement is.
    if origins_agree(stored.get("origin"), origin_of(prop)) is False:
        return {**base, "status": STATUS_STALE_ORIGIN}

    status = stored.get("status")
    base["updated_at"] = stored.get("updated_at")
    base["last_attempt_status"] = stored.get("last_attempt_status")
    searched = _safe_float(stored.get("searched_m"))
    base["searched_m"] = searched
    # What the scan guarantees about the *parcel*: the radius it covered
    # around the stored point, less the distance the parcel may sit from it.
    # For an approximate row that is 1 km, and saying so is the difference
    # between "nothing near this plot" and "nothing near a village centre".
    base["guaranteed_m"] = None if searched is None else max(0.0, searched - slack)
    base["truncated"] = bool(stored.get("truncated"))

    if status not in MEASURED_STATUSES:
        return {**base, "status": status or STATUS_MISSING}

    items = []
    high = 0
    for stored_item in stored.get("items") or []:
        if not isinstance(stored_item, dict):
            continue
        measured = _safe_float(stored_item.get("origin_distance_m"))
        lower, upper = distance_bounds_m(measured, slack)
        item = {
            **stored_item,
            "origin_distance_m": measured,
            "distance_m": measured if not slack else None,
            "min_distance_m": lower,
            "max_distance_m": upper,
            "cardinal": cardinal(_safe_float(stored_item.get("bearing_deg"))),
        }
        if item.get("severity") == hazard_rules.SEVERITY_HIGH:
            high += 1
        items.append(item)

    # Nearest first, and an item whose distance did not survive the round trip
    # sorts last rather than first: an unreadable measurement must not become
    # the one the badge and the scorer answer from.
    items.sort(
        key=lambda item: (
            item.get("origin_distance_m")
            if item.get("origin_distance_m") is not None
            else float("inf")
        )
    )
    return {
        **base,
        "status": status,
        "measured": True,
        "flagged": bool(items),
        "items": items,
        "item_count": int(stored.get("item_count") or len(items)),
        "high_count": high,
        "nearest": items[0] if items else None,
    }


def measured_expression(model):
    """`read_verdict(...)["measured"]` as a SQL predicate over `model`.

    The coverage line beside the result count is drawn from this, and it has
    to agree with the badges under it row for row -- a header reading "40 of
    730 scanned" above a table whose badges say otherwise is a third wrong
    number rather than a disclosure. That is `listing_verification`'s rule,
    and `tests/test_hazard_proximity.py` runs one matrix through both readings
    for the same reason.

    Which is why the origin check is here too, awkward as it is in SQL: the
    moment `read_verdict` learned to answer `stale_origin`, a count that knew
    only about `status` started disagreeing with the badges it sits above.
    An unreadable origin on either side is *not* a move, exactly as
    `origins_agree` has it -- the two sides of one rule, in two languages.
    """
    block = model.enrichment[ENRICHMENT_KEY]
    status = block["status"].as_string()
    origin_lat = block["origin"]["lat"].as_float()
    origin_lon = block["origin"]["lon"].as_float()
    row_lat = cast(model.location_lat, Float)
    row_lon = cast(model.location_lon, Float)
    origin_unknown = or_(
        origin_lat.is_(None),
        origin_lon.is_(None),
        model.location_lat.is_(None),
        model.location_lon.is_(None),
    )
    origin_matches = and_(
        func.abs(origin_lat - row_lat) <= ORIGIN_TOLERANCE_DEG,
        func.abs(origin_lon - row_lon) <= ORIGIN_TOLERANCE_DEG,
    )
    return and_(
        or_(*[status == value for value in MEASURED_STATUSES]),
        or_(origin_unknown, origin_matches),
    )


def needs_hazards(prop: Any) -> bool:
    """Is this row still worth scanning?

    The backfill's scope, and what makes `resumable=True` an honest claim on
    it: a row that answered leaves the scope, so a killed run repeats one
    property at most.
    """
    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
    stored = enrichment.get(ENRICHMENT_KEY)
    if not isinstance(stored, dict):
        return True
    if stored.get("status") in RETRYABLE_STATUSES:
        return True
    # A block measured from a point the listing has since moved away from is
    # not an answer about this listing, and the surfaces already refuse to
    # read it as one. Re-scanning is one free Overpass query.
    return origins_agree(stored.get("origin"), origin_of(prop)) is False
