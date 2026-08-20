"""Drive times from a routing engine on this machine instead of Distance Matrix.

Step 3 of the cost plan. With the presets and the beaches on OpenStreetMap,
the Distance Matrix leg is the only billed call an enrichment still makes --
~26 elements, about $0.13 a listing -- and OSRM answers the same question from
the same map, locally, for nothing.

**Measured against the durations already in the database** (30 random target
pairs from production, precise origins, all previously answered by Google):

* the **distances agree** -- 49.0 vs 49.3 km, 64.2 vs 65.9, 75.4 vs 77.2,
  10.3 vs 10.4 -- so the two engines are choosing the same roads;
* the median duration difference is **-1.3%** and the mean **-5.9%**;
* the outliers have structure rather than being noise. Under five minutes
  Google rounds to whole minutes and OSRM does not (0.8 min against "2 min" is
  -59% and no disagreement at all), and on **motorway runs of 30-75 km OSRM is
  consistently slower: +26% to +34%** on five airport measurements. That is
  the car profile's speed assumptions, not traffic -- nothing here or in the
  Google calls it replaces ever asked for a departure time.

That bias is why this module is **opt-in** (`OSRM_URL` unset means Google
answers, exactly as before) and why turning it on is a decision about what the
stored numbers mean rather than a cost optimisation: a table holding Google
minutes for old rows and OSRM minutes for new ones compares two things.

Two rules it keeps from everything around it. A routing engine that cannot be
reached is a **refusal**, never a zero and never a silent fall back to the paid
API -- falling back would spend exactly when the free path is down, which is
the decision `services/osm_places.py` and `services/reference_places.py`
already made. And a **mode this engine was not built for is refused rather
than answered by the car profile**: the extract carries `car.lua` alone, so a
walking preset would otherwise be told a driving time. Production has only ever
stored `driving` -- 4332 targets, no other mode -- so that refusal costs
nothing today and prevents a wrong number tomorrow.
"""

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

import requests

from config import Config
from utils.google_api import (
    REASON_HTTP_ERROR,
    REASON_MALFORMED_RESPONSE,
    GoogleApiFailure,
    failure_from_exception,
)
from utils.http import HTTP_USER_AGENT, request_with_retries

logger = logging.getLogger(__name__)

# The profiles this deployment's extract was built with. `osrm-extract -p
# /opt/car.lua` produces one, so one is what may be answered.
_PROFILE_FOR_MODE = {"driving": "driving"}

REASON_MODE_UNSUPPORTED = "osrm_mode_unsupported"
REASON_NO_ROUTE = "osrm_no_route"


@dataclass
class RouteLeg:
    """One origin-to-destination answer, or the absence of a route.

    `distance_m is None and duration_s is None` with no failure means the
    engine answered and there is no road route -- an island, a pedestrian
    zone -- which is a measurement. A failure means nobody answered.
    """

    distance_m: Optional[int] = None
    duration_s: Optional[int] = None


def osrm_url() -> str:
    """Where the routing engine is, or "" when this deployment has none."""
    return str(getattr(Config, "OSRM_URL", "") or "").rstrip("/")


def is_enabled() -> bool:
    return bool(osrm_url())


def table(
    origin: Tuple[float, float],
    destinations: Sequence[Tuple[float, float]],
    mode: str = "driving",
) -> Tuple[Optional[List[RouteLeg]], Optional[GoogleApiFailure]]:
    """Durations and distances from one origin to many destinations.

    One request for the whole batch, which is what OSRM's `/table` is for --
    and, unlike the Distance Matrix it replaces, there is no 25-destination
    limit to split around and no per-element price to split it for.
    """
    if not destinations:
        return [], None

    base = osrm_url()
    if not base:
        return None, GoogleApiFailure(reason=REASON_MODE_UNSUPPORTED)

    profile = _PROFILE_FOR_MODE.get((mode or "driving").lower())
    if profile is None:
        # The extract has one profile. Answering a walk with a drive would be
        # a wrong number wearing a right number's clothes.
        logger.info("OSRM has no profile for mode %s; refusing", mode)
        return None, GoogleApiFailure(reason=REASON_MODE_UNSUPPORTED)

    points = [origin] + list(destinations)
    coordinates = ";".join(f"{float(lon)},{float(lat)}" for lat, lon in points)
    url = (
        f"{base}/table/v1/{profile}/{coordinates}"
        "?sources=0&annotations=duration,distance"
    )

    try:
        response = request_with_retries(
            requests.get,
            url,
            headers={"User-Agent": HTTP_USER_AGENT},
            timeout=30,
            logger=logger,
        )
    except requests.RequestException as exc:
        return None, failure_from_exception(exc)

    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        return None, GoogleApiFailure(
            reason=REASON_HTTP_ERROR,
            http_status=status_code if isinstance(status_code, int) else None,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return None, GoogleApiFailure(
            reason=REASON_MALFORMED_RESPONSE, message=str(exc)
        )

    if not isinstance(payload, dict) or payload.get("code") != "Ok":
        return None, GoogleApiFailure(
            reason=REASON_MALFORMED_RESPONSE,
            message=str(payload.get("code") if isinstance(payload, dict) else payload),
        )

    durations = _first_row(payload.get("durations"))
    distances = _first_row(payload.get("distances"))
    if durations is None:
        return None, GoogleApiFailure(
            reason=REASON_MALFORMED_RESPONSE, message="no durations in the answer"
        )

    legs: List[RouteLeg] = []
    for index in range(len(destinations)):
        # Index 0 is the origin to itself; the destinations follow it.
        duration = _at(durations, index + 1)
        distance = _at(distances, index + 1)
        if duration is None:
            # OSRM answered and there is no route. A measurement, not a
            # failure -- the caller keeps the two apart (#98).
            legs.append(RouteLeg())
            continue
        legs.append(
            RouteLeg(
                distance_m=int(round(distance)) if distance is not None else None,
                duration_s=int(round(duration)),
            )
        )
    return legs, None


def _first_row(matrix: Any) -> Optional[List[Any]]:
    if not isinstance(matrix, list) or not matrix:
        return None
    row = matrix[0]
    return row if isinstance(row, list) else None


def _at(row: Optional[List[Any]], index: int) -> Optional[float]:
    if row is None or index >= len(row):
        return None
    value = row[index]
    return float(value) if isinstance(value, (int, float)) else None
