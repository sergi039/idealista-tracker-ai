import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm.attributes import flag_modified

from app import db
from models import Property
from services import advertiser, sea_view_service
from services.enrichment_service import EnrichmentService
from services.hazard_service import HazardService
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
    - the free pass: nearby amenities, quality of life, hazardous
      neighbours, sea-view verdict (OpenStreetMap / OpenTopoData / local
      reference files, all free)
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
        hazard_service: Optional["HazardService"] = None,
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
        # And again for the hazard scan (#437): one EnrichmentService means
        # one Overpass client and one 5 s gate across all three lookups, which
        # is the whole reason this class holds the instance rather than each
        # service building its own.
        self.hazard_service = hazard_service or HazardService(
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

    def enrich_free_sources(
        self, prop: Property, *, commit: bool, use_ai: bool, scan_hazards: bool = True
    ) -> None:
        """The free pass: OSM amenities, quality-of-life, hazards, sea view.

        One home for the enrichers that reach no billed API -- the amenity
        counts (#152), the QoL block (#275), the hazardous-neighbour scan
        (#437) and the sea-view verdict come from OpenStreetMap, OpenTopoData
        and local reference files, so there is no billing argument for
        skipping them. Ingestion skipped them anyway until #299, which is how
        every row ingested 13-14 Aug arrived with no Extended Infrastructure
        card, no QoL block and no sea-view verdict. No Google call is fired
        here at all.

        `use_ai` is required, and it is the one part of this pass that is not
        free of consequences. The sea-view *text* signal can ask the owner's
        Claude subscription, through `tools/ai_bridge.py`, what a mention of
        the sea means. That is a cold CLI run with a 600 s timeout (#201) --
        seconds to minutes, per listing that says "vistas al mar", which in
        Asturias and Galicia is most of them.

        So the two callers differ deliberately:

        * **ingestion passes False.** A scheduled overnight run over a batch
          of alert emails would otherwise spawn one CLI per sea-mentioning
          listing, unattended and unbounded, and the owner's rule is one
          press, one subscription call. The keyword path is honest about it:
          the verdict records `source: "keywords_only"`, and `likely` /
          `unknown` stay correct states. `utils/backfill_sea_view.py` has
          carried `--no-ai` for the same reason since it was written -- the
          unattended path must not be bolder than the backfill.
        * **the Enrich button passes True.** There is a press behind it.

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

        # The hazard scan (#437) is one more Overpass round trip through the
        # same client and gate, and it runs here **only when this pass owns
        # its commits**. With `commit=False` the caller owns a transaction
        # this cannot lock inside, and a scan written that way loses a
        # concurrently committed measurement -- reproduced on the ordinary
        # Enrich flow (codex review, 2026-08-20), where session B's
        # `none_within_radius` was overwritten by session A's refusal. So the
        # Enrich path runs the scan on its own, under its own lock, before its
        # shared transaction opens; `enrich_property` does that and this skips
        # it rather than doing it twice.
        if commit and scan_hazards:
            try:
                self.hazard_service.enrich(prop, commit=True)
            except Exception as e:
                logger.warning(
                    "Hazard scan failed for %s: %s",
                    getattr(prop, "id", None),
                    e,
                )
                db.session.rollback()

        # Sea view last: with commit=True its writer takes the row under
        # FOR UPDATE and requires a session with nothing pending, which the
        # per-step commits above guarantee. A hand-set verdict survives
        # either way -- `apply_to_property` refuses to overwrite
        # `source == "manual"` (see services/sea_view_service.py).
        try:
            self.sea_view_calculator(prop, commit=commit, use_ai=use_ai)
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

        # Coordinates first, and first of everything -- not only "before the
        # travel step that needs them" (#400).
        #
        # `ensure_coordinates` now writes with the row held and commits its own
        # transaction, because the alternative is holding a row lock across
        # everything below it: Overpass, an AI-bridge call whose timeout is
        # 600 s, and Distance Matrix. That is precisely the cost #196 refused,
        # so the coordinate write is its own short transaction instead.
        #
        # Which is why it runs before the advertiser lookup rather than after.
        # `check_writable` refuses a `commit=True` write on a session with
        # anything pending (`services/enrichment_write.py`), and
        # `advertiser.enrich(commit=False)` assigns `prop.enrichment` and calls
        # `flag_modified` on a row whose seller nothing has established yet --
        # which is every fresh fotocasa import, the rows most likely to need a
        # coordinate. Left in the old order this raises on exactly them.
        #
        # The swap costs the advertiser step nothing: its early returns read
        # the URL and the stored verdict, never a coordinate, and it still runs
        # before the "no coordinates" return below, so a listing the geocoder
        # cannot place still gets its seller answered.
        self.location_service.ensure_coordinates(
            prop, refresh=refresh_coords, commit=True
        )

        # Who is selling: the owner, or an agency. Free: it reads the listing
        # page the row already links to, and only when the row does not answer
        # for itself already (`services/advertiser.py` refuses the fetch
        # otherwise). Advisory -- no score reads it, and a refusal must not
        # fail the run.
        try:
            advertiser.enrich(prop, commit=False)
        except Exception as e:
            logger.warning(
                "Advertiser lookup failed for %s: %s", getattr(prop, "id", None), e
            )

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
            #
            # `use_ai=True` because this method is the Enrich button: the
            # text signal runs before the coordinate check, so it is the one
            # part of the pass a coordinate-less row still gets in full.
            # `scan_hazards=False`: `enrich_property` already ran it above,
            # under its own lock, and a second call would take the row twice
            # for an answer that cannot have changed.
            self.enrich_free_sources(prop, commit=True, use_ai=True, scan_hazards=False)
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
        # alone by the sea-view writer itself. `use_ai=True`: this method is
        # what the Enrich button calls, so a subscription call here is one
        # owner press, not an unattended loop.
        self.enrich_free_sources(prop, commit=False, use_ai=True)

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

        # **Last**, and that position is the whole point (#437, codex review
        # round 4). Everything above shares one transaction and ends by
        # assigning the *whole* `enrichment` column from a copy this session
        # loaded before its network calls -- so a hazard block committed under
        # a lock earlier in this same request is restored to that older value
        # by the commit above, and a measurement another session made in the
        # meantime disappears with it. Reproduced with two sessions: A's early
        # `none_within_radius` came back over B's `ok`.
        #
        # Running the scan after that commit makes the sequence impossible:
        # the session is clean, the row is taken `FOR UPDATE`, the stored
        # block is read inside the lock and nothing writes the column
        # afterwards. What it does not fix is the same hazard for every
        # *other* block in this column -- `sea`, `quality_of_life`,
        # `environment`, `pool` all ride that shared assignment, and closing
        # that is a change to how this method orchestrates all of them.
        try:
            self.hazard_service.enrich(prop, commit=True)
        except Exception as e:
            logger.warning(
                "Hazard scan failed for %s: %s", getattr(prop, "id", None), e
            )
            db.session.rollback()

        # Scoring is **last**, and it moved here from the shared phase for the
        # same reason the scan did: it has to run after every measurement it
        # reads, and one of them now lands after that commit. Scoring once,
        # here, keeps the rule this pass is built on -- measure, then score --
        # rather than scoring twice or scoring over a block that did not exist
        # yet. It owns its own transaction, so a failure leaves the
        # measurements committed and the score stale, which any rescore fixes.
        if recalc_scoring:
            try:
                self.scoring_service.calculate_for_property(prop, commit=True)
            except Exception as e:
                logger.warning(
                    "Property scoring failed during enrichment for %s: %s", prop.id, e
                )
                db.session.rollback()
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
