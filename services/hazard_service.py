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

**Keep only what a silence cannot contradict.** A stored measurement survives a
re-run on exactly one condition: the subject is unchanged *and* the new run
learned nothing, because the source did not answer. `unavailable` is that, and
it is the whole of it. A subject that moved, that lost the precision the stored
claim rested on, or that disappeared is not: the stored answer is then about
somewhere else, and this row's honest answer is that it has none.

That is the rule `services/sea_view_service.py` and
`services/sea_distance_service.py` already apply, and until 2026-08-26 this
module applied the opposite one to a coordinate that goes NULL -- it kept the
measurement, on the grounds that `no_coordinates` is not retryable and
overwriting would take the row out of the backfill's scope for good. Both
halves of that were measured and neither survived. `needs_hazards` reads
`read_verdict`, not the stored status, so a row stored `no_coordinates` is in
scope either way; and the kept block is invisible on every surface, because
`read_verdict` refuses to assert a measurement whose origin the row no longer
has. Keeping bought one free Overpass query, in the one case where the
identical coordinate comes back -- and cost one wrong number, since
`complete_expression` counts a row the app cannot locate as carrying a
complete scan.

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

from sqlalchemy import and_, func, or_
from sqlalchemy.orm.attributes import flag_modified

from services import hazard_rules
from services.coordinate_quality import (
    coordinate_slack_m,
    distance_bounds_m,
    normalize_accuracy,
)
from services.enrichment_origin import origin_of, origins_agree
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

# Every state this reader is allowed to answer with. A stored status outside
# it is normalised to `missing_hazards`: the surfaces branch on this set, and
# a sixth value nobody wrote a branch for renders as silence, which is the one
# thing this feature may not do.
KNOWN_STATUSES = (
    STATUS_OK,
    STATUS_NONE,
    STATUS_UNAVAILABLE,
    STATUS_NO_COORDINATES,
    STATUS_MISSING,
    STATUS_STALE_ORIGIN,
)

# A status the data can be trusted for. It survives a later refusal as
# last-known-good, for the reason `sea_distance_service` keeps its own: a
# cement works does not move, and replacing a measurement with "the network was
# down" loses the only thing anybody looked up.
#
# It survives a *refusal*, and nothing else. A row whose coordinate has gone is
# not a row whose scan refused, and the rule in this module's docstring is what
# separates them. There was a `RETRYABLE_STATUSES` here saying `no_coordinates`
# was not retryable; nothing read it, and `needs_hazards` -- the predicate that
# really answers "is this row still worth scanning" -- disagreed with it, so it
# is gone rather than left to justify a rule it never governed.
MEASURED_STATUSES = (STATUS_OK, STATUS_NONE)

SOURCE = "openstreetmap"
DISTANCE_BASIS = "straight_line"

# A month, like the preset candidates: this is a claim about the ground, and
# the expensive part of a bulk run is the 5 s Overpass gate in front of it.
_CACHE_TTL_S = 60 * 60 * 24 * 30
# The cache holds the *elements*, not the verdict, so changing the rules table
# does not need a bump here -- only changing the query does.
_CACHE_KEY = "hazard_scan_v1"

# `utils/cache.py` keys an enrichment entry on the coordinate rounded to four
# decimals, so an answer fetched for one point can serve any point in an
# ~11 m cell. Everything inside that cell is within the cell's diagonal of the
# point the query was centred on, so the radius this scan is guaranteed for
# *here* is the query radius less that diagonal. It is 16 m against 6 km and
# it changes no verdict -- and saying `searched_m: 6000` when the query was
# centred somewhere else is still a claim nobody measured, which is the
# `sea_view_service` cell rule (its own radius already carries the same
# subtraction) arriving one module over.
_CACHE_CELL_SLACK_M = 16.0

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
    # `OverflowError` as well as the two obvious ones: OSM is user-edited and
    # `float(10 ** 400)` raises it rather than returning `inf` (review,
    # 2026-08-20). It escaped this function, was caught by the pass above, and
    # left the row reading "not scanned yet" -- which is the honest words for
    # the wrong fact, since the scan did run and one element was unreadable.
    # Returning None puts that element on the `unreadable` count, where an
    # incomplete answer is already refused the cache and stays in the
    # backfill's scope.
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(number) or abs(number) > limit:
        return None
    return number


def _safe_float(value: Any) -> Optional[float]:
    """A finite number, or None.

    `math.isfinite` and not `isnan`: a stored `"Infinity"` parses, and it made
    `guaranteed_m` infinite, which cleared every horizon the scorer checks and
    scored a listing 100 (codex review, 2026-08-20).
    """
    if isinstance(value, bool):
        # `True` is 1.0 to `float()`, and a distance of one metre is not what
        # a stored `true` means.
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        # `OverflowError`: a JSON integer has no width limit, and a 310-digit
        # one raises rather than parsing (codex review, 2026-08-20).
        return None
    return number if math.isfinite(number) else None


# The spellings a stored `truncated` may arrive in that mean "not truncated".
# Everything else -- `true`, `1`, an object, a string nobody expected -- reads
# as truncated, which is the fail-closed direction: a scan wrongly called
# short costs a disclosure, a scan wrongly called complete is the defect this
# whole feature exists to remove. Both languages read the same list, because
# a JSON boolean does not render the same on both backends: PostgreSQL's `->>`
# gives `false` and SQLite's `json_extract` gives `0`.
_NOT_TRUNCATED_TEXT = ("", "false", "0", "none", "null")


def _is_truncated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    # `strip(" ")` and not a bare `strip()`: SQL's `trim` removes spaces and
    # nothing else, so stripping tabs and newlines here would make `"\tfalse"`
    # complete in one language and truncated in the other (codex review,
    # 2026-08-20). A value carrying a tab is not one of the spellings this
    # writer produces, and the fail-closed reading of an unrecognised one is
    # that the scan was short.
    return str(value).strip(" ").casefold() not in _NOT_TRUNCATED_TEXT


def _safe_int(value: Any) -> Optional[int]:
    """A plain non-negative integer, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _as_list(value: Any) -> Optional[list]:
    """The value if it really is a list, else None.

    `value or []` is not this: it turns a stored `1` into `1` and a `{}` into
    `[]`, so the template iterated an integer and raised, and a block whose
    items were an object read as a clean neighbourhood (codex review,
    2026-08-20).
    """
    return value if isinstance(value, list) else None


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
    unreadable = 0
    for element in elements or []:
        if not isinstance(element, dict) or not isinstance(element.get("tags"), dict):
            # A 200 carrying `[42]`, or an element with `tags: null`, used to
            # be dropped here and the scan then reported `none_within_radius`
            # with `truncated: False` -- a clean neighbourhood built out of a
            # response nobody could read (codex review, 2026-08-20). Counted
            # with the unplaced ones instead.
            unreadable += 1
            continue
        # An element with no readable centre is kept, with no point. Dropping
        # it here would be silent: `out center` normally gives every way and
        # relation one, but a relation whose geometry does not resolve arrives
        # without, and if that element is a hazard the scan has missed
        # something and has to say so rather than answering as if it had not
        # (codex review, 2026-08-20). What to do about it is `measure`'s, which
        # is where the rules live.
        point = _element_point(element)
        trimmed.append(
            {
                "type": element.get("type"),
                "id": element.get("id"),
                "lat": None if point is None else point[0],
                "lon": None if point is None else point[1],
                "tags": element["tags"],
            }
        )
    # `returned` is the count Overpass *answered with*, not the count that
    # survived the trim above -- an element with no readable centre is dropped
    # here, and counting after that would let a scan that hit the server-side
    # cap read as one that did not. The cap is the only way this feature can
    # quietly show a short list, so the number that detects it has to be the
    # raw one, and it has to survive into the cache.
    payload = {
        "elements": trimmed,
        "returned": len(elements or []),
        "unreadable": unreadable,
    }
    # An incomplete answer is not cached, for the reason a refusal is not: it
    # would be handed back for a month, and the retry the block asks for --
    # the row is in the backfill's scope precisely *because* the scan came
    # back short -- would read the same partial entry and never reach a
    # transport that has since recovered (codex review, 2026-08-20).
    incomplete = payload["returned"] >= hazard_rules.ELEMENT_LIMIT or unreadable
    if not incomplete:
        try:
            cache_enrichment_data(lat, lon, _CACHE_KEY, payload, timeout=_CACHE_TTL_S)
        except Exception:
            logger.warning(
                "Could not cache hazard scan for %s,%s", lat, lon, exc_info=True
            )
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
        unreadable = int(answer.get("unreadable") or 0)

        qualifying: List[Dict[str, Any]] = []
        unplaced = 0
        for element in elements:
            verdict = hazard_rules.classify(element.get("tags"))
            if verdict is None:
                continue
            if element.get("lat") is None or element.get("lon") is None:
                # A hazard OSM could not put on the map. It cannot be measured
                # and it cannot be ignored, so it is counted: the block reports
                # an incomplete scan and the scorer abstains, exactly as it
                # does when the element cap was reached.
                unplaced += 1
                continue
            distance = haversine_m(lat, lon, element["lat"], element["lon"])
            if distance > hazard_rules.SEARCH_RADIUS_M - _CACHE_CELL_SLACK_M:
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
            "searched_m": hazard_rules.SEARCH_RADIUS_M - _CACHE_CELL_SLACK_M,
            "truncated": truncated or bool(unplaced) or bool(unreadable),
            "unplaced": unplaced,
            "unreadable": unreadable,
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
        operators: set = set()
        for candidate in qualifying:
            key = hazard_rules.facility_key(candidate["tags"])
            operator = hazard_rules.operator_key(candidate["tags"])
            if operator:
                operators.add(operator)
            if key:
                keyed.setdefault(key, []).append(candidate)
            else:
                keyless.append(candidate)

        # Only an operator may absorb another key: a generic *name* swallowing
        # a specific one reported two quarries as one.
        canonical = hazard_rules.merge_keys(keyed.keys(), absorbing=operators)
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
            # A row can lose its coordinate: `refresh=True` clears it before
            # geocoding and a refusal leaves it cleared (#393), and
            # `enrich_property` then runs the free pass on a row with no point
            # at all. The stored measurement does not survive that, and this
            # branch used to be the one place in the family where it did.
            #
            # It is not a refusal. Overpass did not go quiet -- the *subject*
            # went away, and a scan of a point nothing connects to this listing
            # any more is not a measurement of it. The two things the keep was
            # written to protect were both measured on 2026-08-26 and neither
            # is real: the row stays in the backfill's scope either way, since
            # `needs_hazards` reads `read_verdict` and a kept block reads
            # `measured=False`; and no surface shows the kept measurement,
            # since `read_verdict` refuses to assert it. What it did buy was
            # `complete_expression` counting a row the app cannot locate as
            # carrying a complete scan, and one saved free Overpass query in
            # the single case where the identical coordinate returns.
            #
            # `previous` is deliberately not read at all. There is no decision
            # left to make from it, and reading it would invite the keep back.
            payload = {
                "status": STATUS_NO_COORDINATES,
                "source": SOURCE,
                "distance_basis": DISTANCE_BASIS,
                "updated_at": now_iso,
                "last_attempt_status": STATUS_NO_COORDINATES,
                "last_attempt_at": now_iso,
            }
            with locked_write(prop, locked=locked, commit=commit):
                self._store(prop, payload)
            return payload

        measurement = self.measure(lat, lon)

        with locked_write(prop, locked=locked, commit=commit):
            previous = self._stored(prop)
            # Re-read under the lock. The row can move while the network call
            # is in flight -- codex reproduced it: A measures from origin A, B
            # moves the row and stores a B-origin measurement, and A then
            # refreshes under the lock and writes its stale result over B's
            # good one. Readers call the result `stale_origin`, so nothing
            # wrong is *shown*, but a measurement of where the listing
            # actually is was lost, and only a re-scan brings it back.
            measured_where_it_is = (
                origins_agree({"lat": lat, "lon": lon}, origin_of(prop)) is not False
            )
            if not measured_where_it_is and previous.get("status") in MEASURED_STATUSES:
                payload = {
                    **previous,
                    "last_attempt_status": STATUS_STALE_ORIGIN,
                    "last_attempt_at": now_iso,
                }
                self._store(prop, payload)
                return payload

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


def _unreadable(base: Dict[str, Any]) -> Dict[str, Any]:
    """A block this reader had to reject, in the shape every surface reads.

    `complete` goes with it. It used to be left as stored, on the grounds that
    the SQL predicate cannot see an item's shape and the two readings must not
    diverge -- but the visible surfaces then disagreed with each other, which
    is worse: the card said "not scanned yet" while the export beside it said
    the scan was complete (codex review, 2026-08-20). The card, the badge and
    the CSV all read this verdict and now agree; the coverage count is the one
    number that cannot see it, and it over-counts by one for a row somebody
    hand-edited, which is the direction that discloses rather than hides.
    """
    return {**base, "status": STATUS_MISSING, "complete": False}


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
        # A scan that reached Overpass's element cap, or that saw a hazard OSM
        # could not place, is a short list rather than an answer. `measured`
        # stays true for it -- something *was* looked at, and the items it did
        # find are real -- but nothing may count it as a completed scan.
        "complete": False,
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
        return _unreadable(base)

    # `complete` is a fact about the *scan* -- did the query see everything it
    # asked for -- and `measured` is a fact about *this point*. They are two
    # different questions and they get two different answers: a block taken
    # before the listing was re-located is a complete scan of somewhere else.
    # Keeping them apart is what lets the coverage line be answerable in SQL
    # without casting untrusted JSON to a number, which on PostgreSQL raises
    # `invalid input syntax` and takes the whole page down for one malformed
    # row (codex review, 2026-08-20).
    status = stored.get("status")
    base["truncated"] = _is_truncated(stored.get("truncated"))
    base["complete"] = status in MEASURED_STATUSES and not base["truncated"]

    # The restatement below re-applies the row's *current* slack to a distance
    # measured from a point recorded at scan time -- which is right only while
    # that is still where the listing is. `utils/refresh_property_accuracy.py`
    # and a `refresh=True` enrich both move the coordinate without touching
    # this block, and an approximate row upgraded to `precise` that way had
    # its centroid-measured distance printed as an exact one (review,
    # 2026-08-20).
    #
    # A block that *records* an origin therefore has to match the row's own
    # coordinate to be read at all -- including when the row now has none.
    # `enrich` no longer *writes* a measured block onto a coordinate-less row
    # (see its `lat is None` branch), and this reader does not lean on that:
    # the shape still arrives from a row whose coordinate was cleared with no
    # enrich run since, from the stale-origin race below, and from the direct
    # SQL that is a supported workflow here. A reader that refuses it only
    # because the current writer cannot produce it is a reader that trusts the
    # writer, which is the opposite of the fail-closed contract this function
    # is under. An unreadable stored origin is the only "cannot tell", and it
    # restates.
    base["updated_at"] = stored.get("updated_at")
    base["last_attempt_status"] = stored.get("last_attempt_status")

    # A status that is not a measurement is answered *first*, before anything
    # asks about an origin -- the writer's own `no_coordinates` block cannot
    # carry one, and asking made its intended branch unreachable: the card
    # said "the listing has been re-located since the scan" for a row that has
    # never had a coordinate at all (codex review, 2026-08-20). An unknown
    # status is normalised rather than echoed: this reader answers a fixed set
    # of states and inventing a sixth for whatever a future writer stored is
    # how one reaches a template that has no branch for it.
    if status not in MEASURED_STATUSES:
        known = status if status in KNOWN_STATUSES else STATUS_MISSING
        if known == STATUS_NO_COORDINATES and origin_of(prop) is not None:
            # The row has one now. Saying it has none is a claim about today
            # made from a block written before the geocoder answered (codex
            # review, 2026-08-20), and `needs_hazards` already puts such a row
            # back in the backfill's scope.
            known = STATUS_MISSING
        return {**base, "status": known}

    # And a measured block has to say where it was measured from. Treating an
    # unreadable origin as "cannot tell" let a moved precise row keep
    # asserting its old distances (codex review, 2026-08-20).
    #
    # The two ways that can fail are different facts and get different words.
    # A row that *lost* its coordinate has not been re-located -- it is
    # nowhere, and saying "the listing has been re-located since the scan"
    # there put this card in direct contradiction with the travel card beside
    # it, which correctly says the listing has no coordinate (found in review,
    # 2026-08-20).
    current_origin = origin_of(prop)
    if current_origin is None:
        return {**base, "status": STATUS_NO_COORDINATES}
    if origins_agree(stored.get("origin"), current_origin) is not True:
        return {**base, "status": STATUS_STALE_ORIGIN}

    # What the scan guarantees about the *parcel*: the radius it covered
    # around the stored point, less the distance the parcel may sit from it.
    # For an approximate row that is 1 km, and saying so is the difference
    # between "nothing near this plot" and "nothing near a village centre".
    # A measurement nobody can read is not a radius, and a block claiming one
    # it cannot support is not a measurement at all.
    searched = _safe_float(stored.get("searched_m"))
    if searched is None or searched <= 0 or searched > hazard_rules.SEARCH_RADIUS_M:
        # Bounded above as well as below: the writer can only ever store
        # `SEARCH_RADIUS_M` less the cache cell, and a stored `1e300` cleared
        # every horizon the scorer checks and turned an empty scan into a
        # clean 100 (codex review, 2026-08-20).
        return _unreadable(base)
    base["searched_m"] = searched
    base["guaranteed_m"] = max(0.0, searched - slack)

    items = []
    high = 0
    stored_items = _as_list(stored.get("items"))
    counted = stored.get("item_count")
    if (
        stored_items is None
        or not isinstance(counted, int)
        or isinstance(counted, bool)
        or counted < 0
        or counted > hazard_rules.ELEMENT_LIMIT
    ):
        return _unreadable(base)
    # The writer stores every facility it found, up to `MAX_ITEMS`. So there
    # are exactly two shapes it can produce, and anything else is a block
    # nobody can read: `item_count == len(items)`, or a count past the cap
    # with the cap's worth stored. `item_count=25` beside a single item is
    # neither, and it scored 100 while 24 facilities -- any of which could
    # have been a high-severity one next door -- were unaccounted for (codex
    # review, 2026-08-20).
    if counted != len(stored_items) and not (
        counted > MAX_ITEMS and len(stored_items) == MAX_ITEMS
    ):
        return _unreadable(base)
    if status == STATUS_NONE and (counted or stored_items):
        return _unreadable(base)
    # `ok` means something qualified, so the block holds at least one item:
    # the writer stores the nearest `MAX_ITEMS` and `MAX_ITEMS` is not zero.
    # A count of one over an empty list rendered "Nothing recognised" beside
    # an exported facility count of 1 (codex review, 2026-08-20).
    if status == STATUS_OK and (not counted or not stored_items):
        return _unreadable(base)
    for stored_item in stored_items:
        if not isinstance(stored_item, dict):
            # A stored shape nobody can read is not an item that can be walked
            # past: the ones beside it would then be presented as the whole
            # list, with a clean badge and a 100 from the scorer (codex
            # review, 2026-08-20). Nothing this writer produces looks like
            # that, so the block is a hand-edit or a shape from some future
            # version, and the honest reading of a block nobody can read is
            # that nobody has read it.
            return _unreadable(base)
        # An `ok` block's items are measurements, and each one has to be a
        # finite distance and one of the two severities. A missing distance
        # rendered a facility with no distance beside it and left the row out
        # of the backfill's reach; an unknown severity scored 50 where `high`
        # scores 0 (codex review, 2026-08-20).
        measured = _safe_float(stored_item.get("origin_distance_m"))
        if measured is None or measured < 0 or measured > searched:
            # Past the radius the block itself claims to have covered. The
            # writer filters on exactly that, so a 10 km item beside a 6 km
            # scan is a shape it cannot produce -- and it rendered "10.0 km"
            # under "Scanned 6.0 km" and scored 100 (codex review,
            # 2026-08-20).
            return _unreadable(base)
        if stored_item.get("severity") not in (
            hazard_rules.SEVERITY_HIGH,
            hazard_rules.SEVERITY_MODERATE,
        ):
            return _unreadable(base)
        lower, upper = distance_bounds_m(measured, slack)
        item = {
            **stored_item,
            # A stored `null` where a list belongs is not the same as a
            # missing key: Jinja iterates an undefined and raises on a None,
            # and `routes/main_routes.py` turns that into a flash and an empty
            # page rather than an error anyone sees (codex review,
            # 2026-08-20). Normalised here, once, for all three surfaces.
            "kinds": _as_list(stored_item.get("kinds")) or [],
            # Only elements a template can turn into a link: a `type` that is
            # not a string, or an id that is not an int, raised in the loop
            # and became a hidden redirect (codex review, 2026-08-20).
            "elements": [
                element
                for element in _as_list(stored_item.get("elements")) or []
                if isinstance(element, dict)
                and isinstance(element.get("type"), str)
                and isinstance(element.get("id"), int)
                and not isinstance(element.get("id"), bool)
            ],
            "evidence": _as_list(stored_item.get("evidence")) or [],
            "names": _as_list(stored_item.get("names")) or [],
            # A count the page compares against a number. `{"x": 1}` raised.
            "element_count": _safe_int(stored_item.get("element_count")),
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
    # `item_count` is what the scan found; `items` is what it stored, capped
    # at `MAX_ITEMS`. Fewer stored than counted is the ordinary cap and is
    # handled by the scorer's own bound; *more* counted than were readable
    # here means something was dropped above, and that is not a complete
    # answer either.
    return {
        **base,
        "status": status,
        "measured": True,
        "flagged": bool(items),
        "items": items,
        "item_count": counted,
        "high_count": high,
        "nearest": items[0] if items else None,
    }


def complete_expression(model):
    """`read_verdict(...)["complete"]` as a SQL predicate over `model`.

    "Carries a complete scan" -- the same question in both languages, and
    deliberately *not* "is about this coordinate". Those are two facts and
    `read_verdict` keeps them apart for a reason that only shows up here:
    answering the second in SQL means casting a stored coordinate to a number,
    and on PostgreSQL `(… ->> 'lat')::float` on a hand-edited `"junk"` raises
    `invalid input syntax` and takes the entire `/properties` coverage query
    down with it (codex review, 2026-08-20). One malformed row must not be
    able to remove the page.

    So nothing here is cast. The truncation flag is read as text and compared
    against the same `_NOT_TRUNCATED_TEXT` list the Python side uses -- which
    is what makes the two agree across a JSON boolean that PostgreSQL renders
    `false` and SQLite renders `0` -- and everything unrecognised counts as
    truncated, the fail-closed direction.

    The line this feeds says "fully scanned", which is exactly what it counts.
    A row whose coordinate has moved since the scan still carries a complete
    scan; that it is no longer about this point is what the card says, and the
    badge above it stays silent either way, so no two numbers on that line
    disagree.
    """
    block = model.enrichment[ENRICHMENT_KEY]
    status = block["status"].as_string()
    # `trim` as well as `lower`: the Python side strips, and `" false "`
    # was complete there and truncated here (codex review, 2026-08-20).
    truncated = func.trim(func.lower(block["truncated"].as_string()))
    return and_(
        or_(*[status == value for value in MEASURED_STATUSES]),
        or_(truncated.is_(None), truncated.in_(_NOT_TRUNCATED_TEXT)),
    )


def needs_hazards(prop: Any) -> bool:
    """Is this row still worth scanning?

    The backfill's scope, and what makes `resumable=True` an honest claim on
    it: a row that answered leaves the scope, so a killed run repeats one
    property at most.
    """
    verdict = read_verdict(prop)
    # Read through the same verdict the surfaces do, rather than off the raw
    # status. Three rows fell out of scope for good by reading the column
    # directly (codex review, 2026-08-20): a truncated scan, which a re-run
    # may well complete; a block nobody can read, which is not an answer at
    # all; and a row stored `no_coordinates` that has since *gained* one,
    # where `origins_agree` says "cannot tell" rather than "moved". A scan is
    # one free Overpass query, so the scope is simply everything that does not
    # currently hold a complete measurement of where this listing is.
    return not (verdict["measured"] and verdict["complete"])


# The old name, kept so nothing outside this module has to know that the
# question it asks is `complete` and never `measured`. It answers what it
# always answered; the name was the thing that was wrong.
measured_expression = complete_expression
