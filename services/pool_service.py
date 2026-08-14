"""Swimming-pool discovery and measurement (agreed proposal D17, Phase 2).

The owner swims daily, so this is the one Phase-2 datum that may move a
score — through the `pool_score` criterion in
services/property_scoring_service.py, which ships at weight 0 and is enabled
by hand. This service only produces the data, under the rules hard-negotiated
in the proposal review:

* the component scores ONLY a measured drive time to a qualifying pool;
* absence is never auto-zeroed: OSM finding nothing triggers ONE budgeted
  Places Text Search cross-check, and whatever it says the status becomes
  `unverified_absence` (component None) — a true 0 needs the per-property
  owner flag (`owner_no_pool`, the hand-set sea-view precedent);
* indoor is *evidence with a source*, never certainty: `covered=yes` is
  `verified`, a building or a "climatizada" name is `likely`, silence is
  `unknown` — and the require-indoor toggle is applied by the scorer, not
  here, so the evidence survives a config change;
* a refusal never overwrites measured candidates (QoL/sea precedent: the
  old answer stays, the failed attempt is stamped);
* Overpass rides the shared transport in services/enrichment_service.py —
  the gate, the UA and the #144 refusal triad live exactly once.

Verified live (2026-08-13, both coasts): `sports_centre` + `sport~swimming`
finds the real municipal pools — 6 named around El Franco (Ribadeo indoor),
12 around San Sadurniño (As Pontes indoor heated) — and hotel/garden noise
stays out via the access/name rules below.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

from models import Property
from services.enrichment_service import EnrichmentService
from services.sea_view_service import haversine_m

logger = logging.getLogger(__name__)

POOL_SEARCH_RADIUS_M = 25000
# Enough measured options for the require-indoor toggle to still have a
# candidate left; also the Distance Matrix element cap per property.
POOL_MEASURE_TOP_N = 3

STATUS_OK = "ok"  # at least one candidate with a measured drive time
STATUS_PENDING = "pending_measurement"  # candidates found, Distance Matrix refused
STATUS_UNVERIFIED_ABSENCE = "unverified_absence"  # OSM + cross-check found nothing
STATUS_UNAVAILABLE = "unavailable"  # the lookup itself refused
STATUS_NO_COORDINATES = "no_coordinates"

# Statuses that carry an answer worth keeping when a later attempt refuses.
MEASURED_STATUSES = frozenset({STATUS_OK, STATUS_UNVERIFIED_ABSENCE})

CROSS_CHECK_QUERY = "piscina municipal climatizada"


def _indoor_evidence(tags: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """(indoor_status, evidence) — evidence names the tag or word that says so."""
    covered = str(tags.get("covered") or "").lower()
    if covered == "yes":
        return "verified", "covered=yes"
    name = str(tags.get("name") or "")
    lowered = name.lower()
    for word in ("climatizada", "climatizado", "cubierta", "cubierto"):
        if word in lowered:
            return "likely", f"name contains '{word}'"
    building = str(tags.get("building") or "").lower()
    if building and building not in ("no",):
        return "likely", f"building={building}"
    location = str(tags.get("location") or "").lower()
    if location == "indoor":
        return "verified", "location=indoor"
    return "unknown", None


def _qualifies(tags: Dict[str, Any]) -> bool:
    """A sports facility, or a *named public* pool — never a garden basin.

    `sports_centre` + swimming qualifies on its own. A bare `swimming_pool`
    needs a name (the live sweep showed unnamed ones are gardens and hotel
    basins) and must not be access=private/customers.
    """
    access = str(tags.get("access") or "").lower()
    if access in ("private", "customers"):
        return False
    leisure = str(tags.get("leisure") or "").lower()
    sport = str(tags.get("sport") or "").lower()
    if leisure == "sports_centre" and "swimming" in sport:
        return True
    if leisure == "swimming_pool" and str(tags.get("name") or "").strip():
        return True
    return False


class PoolService:
    """Finds qualifying pools, measures drive times, stores enrichment.pool."""

    def __init__(
        self,
        enrichment_service: Optional[EnrichmentService] = None,
        travel_service=None,
    ):
        self.enrichment_service = enrichment_service or EnrichmentService()
        # Lazy import avoids a cycle: property_travel_service imports nothing
        # from here, but keeping the default late makes the dependency
        # injectable for tests without paying the import at module load.
        if travel_service is None:
            from services.property_travel_service import PropertyTravelService

            travel_service = PropertyTravelService()
        self.travel_service = travel_service

    # -- discovery ---------------------------------------------------------

    def discover_candidates(self, lat: float, lon: float) -> Dict[str, Any]:
        """Qualifying pool candidates nearest-first, or why the lookup refused.

        Returns {"candidates": [...]} or {"failure_reason": str}.
        """
        around = f"around:{POOL_SEARCH_RADIUS_M},{lat},{lon}"
        query = f"""
        [out:json][timeout:25];
        (
          nwr["leisure"="sports_centre"]["sport"~"swimming"]({around});
          nwr["leisure"="swimming_pool"]["access"!~"private|customers"]({around});
        );
        out center tags 60;
        """
        elements, failure = self.enrichment_service._overpass_elements(query)
        if failure is not None:
            return {"failure_reason": getattr(failure, "reason", "unavailable")}

        candidates: List[Dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags") or {}
            if not _qualifies(tags):
                continue
            el_lat = element.get("lat")
            el_lon = element.get("lon")
            if el_lat is None or el_lon is None:
                center = element.get("center") or {}
                el_lat, el_lon = center.get("lat"), center.get("lon")
            if el_lat is None or el_lon is None:
                continue
            indoor_status, indoor_evidence = _indoor_evidence(tags)
            candidates.append(
                {
                    "name": tags.get("name"),
                    "lat": el_lat,
                    "lon": el_lon,
                    "indoor_status": indoor_status,
                    "indoor_evidence": indoor_evidence,
                    "straight_km": round(
                        haversine_m(lat, lon, float(el_lat), float(el_lon)) / 1000.0,
                        1,
                    ),
                }
            )
        candidates.sort(key=lambda c: c["straight_km"])
        return {"candidates": candidates}

    def _cross_check(self, lat: float, lon: float) -> Dict[str, Any]:
        """One budgeted Places Text Search before an absence may be recorded.

        Whatever it answers, the status stays `unverified_absence` unless it
        actually finds a pool to measure — a single query proves nothing
        about completeness (review round 3). Its outcome is recorded so the
        card can say the check ran.
        """
        try:
            lookup = self.travel_service._nearest_place_text_search(
                lat, lon, CROSS_CHECK_QUERY, place_types=[]
            )
        except Exception:
            logger.warning("Pool cross-check failed", exc_info=True)
            return {"ran": True, "outcome": "unavailable"}
        place = getattr(lookup, "place", None)
        if isinstance(place, dict) and place.get("lat") is not None:
            return {
                "ran": True,
                "outcome": "found",
                "candidate": {
                    "name": place.get("name"),
                    "lat": place.get("lat"),
                    "lon": place.get("lon"),
                    "indoor_status": "unknown",
                    "indoor_evidence": None,
                    "straight_km": round(
                        haversine_m(lat, lon, float(place["lat"]), float(place["lon"]))
                        / 1000.0,
                        1,
                    ),
                    "source": "places_text_search",
                },
            }
        failure = getattr(lookup, "failure", None)
        return {"ran": True, "outcome": "refused" if failure else "empty"}

    # -- measurement + storage --------------------------------------------

    def enrich(self, prop: Property, commit: bool = False) -> Dict[str, Any]:
        part = self._compute(prop)

        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        previous = enrichment.get("pool")
        previous = previous if isinstance(previous, dict) else {}
        now_iso = datetime.now(timezone.utc).isoformat()

        # A refusal never overwrites an answer (sea/QoL precedent). The
        # owner's hand-set flag lives inside the block and must survive
        # every recompute, whatever the new status is.
        if (
            part.get("status") in (STATUS_UNAVAILABLE, STATUS_PENDING)
            and previous.get("status") in MEASURED_STATUSES
        ):
            kept = dict(previous)
            kept["last_attempt_status"] = part.get("status")
            kept["last_attempt_at"] = now_iso
            part = kept
        if isinstance(previous.get("owner_no_pool"), dict):
            part["owner_no_pool"] = previous["owner_no_pool"]

        part["updated_at"] = now_iso
        enrichment["pool"] = part
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")
        if commit:
            from app import db

            db.session.commit()
        return part

    def _compute(self, prop: Property) -> Dict[str, Any]:
        if prop.location_lat is None or prop.location_lon is None:
            return {"status": STATUS_NO_COORDINATES}
        lat, lon = float(prop.location_lat), float(prop.location_lon)

        discovery = self.discover_candidates(lat, lon)
        if "failure_reason" in discovery:
            return {
                "status": STATUS_UNAVAILABLE,
                "reason": discovery["failure_reason"],
            }

        candidates = discovery["candidates"]
        cross_check: Dict[str, Any] = {"ran": False}
        if not candidates:
            cross_check = self._cross_check(lat, lon)
            if cross_check.get("outcome") == "found":
                candidates = [cross_check.pop("candidate")]
            else:
                return {
                    "status": STATUS_UNVERIFIED_ABSENCE,
                    "search_radius_m": POOL_SEARCH_RADIUS_M,
                    "cross_check": cross_check,
                }

        to_measure = candidates[:POOL_MEASURE_TOP_N]
        minutes = self.travel_service.measure_drive_minutes(
            lat, lon, [(c["lat"], c["lon"]) for c in to_measure]
        )
        measured_any = False
        for candidate, drive in zip(to_measure, minutes):
            if drive is not None:
                candidate["drive_min"] = drive
                measured_any = True

        return {
            "status": STATUS_OK if measured_any else STATUS_PENDING,
            "search_radius_m": POOL_SEARCH_RADIUS_M,
            "candidates": to_measure,
            "unmeasured_further": max(0, len(candidates) - len(to_measure)),
            "cross_check": cross_check,
        }
