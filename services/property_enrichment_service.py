import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm.attributes import flag_modified

from app import db
from models import Property
from services import sea_view_service
from services.enrichment_service import EnrichmentService
from services.property_location_service import PropertyLocationService
from services.property_scoring_service import PropertyScoringService
from services.pool_service import PoolService
from services.property_travel_service import PropertyTravelService, travel_api_state
from services.quality_of_life_service import QualityOfLifeService
from services.sea_distance_service import SeaDistanceService

logger = logging.getLogger(__name__)


class PropertyEnrichmentService:
    """Enrichment orchestration for universal Property.

    Mirrors the legacy "Enrich with Google APIs" flow:
    - ensure coordinates (Geocoding)
    - measure distance to the sea (OpenStreetMap, free)
    - the free pass: nearby amenities, quality of life, sea-view verdict
      (OpenStreetMap / OpenTopoData / local reference files, all free)
    - compute travel targets (Places + Distance Matrix, with fallback)
    - recompute scoring (local)

    `enrich_free_sources` is that free pass on its own: ingestion runs it
    per new row (#299) without re-firing the paid Google calls it already
    made.
    """

    def __init__(
        self,
        location_service: Optional[PropertyLocationService] = None,
        travel_service: Optional[PropertyTravelService] = None,
        scoring_service: Optional[PropertyScoringService] = None,
        sea_distance_service: Optional[SeaDistanceService] = None,
        enrichment_service: Optional[EnrichmentService] = None,
        quality_of_life_service: Optional["QualityOfLifeService"] = None,
        pool_service: Optional["PoolService"] = None,
        sea_view_calculator=None,
    ):
        self.location_service = location_service or PropertyLocationService()
        self.travel_service = travel_service or PropertyTravelService()
        self.scoring_service = scoring_service or PropertyScoringService()
        self.sea_distance_service = sea_distance_service or SeaDistanceService()
        # Only its OSM amenity half is used here; the Google half of this class
        # is the property services above.
        self.enrichment_service = enrichment_service or EnrichmentService()
        # Shares the amenity client's Overpass transport through the same
        # EnrichmentService instance, so one gate paces both lookups.
        self.quality_of_life_service = quality_of_life_service or QualityOfLifeService(
            enrichment_service=self.enrichment_service
        )
        # Same sharing for the pool lookup (Overpass) and its drive times
        # (the travel service's own Distance Matrix client and caches).
        self.pool_service = pool_service or PoolService(
            enrichment_service=self.enrichment_service,
            travel_service=self.travel_service,
        )
        # The sea-view half is module functions, not a class, but the
        # injection point exists for the reason every other half has one: a
        # test replaces the step that reaches Overpass and OpenTopoData
        # through its own module, whose transport the amenity mocks above
        # never cover. The default is the same evaluate+apply pair the
        # backfill uses (services/sea_view_service.py).
        self.sea_view_calculator = (
            sea_view_calculator or sea_view_service.calculate_for_property
        )

    def enrich_free_sources(self, prop: Property, *, commit: bool) -> None:
        """The free pass: OSM amenities, quality-of-life, sea view (#299).

        One home for the three enrichers that spend nothing -- the amenity
        counts (#152), the QoL block (#275) and the sea-view verdict all come
        from OpenStreetMap, OpenTopoData and local reference files, so there
        is no billing argument for skipping them. Ingestion skipped them
        anyway until #299, which is how every row ingested 13-14 Aug arrived
        with no Extended Infrastructure card, no QoL block and no sea-view
        verdict. Nothing here fires a paid Google call.

        Pacing stays in the transports (each client hands its gate to
        `request_with_retries`), never in this loop. Each step fails
        independently, the writers themselves record a refusal as a refusal,
        and no failure here may fail the caller's run.

        With `commit=True` (ingestion) each step owns its commit, and a step
        that raised rolls back so the next one starts on a clean session.
        With `commit=False` (the Enrich flow) everything rides the caller's
        transaction, so a failed step must not roll back -- that would
        discard the caller's own pending work.
        """
        try:
            self.enrichment_service.enrich_osm_amenities(prop, commit=commit)
        except Exception as e:
            logger.warning(
                "OSM amenity lookup failed for %s: %s",
                getattr(prop, "id", None),
                e,
            )
            if commit:
                db.session.rollback()

        try:
            self.quality_of_life_service.enrich(prop, commit=commit)
        except Exception as e:
            logger.warning(
                "Quality-of-life enrichment failed for %s: %s",
                getattr(prop, "id", None),
                e,
            )
            if commit:
                db.session.rollback()

        # Sea view last: with commit=True its writer takes the row under
        # FOR UPDATE and requires a session with nothing pending, which the
        # per-step commits above guarantee. A hand-set verdict survives
        # either way -- `apply_to_property` refuses to overwrite
        # `source == "manual"` (see services/sea_view_service.py).
        try:
            self.sea_view_calculator(prop, commit=commit)
        except Exception as e:
            logger.warning(
                "Sea-view evaluation failed for %s: %s",
                getattr(prop, "id", None),
                e,
            )
            if commit:
                db.session.rollback()

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

        # `is None`, not truthiness: a coordinate of exactly 0 is a location,
        # and the amenity lookup below already treats it as one.
        if prop.location_lat is None or prop.location_lon is None:
            # Geocoding could not place this listing, so nothing *paid* below
            # can run. The free pass still does: the amenity lookup records
            # that it was never asked rather than leaving the section absent,
            # which reads as "nothing nearby" (#152); the QoL INE context
            # needs no coordinates at all, and its coordinate parts record
            # `no_coordinates` instead of silently never existing (diff
            # review, 2026-08-14); the sea-view text signal is computed and
            # its geometry honestly reads `unknown`. This path has no shared
            # commit to ride, so each step takes its own.
            self.enrich_free_sources(prop, commit=True)
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

        # The free pass: amenity counts (#152), the QoL block (#275) and the
        # sea-view verdict (#299). All advisory and score-neutral; a refusal
        # is recorded as a refusal and never fails the run. It rides the
        # shared commit at the end, and a hand-set sea-view verdict is left
        # alone by the sea-view writer itself.
        self.enrich_free_sources(prop, commit=False)

        ok = self.travel_service.calculate_for_property(prop, commit=False)
        travel_state = travel_api_state(prop)

        # Pool discovery + drive times (proposal D17): OSM via the shared
        # gate plus ≤3 Distance Matrix elements (and, only on the empty
        # path, one budgeted Text Search). Before scoring, because
        # `pool_score` reads it — though it ships at weight 0, so nothing
        # moves until the owner turns it on. A failure never fails the run.
        try:
            self.pool_service.enrich(prop, commit=False)
        except Exception as e:
            logger.warning(
                "Pool enrichment failed for %s: %s", getattr(prop, "id", None), e
            )

        if recalc_scoring:
            try:
                self.scoring_service.calculate_for_property(prop, commit=False)
            except Exception as e:
                logger.warning(
                    "Property scoring failed during enrichment for %s: %s", prop.id, e
                )

        # `enrichment` is a plain JSON column, not a MutableDict: reading the
        # loaded dict, mutating it and assigning the *same object* back leaves
        # the attribute clean and the flush emits no UPDATE. It happened to
        # reach the database only because a step above had already replaced the
        # blob; on a run where every one of them failed, this marker was lost.
        enrichment = dict(prop.enrichment) if isinstance(prop.enrichment, dict) else {}
        google_meta = (
            dict(enrichment["google"])
            if isinstance(enrichment.get("google"), dict)
            else {}
        )
        # An "updated_at" on its own claimed the property was enriched even when
        # Google refused every request (#98). Record what actually happened.
        google_meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        google_meta["travel_state"] = travel_state
        enrichment["google"] = google_meta
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

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
