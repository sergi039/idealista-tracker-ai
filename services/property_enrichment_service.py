import logging
from datetime import datetime, timezone
from typing import Optional

from app import db
from models import Property
from services.property_location_service import PropertyLocationService
from services.property_scoring_service import PropertyScoringService
from services.property_travel_service import PropertyTravelService, travel_api_state
from services.sea_distance_service import SeaDistanceService

logger = logging.getLogger(__name__)


class PropertyEnrichmentService:
    """Google-enrichment orchestration for universal Property.

    Mirrors the legacy "Enrich with Google APIs" flow:
    - ensure coordinates (Geocoding)
    - compute travel targets (Places + Distance Matrix, with fallback)
    - recompute scoring (local)
    """

    def __init__(
        self,
        location_service: Optional[PropertyLocationService] = None,
        travel_service: Optional[PropertyTravelService] = None,
        scoring_service: Optional[PropertyScoringService] = None,
        sea_distance_service: Optional[SeaDistanceService] = None,
    ):
        self.location_service = location_service or PropertyLocationService()
        self.travel_service = travel_service or PropertyTravelService()
        self.scoring_service = scoring_service or PropertyScoringService()
        self.sea_distance_service = sea_distance_service or SeaDistanceService()

    def enrich_property(
        self,
        prop: Property,
        *,
        refresh_coords: bool = False,
        recalc_scoring: bool = True,
    ) -> bool:
        if not prop:
            return False

        # Coordinates first (needed for travel).
        self.location_service.ensure_coordinates(prop, refresh=refresh_coords)

        if not (prop.location_lat and prop.location_lon):
            return False

        # Enrichment does not touch `search_profile_id` (owner decision,
        # 2026-08-09). It used to refile the property under whichever active
        # profile had the nearest custom target, discarding the saved search
        # its alert email came from. Ingestion owns that column now.

        # Distance to the sea is measured before scoring so the recalculation
        # below already sees it. It rides on the shared commit at the end.
        try:
            self.sea_distance_service.update_property(prop, commit=False)
        except Exception as e:
            logger.warning(
                "Sea distance measurement failed for %s: %s",
                getattr(prop, "id", None),
                e,
            )

>>>>>>> 1fa6510 (feat(scoring): score distance to the sea from OSM coastline)
        ok = self.travel_service.calculate_for_property(prop, commit=False)
        travel_state = travel_api_state(prop)

        if recalc_scoring:
            try:
                self.scoring_service.calculate_for_property(prop, commit=False)
            except Exception as e:
                logger.warning(
                    "Property scoring failed during enrichment for %s: %s", prop.id, e
                )

        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        if not isinstance(enrichment, dict):
            enrichment = {}
        google_meta = (
            enrichment.get("google")
            if isinstance(enrichment.get("google"), dict)
            else {}
        )
        # An "updated_at" on its own claimed the property was enriched even when
        # Google refused every request (#98). Record what actually happened.
        google_meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        google_meta["travel_state"] = travel_state
        enrichment["google"] = google_meta
        prop.enrichment = enrichment

        db.session.commit()
        return ok

    def enrich_property_id(
        self,
        property_id: int,
        *,
        refresh_coords: bool = False,
        recalc_scoring: bool = True,
    ) -> bool:
        prop = db.session.get(Property, property_id)
        if not prop:
            return False
        return self.enrich_property(
            prop, refresh_coords=refresh_coords, recalc_scoring=recalc_scoring
        )
