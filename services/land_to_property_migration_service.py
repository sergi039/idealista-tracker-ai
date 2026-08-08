import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app import db
from models import Land, Property, SearchProfile
from services.search_profile_service import SearchProfileService

logger = logging.getLogger(__name__)


class LandToPropertyMigrationService:
    """One-way migration helper from legacy Land records to universal Property records."""

    DEFAULT_PROFILE_NAME = "Legacy Lands"

    def __init__(self, profile_name: Optional[str] = None):
        self.profile_name = (
            profile_name or self.DEFAULT_PROFILE_NAME
        ).strip() or self.DEFAULT_PROFILE_NAME

    @staticmethod
    def _as_positive_int(value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            as_int = int(round(float(value)))
            if as_int < 0:
                return None
            return as_int
        except Exception:
            return None

    @staticmethod
    def _meters_or_km_to_km(value: Any) -> Optional[float]:
        """Normalize legacy distance values that may be in meters or kilometers.

        For legacy infrastructure distances we treat values > 10 as meters.
        """
        try:
            if value is None:
                return None
            raw = float(value)
            if raw < 0:
                return None
            if raw > 10:
                return round(raw / 1000.0, 1)
            return round(raw, 1)
        except Exception:
            return None

    def _build_legacy_blob(self, land: Land) -> Dict[str, Any]:
        return {
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
            "distance_hospital": getattr(land, "distance_hospital", None),
            "distance_police": getattr(land, "distance_police", None),
        }

    def _legacy_travel_targets(
        self, legacy_blob: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        labels = {
            d["key"]: d.get("label") or d["key"]
            for d in SearchProfileService.get_travel_preset_defs()
        }
        infra_ext = (
            legacy_blob.get("infrastructure_extended")
            if isinstance(legacy_blob, dict)
            else {}
        )
        infra_ext = infra_ext if isinstance(infra_ext, dict) else {}

        key_map = {
            "airport": ("travel_time_airport", "distance_airport"),
            "train_station": ("travel_time_train_station", "distance_train_station"),
            "hospital": ("travel_time_hospital", "distance_hospital"),
            "police": ("travel_time_police", "distance_police"),
            "supermarket": ("supermarket_travel_time", "supermarket_distance"),
            "school": ("school_travel_time", "school_distance"),
        }

        out: Dict[str, Dict[str, Any]] = {}
        for key, (duration_key, distance_key) in key_map.items():
            if key in ("supermarket", "school"):
                duration_min = self._as_positive_int(infra_ext.get(duration_key))
                distance_km = self._meters_or_km_to_km(infra_ext.get(distance_key))
            else:
                duration_min = self._as_positive_int(legacy_blob.get(duration_key))
                raw_dist = legacy_blob.get(distance_key)
                distance_km = (
                    round(float(raw_dist), 1) if raw_dist is not None else None
                )

            if duration_min is None and distance_km is None:
                continue

            target: Dict[str, Any] = {
                "kind": "preset",
                "enabled": True,
                "mode": "driving",
                "label": labels.get(key, key),
                "status": "legacy_imported",
            }
            if duration_min is not None:
                target["duration_min"] = duration_min
                target["duration_s"] = duration_min * 60
            if distance_km is not None:
                target["distance_km"] = distance_km
                target["distance_m"] = int(round(distance_km * 1000))
            out[key] = target

        return out

    def _merge_missing_legacy_travel(
        self, prop: Property, legacy_blob: Dict[str, Any]
    ) -> bool:
        """Backfill missing travel target keys from legacy land metrics."""
        if not isinstance(legacy_blob, dict):
            return False

        travel = prop.travel if isinstance(prop.travel, dict) else {}
        targets = (
            travel.get("targets") if isinstance(travel.get("targets"), dict) else {}
        )

        legacy_targets = self._legacy_travel_targets(legacy_blob)
        changed = False

        for key, legacy_target in legacy_targets.items():
            current = targets.get(key)
            # Keep existing calculated values; only fill missing/unresolved targets.
            if isinstance(current, dict) and current.get("duration_min") is not None:
                continue

            merged = dict(current) if isinstance(current, dict) else {}
            for field, value in legacy_target.items():
                if merged.get(field) is None:
                    merged[field] = value
            if merged != current:
                targets[key] = merged
                changed = True

        if not changed:
            return False

        travel = dict(travel)
        travel["targets"] = targets
        travel["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "profile_id" not in travel and prop.search_profile_id is not None:
            travel["profile_id"] = prop.search_profile_id
        if (
            "origin" not in travel
            and prop.location_lat is not None
            and prop.location_lon is not None
        ):
            travel["origin"] = {
                "lat": float(prop.location_lat),
                "lon": float(prop.location_lon),
            }

        prop.travel = travel
        return True

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
            logger.error(
                "Failed to create migration profile %r: %s", self.profile_name, e
            )
            db.session.rollback()
            return SearchProfile.query.filter_by(name=self.profile_name).first()

    def migrate(
        self, *, dry_run: bool = True, limit: Optional[int] = None
    ) -> Dict[str, Any]:
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
                    existing = (
                        Property.query.filter_by(
                            idealista_property_id=land.idealista_property_id
                        )
                        .limit(1)
                        .all()
                    )
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
                legacy_blob = self._build_legacy_blob(land)
                prop.enrichment = {"legacy_land": legacy_blob}
                # Seed travel targets from legacy metrics so migrated records keep
                # supermarket/school/etc. even before recalculation.
                self._merge_missing_legacy_travel(prop, legacy_blob)

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
                logger.error(
                    "Failed to migrate land %s: %s", getattr(land, "id", None), e
                )
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

    def backfill_missing_legacy_travel(
        self, *, limit: Optional[int] = None, commit: bool = True
    ) -> Dict[str, Any]:
        """Backfill missing Property.travel targets from enrichment.legacy_land.

        Useful after cutover when records were migrated before this logic existed.
        """
        query = Property.query.filter(Property.property_category == "land").order_by(
            Property.id.asc()
        )
        if limit is not None:
            query = query.limit(max(1, int(limit)))
        properties = query.all()

        updated = 0
        no_legacy = 0
        errors = 0

        for prop in properties:
            try:
                enrichment = (
                    prop.enrichment if isinstance(prop.enrichment, dict) else {}
                )
                legacy_blob = (
                    enrichment.get("legacy_land")
                    if isinstance(enrichment, dict)
                    else None
                )
                if not isinstance(legacy_blob, dict):
                    no_legacy += 1
                    continue
                if self._merge_missing_legacy_travel(prop, legacy_blob):
                    updated += 1
            except Exception as e:
                errors += 1
                logger.error(
                    "Failed to backfill legacy travel for property %s: %s",
                    getattr(prop, "id", None),
                    e,
                )
                db.session.rollback()

        if commit and updated:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                raise RuntimeError(
                    f"Failed to commit legacy travel backfill: {e}"
                ) from e

        return {
            "properties_scanned": len(properties),
            "updated": updated,
            "no_legacy_blob": no_legacy,
            "errors": errors,
            "committed": bool(commit),
        }
