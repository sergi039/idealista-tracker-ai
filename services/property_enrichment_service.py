import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm.attributes import flag_modified

from app import db
from models import Property
from services import advertiser, sea_view_service
from services.enrich_budget import lookup_budget_seconds
from services.enrichment_service import EnrichmentService
from services.property_location_service import PropertyLocationService
from services.property_scoring_service import PropertyScoringService
from services.pool_service import PoolService
from services.property_travel_service import PropertyTravelService, travel_api_state
from services.quality_of_life_service import QualityOfLifeService
from services.sea_distance_service import SeaDistanceService
from utils.http import lookup_budget

logger = logging.getLogger(__name__)


class PropertyEnrichmentService:
    """Enrichment orchestration for universal Property.

    Mirrors the legacy "Enrich with Google APIs" flow:
    - ensure coordinates (Geocoding)
    - the decisive pass, committed on its own (#434): distance to the sea
      (OpenStreetMap, free but scored) and the travel targets (Places +
      Distance Matrix, paid)
    - the advisory pass, each step writing for itself: who is selling, the
      free sources (amenities, quality of life, sea-view verdict) and the
      pool
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

    def enrich_free_sources(
        self, prop: Property, *, commit: bool, use_ai: bool
    ) -> None:
        """The free pass: OSM amenities, quality-of-life, sea view (#299).

        One home for the three enrichers that reach no billed API -- the
        amenity counts (#152), the QoL block (#275) and the sea-view verdict
        come from OpenStreetMap, OpenTopoData and local reference files, so
        there is no billing argument for skipping them. Ingestion skipped
        them anyway until #299, which is how every row ingested 13-14 Aug
        arrived with no Extended Infrastructure card, no QoL block and no
        sea-view verdict. No Google call is fired here at all.

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
        """One Enrich press: coordinates, the decisive measurements, then the
        advisory ones, then the score.

        The order and the commits are the ticket (#434). On 2026-08-20 a press
        on property 793 spent 888 s inside `PoolService.enrich` waiting on
        three unreachable Overpass instances -- a step whose criterion ships at
        weight 0 -- while the Distance Matrix request that had already been
        *billed* at 12:59 sat in an uncommitted session until 13:12:55. A
        container recreated in that window (#283) would have taken the paid
        measurement with it and left nothing recording that it had ever been
        made.

        So the run has two passes and the boundary between them is a commit:

        * the **decisive** pass -- the coordinate, the sea distance and the
          travel times -- runs first and is committed on its own. It goes
          first for the clock as much as for the commit: the free-lookup
          budget below is shared, and the steps that feed a score (and the one
          that spends money) must be the ones that get it.
        * the **advisory** pass -- who is selling, the free sources, the pool
          -- runs after, each step owning its own locked write. None of them
          can move what is already stored, and a step that finds the budget
          spent records `unavailable` and the run goes on.

        `lookup_budget` bounds the free lookups of the whole run, not each
        one: eleven bounded Overpass walks are still eleven walks. It is
        deliberately not opened around the Google steps -- see
        `utils/http._LOOKUP_DEADLINE` for why abandoning a billed request is
        the defect this fix would otherwise import.
        """
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
        self.location_service.ensure_coordinates(
            prop, refresh=refresh_coords, commit=True
        )

        with lookup_budget(lookup_budget_seconds()):
            return self._enrich_located(prop, recalc_scoring=recalc_scoring)

    def _enrich_located(self, prop: Property, *, recalc_scoring: bool) -> bool:
        """Everything after the coordinate, inside the run's lookup budget."""
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
            # Without the pool step, which the old shape did not run here
            # either. `PoolService._compute` answers `no_coordinates` -- free,
            # no network -- but that status is not one of the two its "a
            # refusal never overwrites an answer" guard defends against, so a
            # re-geocode that lost a coordinate would write it over a pool
            # somebody had measured when the row still had one.
            self._advisory_pass(prop, use_ai=True, with_pool=False)
            return False

        # Enrichment does not touch `search_profile_id` (owner decision,
        # 2026-08-09). It used to refile the property under whichever active
        # profile had the nearest custom target, discarding the saved search
        # its alert email came from. Ingestion owns that column now.

        # -- the decisive pass ------------------------------------------------
        # Distance to the sea is a scored criterion, so it runs here rather
        # than among the advisory steps, and it rides the commit below.
        try:
            self.sea_distance_service.update_property(prop, commit=False)
        except Exception as e:
            logger.warning(
                "Sea distance measurement failed for %s: %s",
                getattr(prop, "id", None),
                e,
            )

        ok = self.travel_service.calculate_for_property(prop, commit=False)
        travel_state = travel_api_state(prop)

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

        # The paid measurement is durable from here. It used to wait for the
        # single commit at the end of the whole method, behind the advisory
        # steps below (#434).
        db.session.commit()

        # -- the advisory pass ------------------------------------------------
        self._advisory_pass(prop, use_ai=True, with_pool=True)

        if recalc_scoring:
            try:
                self.scoring_service.calculate_for_property(prop, commit=False)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(
                    "Property scoring failed during enrichment for %s: %s", prop.id, e
                )
        return ok

    def _advisory_pass(self, prop: Property, *, use_ai: bool, with_pool: bool) -> None:
        """The score-neutral steps, each owning its own write.

        They commit for themselves rather than riding a shared transaction at
        the end of the run, so nothing decisive waits behind them and a step
        that fails costs only itself. `commit=True` also means each takes its
        row under `FOR UPDATE` for the length of its own write
        (`services/enrichment_write.py`), which is the #339 guarantee the
        shared-transaction form could not make.
        """
        # Who is selling: the owner, or an agency. Free: it reads the listing
        # page the row already links to, and only when the row does not answer
        # for itself already (`services/advertiser.py` refuses the fetch
        # otherwise). Advisory -- no score reads it, and a refusal must not
        # fail the run.
        try:
            advertiser.enrich(prop, commit=True)
        except Exception as e:
            db.session.rollback()
            logger.warning(
                "Advertiser lookup failed for %s: %s", getattr(prop, "id", None), e
            )

        # The free pass: amenity counts (#152), the QoL block (#275) and the
        # sea-view verdict (#299). All advisory and score-neutral; a refusal
        # is recorded as a refusal and never fails the run. A hand-set sea-view
        # verdict is left alone by the sea-view writer itself.
        self.enrich_free_sources(prop, commit=True, use_ai=use_ai)

        # Pool discovery + drive times (proposal D17): OSM via the shared
        # gate plus <=3 Distance Matrix elements (and, only on the empty
        # path, one budgeted Text Search). `pool_score` reads it, and ships at
        # weight 0 -- which is exactly why it may not hold the paid steps up.
        # It runs on whatever is left of the run's lookup budget, and records
        # `unavailable` when there is none.
        if not with_pool:
            return
        try:
            self.pool_service.enrich(prop, commit=True)
        except Exception as e:
            db.session.rollback()
            logger.warning(
                "Pool enrichment failed for %s: %s", getattr(prop, "id", None), e
            )

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
