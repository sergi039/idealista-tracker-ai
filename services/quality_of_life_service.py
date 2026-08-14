"""Quality-of-life enrichment for properties (agreed proposal D15/D16/D18/D20).

Writes ``enrichment["quality_of_life"]`` with one honestly-labeled block per
part — municipality socioeconomic context (INE), supermarket reach (OSM) and
hospitals (CNH) — each carrying its own status and source vintage. Nothing in
this service moves a score: the QoL card is informational, like the beaches
block, and the only Phase-2 criterion that will score (the pool) ships
separately at weight 0.

The three #98-shaped rules, hard-negotiated in the proposal review:
* a lookup that refused (`unavailable`) never renders or caches as absence;
* a municipality the join cannot match says `not_matched`, it is not guessed;
* every distance here is straight-line and labeled so — drive times belong
  to the Distance Matrix passes, not to this free service.

Reference data comes from two importer-generated files (`data/
ine_municipal.json`, `data/hospitals_cnh.json`); a missing or unreadable file
reads as `no_reference_data`, never as an empty landscape.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm.attributes import flag_modified

from models import Property
from services.enrichment_service import EnrichmentService
from services.sea_view_service import haversine_m
from utils.municipality_codes import build_index as build_municipality_index
from utils.municipality_codes import match as match_municipality

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
INE_DATA_PATH = os.path.join(_DATA_DIR, "ine_municipal.json")
CNH_DATA_PATH = os.path.join(_DATA_DIR, "hospitals_cnh.json")

# The hospital groupings are *descriptive local display groupings* derived
# from CNH fields (beds, teaching accreditation, high-tech equipment) — not
# official tiers, and the card says so. Defined by the importer; listed here
# so the card renders them in a stable order.
HOSPITAL_GROUPINGS = (
    "teaching_high_tech",
    "general_acute",
    "limited_recorded_capability",
)

# Statuses a rerun could improve — shared with the backfill's `needs` filter.
# Everything else is an answer; re-asking does not change it.
RETRYABLE_STATUSES = frozenset({"unavailable", "no_reference_data"})


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Reference file unreadable: %s", path, exc_info=True)
        return None


class QualityOfLifeService:
    """Free QoL enrichment: INE context, supermarket reach, CNH hospitals."""

    def __init__(self, enrichment_service: Optional[EnrichmentService] = None):
        self.enrichment_service = enrichment_service or EnrichmentService()
        self._ine: Optional[dict] = None
        self._ine_index: Optional[Dict[str, str]] = None
        self._hospitals: Optional[dict] = None

    # -- reference data ----------------------------------------------------

    def _ine_data(self) -> Optional[dict]:
        if self._ine is None:
            self._ine = _load_json(INE_DATA_PATH)
        return self._ine

    def _ine_name_index(self) -> Dict[str, str]:
        """normalized municipality name -> INE code.

        Built through `build_index`, whose collision guard is the point: two
        in-scope names normalizing to one key would make every later match a
        silent coin flip (diff review, 2026-08-14). A collision raises, the
        per-part handler in enrich() records the part `unavailable`, and the
        bad reference file gets fixed instead of quietly mis-joining.
        """
        if self._ine_index is None:
            data = self._ine_data() or {}
            code_to_name = {
                code: (row or {}).get("name")
                for code, row in (data.get("municipalities") or {}).items()
                if (row or {}).get("name")
            }
            self._ine_index = build_municipality_index(code_to_name)
        return self._ine_index

    def _hospital_rows(self) -> Optional[list]:
        if self._hospitals is None:
            self._hospitals = _load_json(CNH_DATA_PATH)
        if self._hospitals is None:
            return None
        rows = self._hospitals.get("hospitals")
        return rows if isinstance(rows, list) else None

    # -- parts -------------------------------------------------------------

    def municipality_context(self, municipality: Optional[str]) -> Dict[str, Any]:
        data = self._ine_data()
        if not data:
            return {"status": "no_reference_data"}
        if not municipality or not str(municipality).strip():
            return {"status": "no_municipality"}

        code = match_municipality(str(municipality), self._ine_name_index())
        if code is None:
            # Truncated names, districts and junk stay honest: the join says
            # it could not match rather than guessing (verified 2026-08-13:
            # normalization + aliases covers 83% of rows; the rest is this).
            return {"status": "not_matched", "queried": str(municipality)}

        row = (data.get("municipalities") or {}).get(code) or {}
        province = row.get("province")
        medians = (data.get("province_medians") or {}).get(province) or {}
        return {
            "status": "ok",
            "ine_code": code,
            "name_matched": row.get("name"),
            "renta_media_persona": row.get("renta_media_persona"),
            "renta_year": row.get("renta_year"),
            "renta_province_median": medians.get("renta_media_persona"),
            "population": row.get("population"),
            "population_5y_change_pct": row.get("population_5y_change_pct"),
            "population_year": row.get("population_year"),
            "source": (data.get("source") or {}),
        }

    def supermarket_reach(
        self, lat: Optional[float], lon: Optional[float]
    ) -> Dict[str, Any]:
        if lat is None or lon is None:
            return {"status": "no_coordinates"}
        reading = self.enrichment_service.fetch_osm_supermarket_reach(
            float(lat), float(lon)
        )
        if reading.failure is not None:
            # A refusal never renders as "no shops" (#98).
            return {
                "status": "unavailable",
                "reason": getattr(reading.failure, "reason", None),
            }
        items = reading.items or []
        return {
            "status": "ok" if items else "osm_empty",
            "items": items,
            "measured_at": reading.measured_at,
            "distance_basis": "straight_line",
        }

    def hospitals(self, lat: Optional[float], lon: Optional[float]) -> Dict[str, Any]:
        rows = self._hospital_rows()
        if rows is None:
            return {"status": "no_reference_data"}
        if lat is None or lon is None:
            return {"status": "no_coordinates"}

        nearest: Dict[str, Dict[str, Any]] = {}
        for hospital in rows:
            if not isinstance(hospital, dict):
                continue
            grouping = hospital.get("grouping")
            h_lat, h_lon = hospital.get("lat"), hospital.get("lon")
            if grouping not in HOSPITAL_GROUPINGS or h_lat is None or h_lon is None:
                continue
            km = round(
                haversine_m(float(lat), float(lon), float(h_lat), float(h_lon))
                / 1000.0,
                1,
            )
            current = nearest.get(grouping)
            if current is None or km < current["distance_km"]:
                nearest[grouping] = {
                    "name": hospital.get("name"),
                    "municipality": hospital.get("municipality"),
                    "beds": hospital.get("beds"),
                    "teaching": hospital.get("teaching"),
                    "high_tech_count": hospital.get("high_tech_count"),
                    "lat": h_lat,
                    "lon": h_lon,
                    "distance_km": km,
                }
        if not nearest:
            return {"status": "no_reference_data"}
        return {
            "status": "ok",
            "nearest": nearest,
            "source": (self._hospitals or {}).get("source"),
            "distance_basis": "straight_line",
        }

    # -- orchestration -----------------------------------------------------

    def enrich(self, prop: Property, commit: bool = False) -> Dict[str, Any]:
        """Compute all parts and store them under enrichment.quality_of_life.

        Each part fails independently: an Overpass refusal must not take the
        INE context down with it. A part that raises is recorded as
        `unavailable` with the error class, mirroring the enrichment
        pipeline's own rule that an advisory source never fails a run.
        """
        parts: Dict[str, Any] = {}
        for key, compute in (
            ("municipality", lambda: self.municipality_context(prop.municipality)),
            (
                "supermarkets",
                lambda: self.supermarket_reach(prop.location_lat, prop.location_lon),
            ),
            ("hospitals", lambda: self.hospitals(prop.location_lat, prop.location_lon)),
        ):
            try:
                parts[key] = compute()
            except Exception as exc:
                logger.error("QoL part %s failed for %s", key, prop.id, exc_info=True)
                parts[key] = {"status": "unavailable", "reason": type(exc).__name__}

        # A refusal never overwrites a previous measurement — the sea-distance
        # precedent, applied per part (diff review, 2026-08-14): when the new
        # part is retryable and the stored one holds an answer, the answer
        # stays and the failed attempt is stamped beside it.
        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        previous = enrichment.get("quality_of_life")
        previous = previous if isinstance(previous, dict) else {}
        now_iso = datetime.now(timezone.utc).isoformat()
        for key, new_part in list(parts.items()):
            old_part = previous.get(key)
            if (
                new_part.get("status") in RETRYABLE_STATUSES
                and isinstance(old_part, dict)
                and old_part.get("status") not in RETRYABLE_STATUSES
                and old_part.get("status") is not None
            ):
                kept = dict(old_part)
                kept["last_attempt_status"] = new_part.get("status")
                kept["last_attempt_at"] = now_iso
                parts[key] = kept

        payload = dict(parts)
        payload["updated_at"] = now_iso

        enrichment["quality_of_life"] = payload
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

        if commit:
            from app import db

            db.session.commit()
        return payload
