import logging
from datetime import datetime, timezone
from typing import Optional

from app import db
from models import Property
from services.property_location_service import PropertyLocationService
from services.property_scoring_service import PropertyScoringService
from services.property_travel_service import PropertyTravelService

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
    ):
        self.location_service = location_service or PropertyLocationService()
        self.travel_service = travel_service or PropertyTravelService()
        self.scoring_service = scoring_service or PropertyScoringService()

    def enrich_property(self, prop: Property, *, refresh_coords: bool = False, recalc_scoring: bool = True) -> bool:
        if not prop:
            return False

        # Coordinates first (needed for travel).
        self.location_service.ensure_coordinates(prop, refresh=refresh_coords)

        if not (prop.location_lat and prop.location_lon):
            return False

        # Auto-assign profile by nearest custom target (optional).
        try:
            from config import Config
            if getattr(Config, "AUTO_PROFILE_ASSIGNMENT", False):
                from services.profile_assignment_service import ProfileAssignmentService

                ProfileAssignmentService().assign_nearest_profile(prop, commit=False)
        except Exception as e:
            logger.warning("Auto profile assignment failed for %s: %s", getattr(prop, "id", None), e)

        ok = self.travel_service.calculate_for_property(prop, commit=False)

        if recalc_scoring:
            try:
                self.scoring_service.calculate_for_property(prop, commit=False)
            except Exception as e:
                logger.warning("Property scoring failed during enrichment for %s: %s", prop.id, e)

        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        if not isinstance(enrichment, dict):
            enrichment = {}
        google_meta = enrichment.get("google") if isinstance(enrichment.get("google"), dict) else {}
        google_meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        enrichment["google"] = google_meta
        prop.enrichment = enrichment

        db.session.commit()
        return ok

    def enrich_property_id(self, property_id: int, *, refresh_coords: bool = False, recalc_scoring: bool = True) -> bool:
        prop = db.session.get(Property, property_id)
        if not prop:
            return False
        return self.enrich_property(prop, refresh_coords=refresh_coords, recalc_scoring=recalc_scoring)
