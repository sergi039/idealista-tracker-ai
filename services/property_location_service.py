import logging
import re
from typing import List, Optional

from sqlalchemy.orm.attributes import flag_modified

from models import Property
from utils.geocoding import GeocodingService

logger = logging.getLogger(__name__)


_LOCATION_FROM_TITLE_RE = re.compile(r"\b(?:in|en)\s+(?P<loc>.+)$", re.IGNORECASE)


def _normalize_query(value: str) -> Optional[str]:
    text = " ".join(str(value or "").replace("\xa0", " ").split()).strip()
    if not text:
        return None
    text = re.sub(r"\s+\d[\d.,]*\s*€.*$", "", text).strip()
    text = re.sub(r"\s+\d[\d.,]*\s*m[²2].*$", "", text).strip()
    return text or None


def _build_geocoding_queries(prop: Property) -> List[str]:
    queries: List[str] = []

    title = _normalize_query(prop.title or "")
    if title:
        match = _LOCATION_FROM_TITLE_RE.search(title)
        loc = _normalize_query(match.group("loc")) if match else title
        if loc:
            queries.append(loc)

    municipality = _normalize_query(prop.municipality or "")
    if municipality:
        queries.append(municipality)

    out: List[str] = []
    seen = set()
    for q in queries:
        if not q:
            continue
        # Prefer explicit Spain bias without overriding full addresses.
        q_with_country = (
            q if re.search(r"\bspain\b|\bespaña\b", q, re.IGNORECASE) else f"{q}, Spain"
        )
        key = q_with_country.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q_with_country)

    return out


class PropertyLocationService:
    def __init__(self, geocoding_service: Optional[GeocodingService] = None):
        self.geocoding_service = geocoding_service or GeocodingService()

    def ensure_coordinates(self, prop: Property, refresh: bool = False) -> bool:
        """Best-effort: populate property.location_lat/lon from title/municipality.

        `Property.enrichment` is a plain `db.Column(JSON)`, so SQLAlchemy tracks
        *assignment*, not mutation -- and `prop.enrichment or {}` hands back the
        very object already on the instance, so mutating it and assigning it
        back is not a change at all. On a fresh row that is invisible, because
        the column is NULL and the `or {}` builds a new dict; on an already
        enriched row the write is silently dropped.

        Measured 2026-08-15: a re-geocode of 168 production rows wrote every
        scalar column -- coordinates and `location_accuracy` both correct -- and
        not one `enrichment["geocoding"]` record. The tool that ran it reads
        that record to decide which rows are still unmeasured, so it would have
        re-geocoded, and re-paid for, all 168 on the next run while reporting
        itself resumable.

        `flag_modified` is the idiom already used for this column in
        `services/sea_distance_service.py`, `services/quality_of_life_service.py`
        and `services/pool_service.py`.
        """
        if not prop:
            return False

        if refresh:
            prop.location_lat = None
            prop.location_lon = None
            prop.location_accuracy = "unknown"
            enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
            if isinstance(enrichment, dict):
                enrichment.pop("geocoding", None)
                prop.enrichment = enrichment or None
                flag_modified(prop, "enrichment")

        if prop.location_lat and prop.location_lon:
            return True

        for query in _build_geocoding_queries(prop):
            try:
                geo = self.geocoding_service.geocode_address(query)
            except Exception as e:
                logger.warning("Geocoding failed for %r: %s", query, e)
                continue
            if not geo:
                continue

            try:
                prop.location_lat = float(geo["lat"])
                prop.location_lon = float(geo["lng"])
            except Exception:
                continue

            accuracy = str(geo.get("accuracy") or "").strip().lower() or "unknown"
            if accuracy not in {"precise", "approximate", "unknown"}:
                accuracy = "unknown"
            prop.location_accuracy = accuracy

            enrichment = prop.enrichment or {}
            enrichment["geocoding"] = {
                "query": query,
                "formatted_address": geo.get("formatted_address"),
                "accuracy": accuracy,
            }
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
            return True

        return False
