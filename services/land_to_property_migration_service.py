import logging
from typing import Any, Dict, Optional

from app import db
from models import Land, Property, SearchProfile
from services.search_profile_service import SearchProfileService

logger = logging.getLogger(__name__)


class LandToPropertyMigrationService:
    """One-way migration helper from legacy Land records to universal Property records."""

    DEFAULT_PROFILE_NAME = "Legacy Lands"

    def __init__(self, profile_name: Optional[str] = None):
        self.profile_name = (profile_name or self.DEFAULT_PROFILE_NAME).strip() or self.DEFAULT_PROFILE_NAME

    def _get_or_create_profile(self) -> Optional[SearchProfile]:
        existing = SearchProfile.query.filter_by(name=self.profile_name).first()
        if existing:
            return existing

        try:
            profile = SearchProfile(
                name=self.profile_name[:120],
                description="Autocreated profile for migrated legacy Land records",
                is_active=True,
                is_default=False,
                travel_targets=SearchProfileService.get_travel_targets_config(None),
            )
            db.session.add(profile)
            db.session.commit()
            return profile
        except Exception as e:
            logger.error("Failed to create migration profile %r: %s", self.profile_name, e)
            db.session.rollback()
            return SearchProfile.query.filter_by(name=self.profile_name).first()

    def migrate(self, *, dry_run: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
        """Migrate legacy `Land` rows to `Property`.

        - Dedup key: `idealista_property_id` if present else `url`.
        - All migrated records go into a dedicated SearchProfile (defaults to "Legacy Lands").
        """
        profile = self._get_or_create_profile()
        if not profile:
            raise RuntimeError("Failed to create/find migration SearchProfile")

        query = Land.query.order_by(Land.id.asc())
        if limit is not None:
            query = query.limit(max(1, int(limit)))
        lands = query.all()

        created = 0
        skipped_existing = 0
        skipped_invalid = 0
        errors = 0

        for land in lands:
            try:
                existing = []
                if land.idealista_property_id:
                    existing = Property.query.filter_by(idealista_property_id=land.idealista_property_id).limit(1).all()
                if not existing and land.url:
                    existing = Property.query.filter_by(url=land.url).limit(1).all()
                if existing:
                    skipped_existing += 1
                    continue

                # Minimal validation: must have at least a title or URL to be useful.
                if not (land.title or land.url):
                    skipped_invalid += 1
                    continue

                prop = Property()
                prop.source_email_id = f"migrated_land_{land.id}"
                prop.idealista_property_id = land.idealista_property_id
                prop.email_subject = land.email_subject
                prop.email_sender = land.email_sender
                prop.search_profile_id = profile.id

                prop.title = land.title
                prop.url = land.url
                prop.deal_type = "sale"
                prop.property_category = "land"
                prop.property_subtype = land.land_type or "plot"
                prop.price = land.price
                prop.currency = "EUR"
                prop.area = land.area
                prop.area_type = "plot"
                prop.municipality = land.municipality
                prop.location_lat = land.location_lat
                prop.location_lon = land.location_lon
                prop.location_accuracy = land.location_accuracy or "unknown"
                prop.description = land.description

                # Keep legacy details in enrichment for reference.
                legacy_blob: Dict[str, Any] = {
                    "land_type": land.land_type,
                    "infrastructure_basic": land.infrastructure_basic or {},
                    "infrastructure_extended": land.infrastructure_extended or {},
                    "transport": land.transport or {},
                    "environment": land.environment or {},
                    "neighborhood": land.neighborhood or {},
                    "services_quality": land.services_quality or {},
                    "legal_status": land.legal_status,
                    "development_potential": getattr(land, "development_potential", None),
                    "nearest_beach_name": land.nearest_beach_name,
                    "travel_time_nearest_beach": land.travel_time_nearest_beach,
                    "travel_time_reference_city_a": land.travel_time_oviedo,
                    "travel_time_reference_city_b": land.travel_time_gijon,
                    "travel_time_airport": land.travel_time_airport,
                    "travel_time_train_station": land.travel_time_train_station,
                    "travel_time_hospital": land.travel_time_hospital,
                    "travel_time_police": land.travel_time_police,
                    "distance_airport": land.distance_airport,
                    "distance_train_station": land.distance_train_station,
                }
                prop.enrichment = {"legacy_land": legacy_blob}

                # Best-effort carryover flags/analysis.
                prop.is_favorite = bool(getattr(land, "is_favorite", False))
                prop.listing_status = land.listing_status or "active"
                prop.listing_removed_date = land.listing_removed_date
                prop.listing_last_checked = land.listing_last_checked
                prop.email_date = land.email_date
                prop.created_at = land.created_at
                prop.updated_at = land.updated_at

                if getattr(land, "ai_analysis", None):
                    prop.ai_analysis = land.ai_analysis
                if getattr(land, "enhanced_description", None):
                    prop.enhanced_description = land.enhanced_description

                if not dry_run:
                    db.session.add(prop)
                created += 1

            except Exception as e:
                errors += 1
                logger.error("Failed to migrate land %s: %s", getattr(land, "id", None), e)
                db.session.rollback()

        if not dry_run:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise RuntimeError(f"Failed to commit migration: {e}") from e

        return {
            "dry_run": bool(dry_run),
            "profile_id": profile.id,
            "profile_name": profile.name,
            "lands_considered": len(lands),
            "properties_created": created,
            "skipped_existing": skipped_existing,
            "skipped_invalid": skipped_invalid,
            "errors": errors,
        }

