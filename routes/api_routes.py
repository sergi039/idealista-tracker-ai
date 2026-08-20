import logging
from datetime import datetime, timezone
from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

# get_or_404 raises HTTPException, and the blanket `except Exception` handlers
# below would answer 500 for it: every one of them re-raises it first so an
# unknown id stays a 404 (issue #136).
from werkzeug.exceptions import HTTPException
from models import Land, LandHistory, SyncHistory, AiAnalysisVariant
from services.enrich_budget import poll_timeout_ms
from services.ingest_policy import ingest_verdict
from services.listing_verification import read_verdict as listing_verdict
from utils.api_errors import json_http_error
from utils.listing_search import listing_search_clause
from services import advertiser
from utils.listing_source import source_filter_clause
from utils.municipality_grouping import municipality_filter_clause
from app import db
from app import limiter

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


@api_bp.errorhandler(HTTPException)
def handle_api_http_exception(error: HTTPException) -> tuple[Response, int]:
    """Answer every HTTP error raised in this blueprint as JSON (issue #140).

    The 404 from `db.get_or_404()` and the 415 from `request.get_json()` used
    to arrive as werkzeug's HTML page while every failure the handlers report
    themselves is `{"success": false, "error": ...}`. A blueprint handler only
    sees what its own views raise; a URL matching no rule at all is caught by
    the app-level handler in app.py.
    """
    return json_http_error(error)


def _should_run_sync(allow_request_override: bool = True) -> bool:
    """Should this request do the work inline instead of queueing it?

    `allow_request_override=False` drops the `?sync=1` escape hatch for
    endpoints where holding a worker for the full outbound timeout is a way to
    exhaust the pool -- the API is unauthenticated, so a handful of concurrent
    calls is all it takes, and a per-IP rate limit does not stop concurrency
    (issue #136).
    """
    try:
        if current_app and current_app.config.get("TESTING"):
            return True
    except Exception:
        pass
    if not allow_request_override:
        return False
    return request.args.get("sync") in ("1", "true", "yes", "on")


def _enqueue(job_fn, *, job_type: str, meta=None, dedupe_key=None) -> str:
    from services.background_jobs import enqueue_job

    app_obj = current_app._get_current_object()
    return enqueue_job(
        job_fn,
        job_type=job_type,
        meta=meta or {},
        app=app_obj,
        dedupe_key=dedupe_key,
    )


# The unique constraint migration 017 adds (#190 review, blocker 3). Named so
# `_is_property_variant_collision` can recognize it from PostgreSQL's
# structured diagnostics.
PROPERTY_VARIANT_UNIQUE_CONSTRAINT = (
    "ux_property_ai_analysis_variants_property_provider"
)
# What SQLite says when that same constraint (declared as a UniqueConstraint
# on the model) refuses a row -- it has no structured diagnostics, so the
# message is matched in full. See services/search_profile_service.py's
# _is_keyless_name_collision for the same idiom against a different table.
_SQLITE_PROPERTY_VARIANT_COLLISION = (
    "UNIQUE constraint failed: property_ai_analysis_variants.property_id, "
    "property_ai_analysis_variants.provider"
)


def _is_property_variant_collision(error: Exception) -> bool:
    """Whether `error` is the (property_id, provider) unique constraint
    refusing a second row for a pair that already has one -- the expected,
    fully-recovered shape of the #190 review's blocker 3 insert race.

    Narrow on purpose, the same way _is_keyless_name_collision is: an
    unrelated IntegrityError (a dropped connection, a foreign-key violation)
    must keep its own report rather than being silently treated as "someone
    else already wrote this". property_ai_analysis_variants only has the one
    unique constraint plus a FK to properties(id) that cannot fail here (the
    property was already loaded via get_or_404 earlier in the request), which
    keeps the false-positive risk low, but the check still names the
    constraint rather than assuming any IntegrityError here is the race.
    """
    if not isinstance(error, IntegrityError):
        return False
    orig = getattr(error, "orig", None)
    if orig is None:
        return False
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint is not None:
        return constraint == PROPERTY_VARIANT_UNIQUE_CONSTRAINT
    return str(orig).strip() == _SQLITE_PROPERTY_VARIANT_COLLISION


def _upsert_property_ai_variant(
    property_id: int, provider: str, *, model, analysis, price_at_analysis=None
) -> None:
    """Write the (property_id, provider) AI-analysis variant without the
    query-then-insert race migration 017 closes off at the database level
    (#190 review, blocker 3).

    An UPDATE first -- the common case, since a variant usually already
    exists after a property's first successful analysis for that provider.
    If none matched, INSERT. If that INSERT loses a race to a concurrent
    writer (an interrupted job's async retry alongside a `?sync=1` request,
    which bypasses background_jobs' dedupe_key entirely because it never
    goes through enqueue_job), the unique constraint turns the race into an
    IntegrityError this recovers from by updating the winner -- instead of
    either leaving this run's result silently unwritten or letting a second
    row exist for the same pair.

    Stages every write -- never commits (#190 review round 4, finding 4).
    The caller (a job function running inside `_execute_job`) commits once,
    together with the terminal CAS write that records the job itself as
    successful, so a reap racing this job's own execution rolls both back
    together rather than letting this land after the job has already lost
    ownership. The INSERT attempt runs inside a `SAVEPOINT`
    (`Session.begin_nested()`): if it loses the race, only that savepoint
    rolls back, not the rest of what the caller staged in the same
    session (e.g. `prop_local.ai_analysis`, set just before this is called
    in `analyze_universal_property_structured`) -- a plain `session.
    rollback()` here would discard that too.
    """
    from models import PropertyAiAnalysisVariant

    stamp = datetime.now(timezone.utc)
    # `price_at_analysis` is the price the prompt carried, so the page can say
    # an analysis predates a price correction instead of presenting it as
    # current (#235). None means the caller could not say, and stays None.
    fields = {
        "analysis": analysis,
        "model": model,
        "created_at": stamp,
        "price_at_analysis": price_at_analysis,
    }

    updated = (
        db.session.query(PropertyAiAnalysisVariant)
        .filter_by(property_id=property_id, provider=provider)
        .update(fields, synchronize_session=False)
    )
    if updated:
        return

    try:
        with db.session.begin_nested():
            db.session.add(
                PropertyAiAnalysisVariant(
                    property_id=property_id,
                    provider=provider,
                    model=model,
                    analysis=analysis,
                    price_at_analysis=price_at_analysis,
                )
            )
    except IntegrityError as exc:
        if not _is_property_variant_collision(exc):
            raise
        recovered = (
            db.session.query(PropertyAiAnalysisVariant)
            .filter_by(property_id=property_id, provider=provider)
            .update(fields, synchronize_session=False)
        )
        if not recovered:
            # The row that just won the race would have to be deleted in
            # this exact instant for this to happen. Fail loudly rather than
            # claim success silently over a result that was never stored.
            raise RuntimeError(
                "Lost the insert race for property_ai_analysis_variants "
                f"(property_id={property_id}, provider={provider}) but the "
                "row that won it could not be found to update"
            ) from exc


# The unique constraint migration 017 also adds for the legacy Land model's
# variants (#190 review round 3, finding 4).
LAND_VARIANT_UNIQUE_CONSTRAINT = "ux_ai_analysis_variants_land_provider"
_SQLITE_LAND_VARIANT_COLLISION = (
    "UNIQUE constraint failed: ai_analysis_variants.land_id, "
    "ai_analysis_variants.provider"
)


def _is_land_variant_collision(error: Exception) -> bool:
    """The (land_id, provider) analogue of `_is_property_variant_collision`."""
    if not isinstance(error, IntegrityError):
        return False
    orig = getattr(error, "orig", None)
    if orig is None:
        return False
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint is not None:
        return constraint == LAND_VARIANT_UNIQUE_CONSTRAINT
    return str(orig).strip() == _SQLITE_LAND_VARIANT_COLLISION


def _upsert_land_ai_variant(
    land_id: int, provider: str, *, model, analysis, price_at_analysis=None
) -> None:
    """Write the (land_id, provider) AI-analysis variant without the
    query-then-insert race migration 017 closes off at the database level
    for the legacy `Land` model too (#190 review round 3, finding 4) -- the
    same update-or-insert-with-race-recovery pattern as
    `_upsert_property_ai_variant`, against `AiAnalysisVariant` instead of
    `PropertyAiAnalysisVariant`.

    Stages every write -- never commits, for the same reason
    `_upsert_property_ai_variant` doesn't (#190 review round 4, finding 4):
    the caller commits once, together with the job's own terminal CAS
    write. The INSERT attempt runs inside a `SAVEPOINT` so a lost race
    rolls back only that attempt, not `land.ai_analysis`, set just before
    this is called and still only staged, not committed.
    """
    from models import AiAnalysisVariant

    stamp = datetime.now(timezone.utc)
    fields = {
        "analysis": analysis,
        "model": model,
        "created_at": stamp,
        "price_at_analysis": price_at_analysis,
    }

    updated = (
        db.session.query(AiAnalysisVariant)
        .filter_by(land_id=land_id, provider=provider)
        .update(fields, synchronize_session=False)
    )
    if updated:
        return

    try:
        with db.session.begin_nested():
            db.session.add(
                AiAnalysisVariant(
                    land_id=land_id,
                    provider=provider,
                    model=model,
                    analysis=analysis,
                    price_at_analysis=price_at_analysis,
                )
            )
    except IntegrityError as exc:
        if not _is_land_variant_collision(exc):
            raise
        recovered = (
            db.session.query(AiAnalysisVariant)
            .filter_by(land_id=land_id, provider=provider)
            .update(fields, synchronize_session=False)
        )
        if not recovered:
            raise RuntimeError(
                "Lost the insert race for ai_analysis_variants "
                f"(land_id={land_id}, provider={provider}) but the row that "
                "won it could not be found to update"
            ) from exc


def _run_sync(job_fn, *, job_type: str, meta=None, dedupe_key=None):
    """Runs `job_fn` inline through `background_jobs`' registry instead of
    calling it directly (#190 review round 3, finding 4).

    The sync path (`?sync=1`, and every request under `TESTING`) used to
    call the paid closure with no `background_jobs` involvement at all --
    bypassing dedupe_key entirely, so a sync call could run alongside a
    live async job (or another sync call) for the exact same unit of work,
    paying for it twice. This claims the same dedupe_key slot
    `enqueue_job`'s async path does and runs `job_fn` in this thread via
    `run_job_sync`, sharing its CAS transitions and its at-most-one-
    execution-per-race guarantee.

    Returns the finished job's dict (status/result/error) on success.
    Raises `JobAlreadyActive` (propagated, not caught here) when the
    dedupe_key is already claimed by another job -- the caller answers 409
    with `.job_id` rather than running a second execution.
    """
    from services.background_jobs import get_job, run_job_sync

    app_obj = current_app._get_current_object()
    job_id = run_job_sync(
        job_fn, job_type=job_type, meta=meta or {}, app=app_obj, dedupe_key=dedupe_key
    )
    return get_job(job_id)


def _job_already_active_response(exc) -> tuple[Response, int]:
    """The 409 body every sync route answers with when `_run_sync` raises
    `JobAlreadyActive` -- the client polls `/api/jobs/<job_id>` for the run
    that already claims this unit of work instead of the route paying for a
    second one."""
    return jsonify(
        {
            "success": False,
            "error": (
                "An equivalent analysis is already in progress or just "
                "finished. Poll its job instead of retrying."
            ),
            "job_id": exc.job_id,
        }
    ), 409


def _enqueue_outcome_unknown_response(exc) -> tuple[Response, int]:
    """The 503 body every route answers with when `enqueue_job`/
    `run_job_sync` raises `EnqueueOutcomeUnknown`: the insert's own commit
    failed ambiguously (PostgreSQL may have committed it server-side and
    only failed to acknowledge that), and `services/background_jobs.py`
    does not try to resolve which it was (issue #204). This process
    genuinely does not know whether the analysis was queued -- 503, not
    500, since this is the same shape as any other transient infrastructure
    failure, and honest about the two real possibilities rather than
    claiming either one.
    """
    return jsonify(
        {
            "success": False,
            "error": (
                "Could not confirm whether this analysis was queued. It "
                "may have started anyway -- wait a few minutes, or try "
                "again."
            ),
        }
    ), 503


@api_bp.route("/jobs/<job_id>", methods=["GET"])
def get_job_status(job_id: str):
    """Fetch status/result for a background job."""
    from services.background_jobs import get_job, serialize_job

    job = get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify({"success": True, "job": serialize_job(job)})


@api_bp.route("/healthz")
def health_check():
    """Return 200 only when the database schema and scheduler are ready."""
    from services.health_service import collect_health_checks

    checks = collect_health_checks(db)
    all_ok = checks == {
        "database": "ok",
        "schema": "ok",
        "scheduler": "running",
    }
    status_code = 200 if all_ok else 503
    return jsonify({"ok": all_ok, "checks": checks}), status_code


@api_bp.route("/lands/enrich-all", methods=["POST"])
@limiter.limit("2 per 5 minutes")
def bulk_enrichment():
    """Enrich all properties that are missing extended infrastructure or environment data"""
    try:
        from services.enrichment_service import EnrichmentService

        def _run():
            lands_to_enrich = Land.query.filter(
                (Land.infrastructure_extended.is_(None))
                | (Land.environment.is_(None))
                | (Land.transport.is_(None))
                | (Land.services_quality.is_(None))
                | (Land.infrastructure_extended == {})
                | (Land.environment == {})
                | (Land.transport == {})
                | (Land.services_quality == {})
            ).all()

            enrichment_service = EnrichmentService()
            success_count = 0
            total_count = len(lands_to_enrich)

            for land in lands_to_enrich:
                try:
                    if enrichment_service.enrich_land(land.id):
                        success_count += 1
                        logger.info(
                            "Enriched land %s: %s", land.id, (land.title or "")[:50]
                        )
                except Exception as e:
                    logger.error("Failed to enrich land %s: %s", land.id, e)
                    continue

            return {
                "success": True,
                "message": f"Successfully enriched {success_count} out of {total_count} properties",
                "enriched_count": success_count,
                "total_found": total_count,
            }

        if _should_run_sync():
            return jsonify(_run())

        job_id = _enqueue(_run, job_type="lands_enrich_all")
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Bulk enrichment queued",
            }
        ), 202

    except Exception:
        logger.error("Bulk enrichment failed", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/land/<int:land_id>/enrich", methods=["POST"])
@limiter.limit("10 per minute")
def manual_enrichment(land_id):
    """Manually trigger data enrichment for a specific property"""
    try:
        from services.enrichment_service import EnrichmentService

        land = db.get_or_404(Land, land_id)

        payload = request.get_json(silent=True) if request.is_json else {}
        refresh_coords = False
        if isinstance(payload, dict):
            refresh_coords = bool(payload.get("refresh_coords"))
        if request.args.get("refresh_coords") in ("1", "true", "yes", "on"):
            refresh_coords = True

        def _run():
            enrichment_service = EnrichmentService()
            success = enrichment_service.enrich_land(
                land_id, refresh_coords=refresh_coords
            )
            if success:
                return {
                    "success": True,
                    "message": "Property enriched successfully with Google API data",
                }
            return {
                "success": False,
                "error": "Geocoding failed; enrichment skipped. Check that the property has a valid address.",
            }

        if _should_run_sync():
            result = _run()
            status_code = 200 if result.get("success") else 200
            return jsonify(result), status_code

        job_id = _enqueue(
            _run,
            job_type="land_enrich",
            meta={"land_id": land.id, "refresh_coords": refresh_coords},
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Enrichment queued",
            }
        ), 202

    except HTTPException:
        raise
    except Exception:
        logger.error("Manual enrichment failed for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/lands/reanalyze-environment", methods=["POST"])
def reanalyze_environment():
    """Re-analyze environment (sea_view, mountain_view, etc.) for all lands using updated logic"""
    try:
        from services.enrichment_service import EnrichmentService
        from sqlalchemy.orm.attributes import flag_modified

        def _run():
            enrichment_service = EnrichmentService()
            lands = Land.query.all()

            updated_count = 0
            sea_view_removed = 0

            for land in lands:
                old_environment = dict(land.environment) if land.environment else {}
                old_sea_view = old_environment.get("sea_view", False)

                # Re-analyze environment with new strict logic
                enrichment_service._analyze_environment(land)

                new_sea_view = land.environment.get("sea_view", False)

                # Mark JSONB field as modified for SQLAlchemy to detect changes
                flag_modified(land, "environment")

                # Track changes
                if old_sea_view != new_sea_view:
                    updated_count += 1
                    if old_sea_view and not new_sea_view:
                        sea_view_removed += 1
                        logger.info(
                            "Removed false sea_view from land %s: %s",
                            land.id,
                            (land.title or "")[:50],
                        )

            db.session.commit()

            return {
                "success": True,
                "total_lands": len(lands),
                "updated": updated_count,
                "sea_view_removed": sea_view_removed,
                "message": f"Re-analyzed {len(lands)} lands. {sea_view_removed} false sea_view flags removed.",
            }

        if _should_run_sync():
            return jsonify(_run())

        job_id = _enqueue(_run, job_type="lands_reanalyze_environment")
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Environment re-analysis queued",
            }
        ), 202

    except Exception:
        logger.error("Environment re-analysis failed", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/ingest/email/run", methods=["POST"])
@limiter.limit("5 per minute")
def manual_ingestion():
    """Manually trigger email ingestion"""
    # Before anything reads the request or touches a service: a machine that does
    # not ingest on a cron tick does not ingest on one click either. The rule and
    # the reason live in services/ingest_policy.py; this is one of its two
    # readers, the navbar being the other. First statement in the function on
    # purpose - a guard that runs after the mailbox is opened guards nothing.
    verdict = ingest_verdict()
    if not verdict.allowed:
        logger.warning(
            "Manual ingestion refused: this machine is not the ingester (%s)",
            verdict.reason,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "reason": verdict.reason,
                    "error": (
                        "This machine is not the ingester. Ingestion runs on the "
                        "deployment, which sets AUTO_START_SCHEDULER=true in its "
                        "own .env; a second machine reading the same mailbox "
                        "produces a divergent database and a second Google bill "
                        "per listing."
                    ),
                }
            ),
            409,
        )

    try:
        # Get sync type from request body (support both JSON and form data)
        if request.is_json:
            data = request.get_json() or {}
        else:
            data = request.form.to_dict() or {}
        sync_type = data.get("sync_type", "incremental")

        from config import Config

        target = getattr(Config, "INGESTION_TARGET", "properties")

        if target == "lands":
            from services.imap_service import IMAPService

            service = IMAPService()
            backend_name = "IMAP (lands)"
        else:
            from services.property_imap_service import PropertyIMAPService

            service = PropertyIMAPService()
            backend_name = "IMAP (properties)"

        # Choose appropriate method based on sync type
        if sync_type == "full" and hasattr(service, "run_full_sync"):
            processed_count = service.run_full_sync()
        else:
            # Use regular ingestion for incremental or if full sync not available
            processed_count = service.run_ingestion()

        return jsonify(
            {
                "success": True,
                "processed_count": processed_count,
                "backend": backend_name,
                "sync_type": sync_type,
                "message": f"Successfully processed {processed_count} new properties via {backend_name} ({sync_type} sync)",
            }
        )

    except Exception:
        logger.error("Manual ingestion failed", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/migrate/lands-to-properties", methods=["POST"])
@limiter.limit("2 per 5 minutes")
def migrate_lands_to_properties():
    """Migrate legacy Land records into universal Property records (one-way helper)."""
    try:
        payload = request.get_json(silent=True) or {}
        dry_run = bool(payload.get("dry_run", True))
        limit = payload.get("limit")
        profile_name = payload.get("profile_name")

        from services.land_to_property_migration_service import (
            LandToPropertyMigrationService,
        )

        svc = LandToPropertyMigrationService(profile_name=profile_name)
        result = svc.migrate(dry_run=dry_run, limit=limit)

        return jsonify({"success": True, "result": result})

    except Exception:
        logger.error("Land→Property migration failed", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/analyze/property/<int:land_id>/structured", methods=["POST"])
@limiter.limit("5 per 5 minutes")
def analyze_property_structured(land_id):
    """Analyze property using Anthropic Claude AI with structured 5-block format"""
    try:
        from services.background_jobs import EnqueueOutcomeUnknown

        land = db.get_or_404(Land, land_id)

        # Get existing analysis from request for enrichment
        request_data = request.get_json() if request.is_json else {}
        existing_analysis = request_data.get("existing_analysis")
        is_enrichment = existing_analysis is not None

        def _run():
            # #190 review, blocker 2: the async path runs this closure on a
            # ThreadPoolExecutor thread, inside its own app context -- a
            # different Flask-SQLAlchemy scoped session than the one that
            # loaded `land` above. Mutating that request-session object and
            # committing through this thread's own session does not flush
            # the mutation (the object was never part of this session's unit
            # of work); by the time the request's own session tears down,
            # `land.ai_analysis` was silently never persisted. Capturing only
            # `land_id` and reloading here -- the same pattern
            # `analyze_universal_property_structured` and
            # `generate_openai_structured` already use -- keeps every read
            # and write inside one session.
            land_local = db.session.get(Land, land_id)
            if land_local is None:
                return {"success": False, "error": "Land not found"}

            from services.anthropic_service import get_anthropic_service

            anthropic_service = get_anthropic_service()

            property_data = {
                "id": land_local.id,
                "title": land_local.title,
                "price": float(land_local.price) if land_local.price else None,
                "area": float(land_local.area) if land_local.area else None,
                "municipality": land_local.municipality,
                "land_type": land_local.land_type,
                "score_total": float(land_local.score_total)
                if land_local.score_total
                else None,
                "description": land_local.description,
                "travel_time_nearest_beach": land_local.travel_time_nearest_beach,
                "nearest_beach_name": land_local.nearest_beach_name,
                "travel_time_oviedo": land_local.travel_time_oviedo,
                "travel_time_gijon": land_local.travel_time_gijon,
                "travel_time_airport": land_local.travel_time_airport,
                "infrastructure_basic": land_local.infrastructure_basic or {},
                "existing_analysis": existing_analysis,
            }

            result = anthropic_service.analyze_property_structured(property_data)

            if result and result.get("status") == "success":
                new_analysis = result.get("structured_analysis")
                model_used = result.get("model")

                if is_enrichment and existing_analysis and new_analysis:
                    merged_analysis = dict(existing_analysis)
                    merged_analysis.update(new_analysis)
                    land_local.ai_analysis = merged_analysis
                    final_analysis = merged_analysis
                else:
                    land_local.ai_analysis = new_analysis
                    final_analysis = new_analysis

                # Staged, not committed: _execute_job commits this together
                # with the job's own terminal CAS write, once, so a reap
                # racing this job's execution rolls both back together
                # rather than letting land_local.ai_analysis land after the
                # job has already lost ownership (#190 review round 4,
                # finding 4).

                # _upsert_land_ai_variant is an update-or-insert that
                # recovers from a lost race against the unique constraint
                # migration 017 adds, rather than the old query-then-insert
                # that let two concurrent writers both see "no row" and
                # both insert (#190 review round 3, finding 4). It stages
                # its own write too -- no commit or rollback here either,
                # since its own SAVEPOINT-based recovery already leaves the
                # session valid for whatever failed for a reason other than
                # the recognized race.
                try:
                    _upsert_land_ai_variant(
                        land_id,
                        "claude",
                        model=model_used,
                        analysis=final_analysis,
                        price_at_analysis=land_local.price,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to store Claude analysis variant for land %s: %s",
                        land_id,
                        e,
                    )

                return {
                    "success": True,
                    "analysis": final_analysis,
                    "model": result.get("model"),
                    "is_enrichment": is_enrichment,
                }

            error_msg = (
                result.get("error", "Analysis failed")
                if result
                else "Analysis service unavailable"
            )
            return {
                "success": False,
                "error": error_msg,
                "raw_analysis": result.get("raw_analysis") if result else None,
            }

        # Claude is the only provider this endpoint calls (see the variant
        # it writes above) -- fixed in the key so a resubmit after an
        # interrupted run cannot race a still-active one for the same land
        # (#176 acceptance criterion 4), and so a `?sync=1` call cannot run
        # alongside a live async one for the same land (#190 review round
        # 3, finding 4).
        dedupe_key = f"land_ai_analysis:{land.id}:claude"

        if _should_run_sync():
            from services.background_jobs import JobAlreadyActive

            try:
                job = _run_sync(
                    _run,
                    job_type="land_ai_analysis",
                    meta={"land_id": land.id, "is_enrichment": is_enrichment},
                    dedupe_key=dedupe_key,
                )
            except JobAlreadyActive as exc:
                return _job_already_active_response(exc)

            result = job["result"] if job else None
            if result and result.get("success"):
                return jsonify(result)
            error_msg = (
                (result or {}).get("error")
                or (job or {}).get("error")
                or ("Analysis failed")
            )
            status_code = (
                503
                if "overloaded" in error_msg.lower()
                or "temporarily" in error_msg.lower()
                else 500
            )
            return jsonify(
                result or {"success": False, "error": error_msg}
            ), status_code

        job_id = _enqueue(
            _run,
            job_type="land_ai_analysis",
            meta={"land_id": land.id, "is_enrichment": is_enrichment},
            dedupe_key=dedupe_key,
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "AI analysis queued",
            }
        ), 202

    except HTTPException:
        raise
    except EnqueueOutcomeUnknown as exc:
        logger.error(
            "Enqueue outcome unknown for land %s: %s", land_id, exc, exc_info=True
        )
        return _enqueue_outcome_unknown_response(exc)
    except Exception:
        logger.error(
            "Structured AI analysis failed for land %s", land_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


def _property_structured_analysis_runner(
    property_id: int, provider: str, existing_analysis=None
):
    """The closure that runs one structured AI analysis, as a job body.

    Module level rather than nested in the route since #434, because the
    enrichment job enqueues this same work as its sequel and a closure defined
    inside a request handler cannot be reached from another job. Nothing about
    the body changed: `existing_analysis` is the merge base the caller passed
    (a re-run enriching an earlier answer), and `None` -- the enrichment
    sequel's case -- means there is nothing to merge with.
    """
    from models import Property
    from services.property_ai_service import PropertyAIService

    is_enrichment = existing_analysis is not None

    def _run():
        prop_local = db.session.get(Property, property_id)
        if not prop_local:
            return {"success": False, "error": "Property not found"}

        service = PropertyAIService()
        result = service.analyze_property_structured(prop_local, provider=provider)

        if not result or result.get("status") != "success":
            return {
                "success": False,
                "error": (result.get("error") if isinstance(result, dict) else None)
                or "Analysis failed",
                "failure_kind": result.get("failure_kind")
                if isinstance(result, dict)
                else None,
            }

        new_analysis = result.get("structured_analysis") or {}

        final = new_analysis
        if (
            is_enrichment
            and isinstance(existing_analysis, dict)
            and isinstance(new_analysis, dict)
        ):
            merged = dict(existing_analysis)
            merged.update(new_analysis)
            final = merged

        # Claude remains the primary analysis stored on the Property
        # record itself (legacy parity). Staged, not committed: the
        # variant upsert below has its own SAVEPOINT-scoped recovery
        # now (#190 review round 4, finding 4), so it no longer needs
        # this change committed first to protect it from its own
        # rollback -- _execute_job commits this together with the
        # job's own terminal CAS write, once, so a reap racing this
        # job's execution rolls both back together.
        if provider == "claude":
            prop_local.ai_analysis = final

        # Store per-provider analysis for side-by-side comparison in the
        # UI. _upsert_property_ai_variant is an update-or-insert that
        # recovers from a lost race against the unique constraint
        # migration 017 adds, rather than the old query-then-insert that
        # let two concurrent writers both see "no row" and both insert.
        # It stages its own write too -- no commit or rollback here
        # either, since its own SAVEPOINT-based recovery already leaves
        # the session valid for whatever failed for a reason other than
        # the recognized race.
        try:
            _upsert_property_ai_variant(
                property_id,
                provider,
                model=result.get("model"),
                analysis=final,
                price_at_analysis=prop_local.price,
            )
        except Exception as e:
            logger.warning(
                "Failed to store AI analysis variant for property %s (%s): %s",
                property_id,
                provider,
                e,
            )

        return {
            "success": True,
            "analysis": final,
            "provider": provider,
            "model": result.get("model"),
            "is_enrichment": is_enrichment,
        }

    return _run


def _property_analysis_providers():
    """The providers an Enrich press should ask, in the page's own order.

    Claude always; ChatGPT only when the bridge is configured, which is the
    same condition `routes/main_routes.py` passes to the template as
    `openai_configured` and the page reads as `window.__OPENAI_CONFIGURED__`.
    Read here rather than sent by the client: a client that says which
    providers to bill is a client that can ask for one that is not there.
    """
    from config import Config

    providers = ["claude"]
    if bool(getattr(Config, "AI_BRIDGE_TOKEN", None)):
        providers.append("openai")
    return providers


def _enqueue_property_analyses(property_id: int) -> None:
    """Queue the AI analyses that follow an Enrich press (#434).

    Called from inside the enrichment job, so the sequel survives the tab that
    started it. Every failure is swallowed and logged: the enrichment has
    already run, and reporting it as failed because a follow-up could not be
    queued would send the owner to press it -- and pay for it -- again (#178).
    """
    for provider in _property_analysis_providers():
        try:
            _enqueue(
                _property_structured_analysis_runner(property_id, provider),
                job_type="property_ai_analysis",
                meta={"property_id": property_id, "provider": provider},
                # The key the analysis endpoint already uses, so the page's own
                # POST for the same pair returns this job's id and attaches to
                # it instead of starting a second, paid run.
                dedupe_key=f"property_ai_analysis:{property_id}:{provider}",
            )
        except Exception:
            logger.warning(
                "Could not queue the %s analysis after enriching property %s",
                provider,
                property_id,
                exc_info=True,
            )


@api_bp.route("/property/<int:property_id>/analyze/structured", methods=["POST"])
@limiter.limit("3 per 5 minutes")
def analyze_universal_property_structured(property_id: int):
    """Analyze a universal Property with a category-aware structured JSON schema."""
    try:
        from models import Property
        from services.background_jobs import EnqueueOutcomeUnknown

        prop = db.get_or_404(Property, property_id)

        request_data = request.get_json() if request.is_json else {}
        provider = (
            (request_data.get("provider") or request.args.get("provider") or "claude")
            .strip()
            .lower()
        )
        if provider in {"chatgpt", "gpt", "openai"}:
            provider = "openai"
        else:
            provider = "claude"
        existing_analysis = request_data.get("existing_analysis")

        _run = _property_structured_analysis_runner(
            property_id, provider, existing_analysis
        )

        # The observed #176 failure: a redeploy interrupted this exact job
        # for (property, provider), and resubmitting it must reuse the
        # in-flight run rather than start a second one racing to write the
        # same PropertyAiAnalysisVariant row. Also what keeps a `?sync=1`
        # call from running alongside a live async one for the same pair
        # (#190 review round 3, finding 4).
        dedupe_key = f"property_ai_analysis:{prop.id}:{provider}"

        if _should_run_sync():
            from services.background_jobs import JobAlreadyActive

            try:
                job = _run_sync(
                    _run,
                    job_type="property_ai_analysis",
                    meta={"property_id": prop.id, "provider": provider},
                    dedupe_key=dedupe_key,
                )
            except JobAlreadyActive as exc:
                return _job_already_active_response(exc)

            result = job["result"] if job else None
            if result and result.get("success"):
                return jsonify(result)
            return jsonify(
                result or {"success": False, "error": (job or {}).get("error")}
            ), 500

        job_id = _enqueue(
            _run,
            job_type="property_ai_analysis",
            meta={"property_id": prop.id, "provider": provider},
            dedupe_key=dedupe_key,
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "AI analysis queued",
            }
        ), 202

    except HTTPException:
        raise
    except EnqueueOutcomeUnknown as exc:
        logger.error(
            "Enqueue outcome unknown for property %s: %s",
            property_id,
            exc,
            exc_info=True,
        )
        return _enqueue_outcome_unknown_response(exc)
    except Exception:
        logger.error(
            "Structured AI analysis failed for property %s", property_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/analysis/generate/<int:land_id>/openai", methods=["POST"])
@limiter.limit("3 per 5 minutes")
def generate_openai_structured(land_id):
    """Generate structured AI analysis with OpenAI (ChatGPT) and store it for comparison."""
    try:
        from services.background_jobs import EnqueueOutcomeUnknown
        from services.openai_service import get_openai_service

        land = db.get_or_404(Land, land_id)

        # A press means recompute, here as on the universal path (#206 owner
        # contract, #219). This endpoint used to answer a press with the stored
        # analysis whenever the caller left `force` out — a finished run
        # standing in for a new one, which is the shape the owner ruled out.
        # Joining applies to a run still in flight, never to a finished row.
        # `force` is gone with the branch it guarded; its only caller
        # (`templates/land_detail.html`) always sent `true`, so nothing on the
        # page changes and no extra analysis is paid for.

        def _run():
            land_local = db.session.get(Land, land_id)
            if not land_local:
                return {"success": False, "error": "Land not found"}

            service = get_openai_service()
            result = service.analyze_property_structured(land_local)

            if not result or result.get("status") != "success":
                return {
                    "success": False,
                    "error": (result.get("error") if isinstance(result, dict) else None)
                    or "OpenAI analysis failed",
                    "failure_kind": result.get("failure_kind")
                    if isinstance(result, dict)
                    else None,
                }

            analysis = result.get("structured_analysis") or {}
            model = result.get("model")

            # _upsert_land_ai_variant recovers from a lost insert race
            # against the unique constraint migration 017 adds, rather than
            # the old query-then-insert that let two concurrent writers
            # both see "no row" and both insert (#190 review round 3,
            # finding 4).
            _upsert_land_ai_variant(
                land_id,
                "openai",
                model=model,
                analysis=analysis,
                price_at_analysis=land_local.price,
            )

            return {
                "success": True,
                "analysis": analysis,
                "model": model,
            }

        # Keeps a `?sync=1` call from running alongside a live async one
        # for the same land (#190 review round 3, finding 4).
        dedupe_key = f"land_openai_analysis:{land.id}:openai"

        if _should_run_sync():
            from services.background_jobs import JobAlreadyActive

            try:
                job = _run_sync(
                    _run,
                    job_type="land_openai_analysis",
                    meta={"land_id": land.id},
                    dedupe_key=dedupe_key,
                )
            except JobAlreadyActive as exc:
                return _job_already_active_response(exc)

            result = job["result"] if job else None
            if result and result.get("success"):
                return jsonify(result)
            return jsonify(
                result or {"success": False, "error": (job or {}).get("error")}
            ), 500

        job_id = _enqueue(
            _run,
            job_type="land_openai_analysis",
            meta={"land_id": land.id},
            dedupe_key=dedupe_key,
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "ChatGPT analysis queued",
            }
        ), 202

    except HTTPException:
        raise
    except EnqueueOutcomeUnknown as exc:
        logger.error(
            "Enqueue outcome unknown for land %s: %s", land_id, exc, exc_info=True
        )
        return _enqueue_outcome_unknown_response(exc)
    except Exception:
        logger.error(
            "OpenAI structured analysis failed for land %s", land_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/analysis/compare/<int:land_id>", methods=["GET"])
def compare_ai_analyses(land_id):
    """Return a rubric-based comparison between stored Claude analysis and ChatGPT analysis."""
    try:
        from config import Config
        from utils.analysis_compare import build_comparison

        land = db.get_or_404(Land, land_id)
        claude_analysis = land.ai_analysis

        claude_variant = (
            AiAnalysisVariant.query.filter_by(land_id=land_id, provider="claude")
            .order_by(AiAnalysisVariant.created_at.desc())
            .first()
        )

        openai_variant = (
            AiAnalysisVariant.query.filter_by(land_id=land_id, provider="openai")
            .order_by(AiAnalysisVariant.created_at.desc())
            .first()
        )

        comparison = build_comparison(
            land, claude_analysis, openai_variant.analysis if openai_variant else None
        )

        return jsonify(
            {
                "success": True,
                "land_id": land_id,
                "has_chatgpt": bool(openai_variant),
                "chatgpt_model": openai_variant.model if openai_variant else None,
                "openai_configured": bool(getattr(Config, "AI_BRIDGE_TOKEN", None)),
                "claude_model": (
                    claude_variant.model
                    if claude_variant
                    else getattr(Config, "ANTHROPIC_MODEL", None)
                ),
                "comparison": comparison,
            }
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("AI comparison failed for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/property/<int:property_id>/analysis/compare", methods=["GET"])
def compare_property_ai_analyses(property_id: int):
    """Return a rubric-based comparison between stored Claude analysis and ChatGPT analysis for a Property."""
    try:
        from config import Config
        from models import Property, PropertyAiAnalysisVariant
        from utils.analysis_compare import build_evaluation

        prop = db.get_or_404(Property, property_id)
        claude_analysis = prop.ai_analysis

        claude_variant = (
            PropertyAiAnalysisVariant.query.filter_by(
                property_id=property_id, provider="claude"
            )
            .order_by(PropertyAiAnalysisVariant.created_at.desc())
            .first()
        )

        openai_variant = (
            PropertyAiAnalysisVariant.query.filter_by(
                property_id=property_id, provider="openai"
            )
            .order_by(PropertyAiAnalysisVariant.created_at.desc())
            .first()
        )

        # There is no market model for universal properties yet, so there is no
        # baseline to score against. Say so instead of standing in a row of
        # zeroes: the placeholder baseline scored every provider "0/100 numeric
        # fidelity" and "60/100 overall" on every listing, which told the owner
        # nothing and read as a verdict on the model.
        baseline = {
            "available": False,
            "reason": (
                "No market baseline for universal properties yet: "
                "the rental model only covers land."
            ),
        }

        def _evaluate(analysis):
            return build_evaluation(
                analysis, expected=None, category=prop.property_category
            )

        comparison = {
            "claude": _evaluate(claude_analysis),
            "chatgpt": _evaluate(openai_variant.analysis) if openai_variant else None,
            "expected": None,
            "baseline": baseline,
        }

        return jsonify(
            {
                "success": True,
                "property_id": property_id,
                "property_category": prop.property_category,
                "has_claude": bool(claude_analysis),
                "has_chatgpt": bool(openai_variant),
                # The price each analysis was computed from, and the one the
                # listing carries now: a correction (#220) leaves a stored
                # analysis reasoning about a number that is no longer there,
                # and the page says so rather than presenting it as current
                # (#235). None means the variant predates the column.
                "current_price": float(prop.price) if prop.price is not None else None,
                "claude_price_at_analysis": (
                    float(claude_variant.price_at_analysis)
                    if claude_variant is not None
                    and claude_variant.price_at_analysis is not None
                    else None
                ),
                "chatgpt_price_at_analysis": (
                    float(openai_variant.price_at_analysis)
                    if openai_variant is not None
                    and openai_variant.price_at_analysis is not None
                    else None
                ),
                "chatgpt_model": openai_variant.model if openai_variant else None,
                "openai_configured": bool(getattr(Config, "AI_BRIDGE_TOKEN", None)),
                "claude_model": (
                    claude_variant.model
                    if claude_variant
                    else getattr(Config, "ANTHROPIC_MODEL", None)
                ),
                "comparison": comparison,
            }
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("AI comparison failed for property %s", property_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/enhance/description/<int:land_id>", methods=["POST"])
@limiter.limit("5 per 5 minutes")
def enhance_description(land_id):
    """Enhance property description using AI"""
    try:
        land = db.get_or_404(Land, land_id)

        # Import description service
        from services.description_service import DescriptionService

        description_service = DescriptionService()

        # Prepare property data for context
        property_data = {
            "price": float(land.price) if land.price else None,
            "area": float(land.area) if land.area else None,
            "municipality": land.municipality,
            "land_type": land.land_type,
            "title": land.title,
        }

        # Enhance the description
        result = description_service.enhance_description(
            land.description, property_data
        )

        if result.get("processing_status") in ["success", "fallback"]:
            # Store the enhanced description
            land.enhanced_description = result
            db.session.commit()

            return jsonify(
                {
                    "success": True,
                    "enhanced_description": result.get("enhanced_description"),
                    "original_description": result.get("original_description"),
                    "processing_status": result.get("processing_status"),
                    "key_highlights": result.get("key_highlights", []),
                    "price_info": result.get("price_info", {}),
                }
            )
        else:
            return jsonify(
                {
                    "success": False,
                    "error": result.get("error", "Enhancement failed"),
                    "original_description": land.description,
                }
            ), 500

    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Description enhancement failed for land %s", land_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/description/variants/<int:land_id>", methods=["GET"])
def get_description_variants(land_id):
    """Get both enhanced and original descriptions for a property"""
    try:
        from services.description_service import DescriptionService

        description_service = DescriptionService()

        variants = description_service.get_description_variants(land_id)

        if "error" in variants:
            return jsonify({"success": False, "error": variants["error"]}), 404

        return jsonify({"success": True, **variants})

    except Exception:
        logger.error(
            "Failed to get description variants for land %s", land_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/land/<int:land_id>/environment", methods=["POST"])
def update_environment(land_id):
    """Update environment data for a land property"""
    try:
        land = db.get_or_404(Land, land_id)
        data = request.get_json()

        # Update environment data
        environment = {
            "sea_view": data.get("sea_view", False),
            "mountain_view": data.get("mountain_view", False),
            "forest_view": data.get("forest_view", False),
            "orientation": data.get("orientation", ""),
            "buildable_floors": data.get("buildable_floors", ""),
            "access_type": data.get("access_type", ""),
            "certified_for": data.get("certified_for", ""),
        }

        land.environment = environment
        db.session.commit()

        logger.info(f"Updated environment data for land {land_id}")

        return jsonify(
            {
                "success": True,
                "message": "Environment data updated successfully",
                "environment": environment,
            }
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to update environment for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


def _manual_sea_view_state(raw):
    """Read the sea-view value a human submitted.

    The property form sends one of the four states; the older boolean form (and
    any caller still posting `true`/`false`) is mapped onto the two states a
    person can actually assert by looking at the listing.
    """
    from services import sea_view_service

    if isinstance(raw, str) and raw.strip().lower() in sea_view_service.VALID_STATES:
        return raw.strip().lower()
    if isinstance(raw, bool):
        return sea_view_service.YES if raw else sea_view_service.NO
    return sea_view_service.UNKNOWN


@api_bp.route("/property/<int:property_id>/environment", methods=["POST"])
def update_property_environment(property_id):
    """Update environment data for a universal property."""
    try:
        from datetime import datetime, timezone

        from models import Property

        prop = db.get_or_404(Property, property_id)
        data = request.get_json() or {}

        sea_view_state = _manual_sea_view_state(data.get("sea_view"))
        environment = {
            "sea_view": sea_view_state,
            # A hand-set verdict outranks both models, and saying so is what
            # stops the next backfill from quietly overwriting it.
            "sea_view_detail": {
                "source": "manual",
                "reason": "set by hand",
                "set_at": datetime.now(timezone.utc).isoformat(),
            },
            "mountain_view": bool(data.get("mountain_view", False)),
            "forest_view": bool(data.get("forest_view", False)),
            "orientation": data.get("orientation", ""),
            "buildable_floors": data.get("buildable_floors", ""),
            "access_type": data.get("access_type", ""),
            "certified_for": data.get("certified_for", ""),
        }

        enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
        enrichment = dict(enrichment)
        enrichment["environment"] = environment
        prop.enrichment = enrichment
        db.session.commit()

        logger.info("Updated environment data for property %s", property_id)

        return jsonify(
            {
                "success": True,
                "message": "Environment data updated successfully",
                "environment": environment,
            }
        )
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Failed to update environment for property %s", property_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/analyze/property/<int:land_id>", methods=["POST"])
@limiter.limit("5 per 5 minutes")
def analyze_property_ai(land_id):
    """Analyze property using Anthropic Claude AI"""
    try:
        land = db.get_or_404(Land, land_id)

        # Import Anthropic service
        from services.anthropic_service import get_anthropic_service

        anthropic_service = get_anthropic_service()

        # Prepare property data for analysis
        property_data = {
            "title": land.title,
            "price": land.price,
            "area": land.area,
            "municipality": land.municipality,
            "land_type": land.land_type,
            "score_total": land.score_total,
            "description": land.description,
        }

        # Get AI analysis
        result = anthropic_service.analyze_property(property_data)

        if result and result.get("status") == "success":
            # Store the analysis in ai_analysis field
            land.ai_analysis = result.get("analysis")
            db.session.commit()

            return jsonify(
                {
                    "success": True,
                    "analysis": result.get("analysis"),
                    "model": result.get("model"),
                }
            )
        else:
            error_msg = "Analysis failed"
            if result:
                error_msg = result.get("error", "Analysis failed")
            return jsonify({"success": False, "error": error_msg}), 500

    except HTTPException:
        raise
    except Exception:
        logger.error("AI analysis failed for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/lands")
def get_lands():
    """Get lands with optional filtering and sorting"""
    try:
        # Get query parameters
        sort_by = request.args.get("sort", "score_total")
        sort_order = request.args.get("order", "desc")
        land_type_filter = request.args.get("filter")
        limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
        offset = max(request.args.get("offset", 0, type=int), 0)

        # Build query
        query = Land.query

        # Apply land type filter
        if land_type_filter and land_type_filter in ["developed", "buildable"]:
            query = query.filter(Land.land_type == land_type_filter)

        # Apply sorting
        if hasattr(Land, sort_by):
            sort_column = getattr(Land, sort_by)
            if sort_order == "asc":
                query = query.order_by(sort_column.asc())
            else:
                query = query.order_by(sort_column.desc())

        # Apply pagination
        lands = query.offset(offset).limit(limit).all()

        # Convert to JSON
        lands_data = [land.to_dict() for land in lands]

        return jsonify({"success": True, "count": len(lands_data), "lands": lands_data})

    except Exception:
        logger.error("Failed to get lands", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/lands/<int:land_id>")
def get_land_detail(land_id):
    """Get detailed information about a specific land"""
    try:
        land = db.session.get(Land, land_id)

        if not land:
            return jsonify({"success": False, "error": "Land not found"}), 404

        land_data = land.to_dict()

        # Add score breakdown if available
        if land.environment and "score_breakdown" in land.environment:
            land_data["score_breakdown"] = land.environment["score_breakdown"]

        return jsonify({"success": True, "land": land_data})

    except Exception:
        logger.error("Failed to get land detail %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/properties")
def get_properties():
    """Get universal Properties with filtering and sorting (defaults to the default SearchProfile)."""
    try:
        from models import Property
        from services.population import (
            BASIS_RAW_ROWS,
            Population,
            subscription_mix,
        )
        from services.search_profile_service import SearchProfileService

        # Query params. The raw spelling is kept as well as the coerced one:
        # `type=int` swallows anything that is not a number, so `profile_id=all`
        # -- the spelling that means "every active subscription" on /properties
        # -- silently arrives here as *omission* and is answered with the
        # Default subscription, which holds nothing. Measured 2026-08-19: the
        # bare endpoint and `profile_id=all` both returned `count: 0` while
        # bare /properties showed 461 listings. The contract is deliberately
        # left as it is (decision #410 -- redefining the spelling cannot be
        # done safely); what changes is that the payload now says so
        # (UNIVERSE-001).
        raw_profile_id = request.args.get("profile_id")
        profile_id = request.args.get("profile_id", type=int)
        category_filter = (request.args.get("category") or "").strip()
        subtype_filter = (request.args.get("subtype") or "").strip()
        municipality_filter = (request.args.get("municipality") or "").strip()
        search_query = (request.args.get("search") or "").strip()
        favorites_only = (request.args.get("favorites") or "").strip().lower() in (
            "1",
            "true",
            "on",
            "yes",
        )

        hide_removed_raw = (request.args.get("hide_removed") or "").strip().lower()
        hide_removed = hide_removed_raw not in ("0", "false", "off", "no")

        sort_by = (request.args.get("sort") or "created_at").strip()
        sort_order = (request.args.get("order") or "desc").strip().lower()
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)
        full = (request.args.get("full") or "").strip().lower() in (
            "1",
            "true",
            "on",
            "yes",
        )

        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)

        # Default to the default profile to avoid mixing unrelated searches.
        if not profile_id:
            default_profile = SearchProfileService.get_default_profile(create=True)
            profile_id = default_profile.id if default_profile else None

        query = Property.query
        if profile_id is not None:
            query = query.filter(Property.search_profile_id == profile_id)

        if category_filter:
            if category_filter == "__none__":
                query = query.filter(
                    (Property.property_category.is_(None))
                    | (Property.property_category == "")
                )
            else:
                query = query.filter(Property.property_category == category_filter)
        if subtype_filter:
            if subtype_filter == "__none__":
                query = query.filter(
                    (Property.property_subtype.is_(None))
                    | (Property.property_subtype == "")
                )
            else:
                query = query.filter(Property.property_subtype == subtype_filter)
        if municipality_filter:
            query = query.filter(municipality_filter_clause(municipality_filter))
        source_clause = source_filter_clause(Property, request.args.get("source", ""))
        if source_clause is not None:
            query = query.filter(source_clause)
        # Who is selling -- the same clause the page and the CSV use.
        advertiser_clause = advertiser.filter_clause(
            Property, request.args.get("advertiser", "")
        )
        if advertiser_clause is not None:
            query = query.filter(advertiser_clause)
        # A pasted listing URL, or a bare listing id, is a search too --
        # utils/listing_search.py owns what the box accepts.
        search_clause = listing_search_clause(Property, search_query)
        if search_clause is not None:
            query = query.filter(search_clause)

        if favorites_only:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed:
            query = query.filter(Property.listing_status.notin_(["removed", "sold"]))

        # Sorting allow-list
        sort_columns = {
            "created_at": Property.created_at,
            "updated_at": Property.updated_at,
            "price": Property.price,
            "area": Property.area,
            "score_total": Property.score_total,
            "score_investment": Property.score_investment,
            "score_lifestyle": Property.score_lifestyle,
        }
        sort_column = sort_columns.get(sort_by, Property.created_at)
        if sort_order == "asc":
            query = query.order_by(sort_column.asc().nullslast())
        else:
            query = query.order_by(sort_column.desc().nullslast())

        # The population, before the page is cut out of it. `count` below is
        # the size of the page and cannot express the set it came from -- with
        # the default limit of 100 a caller reading `count` learns the page
        # size and nothing about the answer (UNIVERSE-001).
        total = query.order_by(None).count()
        # Which subscriptions that population is made of, over the whole of it
        # rather than over the page: a mix tallied from `props` would describe
        # the first 100 rows and wear the name of the answer.
        mix = subscription_mix(
            {
                profile: count
                for profile, count in query.order_by(None)
                .with_entities(Property.search_profile_id, func.count(Property.id))
                .group_by(Property.search_profile_id)
                .all()
            }
        )

        props = query.offset(offset).limit(limit).all()

        if full:
            properties_data = [p.to_dict() for p in props]
        else:
            properties_data = []
            for p in props:
                properties_data.append(
                    {
                        "id": p.id,
                        "search_profile_id": p.search_profile_id,
                        "title": p.title,
                        "url": p.url,
                        "price": float(p.price) if p.price else None,
                        "area": float(p.area) if p.area else None,
                        "municipality": p.municipality,
                        "property_category": p.property_category,
                        "property_subtype": p.property_subtype,
                        "score_total": float(p.score_total) if p.score_total else None,
                        "score_investment": float(p.score_investment)
                        if p.score_investment
                        else None,
                        "score_lifestyle": float(p.score_lifestyle)
                        if p.score_lifestyle
                        else None,
                        "is_favorite": bool(p.is_favorite),
                        # Both, for the reason `to_dict` carries both: the raw
                        # column is 'active' by default and nobody verified that
                        # default, so a consumer reading it alone cannot tell a
                        # live listing from a never-checked one.
                        "listing_status": p.listing_status or "active",
                        "listing_status_verdict": listing_verdict(p)["state"],
                        "created_at": p.created_at.isoformat()
                        if p.created_at
                        else None,
                        "updated_at": p.updated_at.isoformat()
                        if p.updated_at
                        else None,
                    }
                )

        # Three states, not two. "Nothing was sent" and "something was sent
        # that could not be read" are the distinction #410 is about, and a
        # single boolean collapses them back together.
        if request.args.get("profile_id", type=int) is not None:
            profile_id_source = "requested"
        elif raw_profile_id is None or not raw_profile_id.strip():
            profile_id_source = "omitted"
        else:
            profile_id_source = "unrecognized"

        notes = []
        if profile_id_source == "unrecognized":
            notes.append(
                f"profile_id={raw_profile_id!r} is not a subscription id; it was "
                "ignored and the default subscription was used instead. This "
                "endpoint takes one integer id -- there is no spelling here for "
                "'every subscription'."
            )
        elif (
            profile_id_source == "requested"
            and str(profile_id) != str(raw_profile_id).strip()
        ):
            # A number that parsed and was still replaced: `if not profile_id`
            # is falsy for `0`, so `profile_id=0` reaches the same substitution
            # as `all` and used to arrive labelled `requested` with no note --
            # indistinguishable from a request for a subscription that really
            # is the default. `/properties` refuses such an id outright rather
            # than falling back, "because falling back would quietly answer a
            # different question and look like a working filter"
            # (services/profile_selection.py); this endpoint keeps its
            # fallback and says so.
            notes.append(
                f"profile_id={raw_profile_id!r} names no subscription; the "
                f"default subscription ({profile_id}) was used instead."
            )
        population = Population(
            label="one_subscription"
            if profile_id is not None
            else "every_subscription",
            total=total,
            returned=len(properties_data),
            cap=limit,
            basis=BASIS_RAW_ROWS,
            subscriptions=mix,
            notes=tuple(notes),
        )

        return jsonify(
            {
                "success": True,
                # Kept as it was -- the size of the page. `scope.total` is the
                # size of the answer.
                "count": len(properties_data),
                "selected_profile_id": profile_id,
                "scope": {
                    **population.as_dict(),
                    "profile_id_requested": raw_profile_id,
                    "profile_id_applied": profile_id,
                    "profile_id_source": profile_id_source,
                    # `offset` only: the page size is already here as `cap`,
                    # and one fact under two names in one object is what this
                    # block exists to stop happening between objects.
                    "offset": offset,
                },
                "properties": properties_data,
            }
        )

    except Exception:
        logger.error("Failed to get properties", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/properties/<int:property_id>")
def get_property_detail(property_id: int):
    """Get detailed information about a specific universal Property."""
    try:
        from models import Property

        prop = db.session.get(Property, property_id)
        if not prop:
            return jsonify({"success": False, "error": "Property not found"}), 404

        return jsonify({"success": True, "property": prop.to_dict()})

    except Exception:
        logger.error("Failed to get property detail %s", property_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/criteria")
def get_criteria():
    """Get current scoring criteria weights"""
    try:
        from services.scoring_service import ScoringService

        scoring_service = ScoringService()
        weights = scoring_service.get_current_weights()

        return jsonify({"success": True, "criteria": weights})

    except Exception:
        logger.error("Failed to get criteria", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/criteria", methods=["PUT"])
@limiter.limit("10 per minute")
def update_criteria():
    """Update scoring criteria weights"""
    try:
        from werkzeug.exceptions import BadRequest

        try:
            data = request.get_json()
        except BadRequest:
            return jsonify({"success": False, "error": "Invalid JSON payload"}), 400

        if not data or "criteria" not in data:
            return jsonify({"success": False, "error": "Missing criteria data"}), 400

        weights = data["criteria"]

        # Validate names and weights at the JSON boundary too. The service
        # repeats the name check before routing this shared update to both real
        # scoring profiles; mix names remain reserved for their form endpoint.
        from services.scoring_service import known_criteria_names

        valid_criteria = known_criteria_names()

        for criteria_name, weight in weights.items():
            if criteria_name not in valid_criteria:
                return jsonify(
                    {
                        "success": False,
                        "error": f"Unknown scoring criterion: {criteria_name}",
                    }
                ), 400
            if not isinstance(weight, (int, float)) or weight < 0 or weight > 1:
                return jsonify(
                    {
                        "success": False,
                        "error": f"Invalid weight for {criteria_name}: must be a positive number between 0 and 1",
                    }
                ), 400

        # Update weights
        from services.scoring_service import ScoringService

        scoring_service = ScoringService()

        if scoring_service.update_weights(weights):
            return jsonify(
                {
                    "success": True,
                    "message": "Criteria updated successfully and all lands rescored",
                }
            )
        else:
            return jsonify(
                {"success": False, "error": "Failed to update criteria"}
            ), 500

    except Exception:
        logger.error("Failed to update criteria", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/scheduler/status")
def scheduler_status():
    """Get scheduler status"""
    try:
        from services.scheduler_service import get_scheduler_status

        status = get_scheduler_status()

        return jsonify({"success": True, "scheduler": status})

    except Exception:
        logger.error("Failed to get scheduler status", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/land/<int:land_id>/favorite", methods=["POST"])
# Favorites are user-facing UI actions and should work without admin auth.
@limiter.limit("60 per minute")
def toggle_favorite(land_id):
    """Toggle favorite status for a land property"""
    try:
        land = db.get_or_404(Land, land_id)

        # Toggle the favorite status
        was_favorite = land.is_favorite
        land.is_favorite = not land.is_favorite

        # Create history snapshot when adding to favorites
        if land.is_favorite and not was_favorite:
            snapshot = LandHistory.create_snapshot(land, "added_to_favorites")
            db.session.add(snapshot)
            logger.info(f"Created initial snapshot for land {land_id}")

        db.session.commit()

        logger.info(f"Toggled favorite for land {land_id}: {land.is_favorite}")

        return jsonify(
            {
                "success": True,
                "is_favorite": land.is_favorite,
                "message": f"Property {'added to' if land.is_favorite else 'removed from'} favorites",
            }
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to toggle favorite for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/property/<int:property_id>/favorite", methods=["POST"])
# Favorites are user-facing UI actions and should work without admin auth.
@limiter.limit("60 per minute")
def toggle_property_favorite(property_id):
    """Toggle favorite status for a universal Property."""
    try:
        from models import Property

        prop = db.get_or_404(Property, property_id)
        prop.is_favorite = not bool(prop.is_favorite)
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "is_favorite": prop.is_favorite,
                "message": f"Property {'added to' if prop.is_favorite else 'removed from'} favorites",
            }
        )
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Failed to toggle favorite for property %s", property_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/property/<int:property_id>/enrich", methods=["POST"])
@limiter.limit("10 per minute")
def manual_property_enrichment(property_id: int):
    """Manually trigger Google enrichment for a universal Property."""
    try:
        from models import Property
        from services.property_enrichment_service import PropertyEnrichmentService
        from services.property_travel_service import (
            TRAVEL_STATE_APPROXIMATE_ORIGIN,
            TRAVEL_STATE_UNAVAILABLE,
            travel_api_state,
        )

        prop = db.get_or_404(Property, property_id)

        payload = request.get_json(silent=True) if request.is_json else {}
        refresh_coords = False
        if isinstance(payload, dict):
            refresh_coords = bool(payload.get("refresh_coords"))
        if request.args.get("refresh_coords") in ("1", "true", "yes", "on"):
            refresh_coords = True

        # Whether the AI analyses are part of what was pressed (#434).
        #
        # The property page's one button has always meant "enrich, then Claude
        # and ChatGPT" -- but only as a promise chain in the tab: the server
        # knew nothing about the sequel, so a reload, a closed tab or a poll
        # that timed out ended it. On property 793 no `property_ai_analysis`
        # row was ever created, because each re-press reloaded the page and
        # killed the previous chain before it reached the AI step.
        #
        # Asked for by the caller rather than assumed, so this endpoint keeps
        # meaning exactly what it says for anything that only wants the
        # enrichment. The page says so once, up front, instead of driving the
        # sequence step by step.
        #
        # **Read from the JSON body only, unlike `refresh_coords` above.** The
        # JSON API blueprints are CSRF-exempt and there is no authentication
        # (owner decision 2026-08-08), so the only thing standing between a
        # page on another origin and this endpoint is that a simple form POST
        # cannot set `Content-Type: application/json` and anything that can
        # takes a CORS preflight this app does not answer. A `?analyze=1` in
        # the query string would hand that page the AI spend as well, which is
        # a wider surface than the ticket asked for and buys nothing: the one
        # caller posts JSON.
        analyze = bool(payload.get("analyze")) if isinstance(payload, dict) else False

        def _run():
            prop_local = db.session.get(Property, property_id)
            if not prop_local:
                return {"success": False, "error": "Property not found"}

            ok = PropertyEnrichmentService().enrich_property(
                prop_local,
                refresh_coords=refresh_coords,
                recalc_scoring=True,
            )

            # The sequel, queued by the server on the numbers it just wrote.
            # Deliberately not conditional on `ok`: the page runs the analyses
            # either way, and a listing Google could not place is still worth
            # an opinion on.
            if analyze:
                _enqueue_property_analyses(property_id)

            if ok:
                return {
                    "success": True,
                    "message": "Property enriched successfully with Google API data",
                }

            # Neither a refused API nor a bad address: the address resolved,
            # to the middle of the locality. Pressing Enrich again buys
            # nothing, so the message names the repair instead of a retry.
            if travel_api_state(prop_local) == TRAVEL_STATE_APPROXIMATE_ORIGIN:
                return {
                    "success": False,
                    "error": (
                        "This listing's coordinate is a locality centroid, not its "
                        "address, so travel times were not measured — they would "
                        "describe that point, not the property. Re-geocode it "
                        "first (utils/refresh_property_accuracy.py)."
                    ),
                }

            # A refused API is not a bad address: say which one it was (#98).
            if travel_api_state(prop_local) == TRAVEL_STATE_UNAVAILABLE:
                return {
                    "success": False,
                    "error": (
                        "Google refused every travel request; no data was stored. "
                        "Check the API keys, billing and enabled APIs, then retry."
                    ),
                }

            return {
                "success": False,
                "error": "Geocoding failed; enrichment skipped. Check that the property has a valid location.",
            }

        # `allow_request_override=False`, the way #136 closed the same hatch on
        # the two status endpoints below. This chain makes up to eleven Overpass
        # round trips, and on 2026-08-17 -- when the endpoint stopped opening
        # sockets at all -- each one cost four attempts against three instances
        # at a 60 s connect timeout. `?sync=1` is the one path that still spends
        # that inside the request, and the API is unauthenticated.
        # #438 closed `?sync=1` at this call site; what is left of the inline
        # path is `TESTING`, and it goes through the registry so it claims the
        # same slot rather than running a second execution alongside a live
        # async one (#190 review round 3, finding 4, in a second place).
        if _should_run_sync(allow_request_override=False):
            from services.background_jobs import JobAlreadyActive

            try:
                job = _run_sync(
                    _run,
                    job_type="property_enrich",
                    meta={
                        "property_id": prop.id,
                        "refresh_coords": refresh_coords,
                        "analyze": analyze,
                    },
                    dedupe_key=f"property_enrich:{prop.id}",
                )
            except JobAlreadyActive as exc:
                return _job_already_active_response(exc)
            result = (job or {}).get("result")
            if result is not None:
                # `_run` answers for itself, including its own `success: False`
                # for a listing the geocoder could not place -- that is a
                # completed run reporting a measured outcome, and it kept its
                # 200 before this path went through the job registry.
                return jsonify(result), 200
            return jsonify(
                {
                    "success": False,
                    "error": (job or {}).get("error")
                    or "An internal error occurred. Check server logs for details.",
                }
            ), 500

        job_id = _enqueue(
            _run,
            job_type="property_enrich",
            meta={
                "property_id": prop.id,
                "refresh_coords": refresh_coords,
                "analyze": analyze,
            },
            # An impatient second press joins the run already in flight instead
            # of claiming another of the four executor slots -- the shape the
            # fotocasa import already uses. Keyed on the property alone: a
            # second press asking for `refresh_coords` joins a run that did not,
            # which is the right trade for a re-click and is said out loud in
            # the response rather than left to be discovered.
            dedupe_key=f"property_enrich:{prop.id}",
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                # What the server itself allows this to take, so the page does
                # not have to guess. `static/js/main.js` carried a 300 000 ms
                # guess that predated the three-instance Overpass fallback and
                # was therefore shorter than the work -- which is #178's defect
                # exactly: a running job announced as a failure, and the
                # obvious next move pays for it again.
                "poll_timeout_ms": poll_timeout_ms(),
                "message": "Enrichment queued",
            }
        ), 202
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "Manual property enrichment failed for property %s",
            property_id,
            exc_info=True,
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/land/<int:land_id>/set-status", methods=["POST"])
def set_land_status(land_id):
    """Manually set the listing status (for when automatic check fails due to captcha)"""
    try:
        from datetime import datetime

        land = db.get_or_404(Land, land_id)
        data = request.get_json() or {}

        new_status = data.get("status", "removed")
        if new_status not in ("active", "removed", "sold"):
            return jsonify(
                {
                    "success": False,
                    "error": "Invalid status. Must be 'active', 'removed', or 'sold'",
                }
            ), 400

        old_status = land.listing_status
        land.listing_status = new_status
        # Deliberately NOT listing_last_checked: nobody read the listing page.
        # Stamping it here is what made the header say "Checked: today" about a
        # status somebody typed -- the false confirmation of issue #136, in the
        # one path that cannot even claim a fetch happened.
        land.listing_status_source = "manual"

        if new_status in ("removed", "sold") and old_status == "active":
            land.listing_removed_date = datetime.now(timezone.utc)

            # Create history record for favorites
            if land.is_favorite:
                snapshot = LandHistory.create_snapshot(land, "removed_from_listing")
                db.session.add(snapshot)

        elif new_status == "active" and old_status in ("removed", "sold"):
            land.listing_removed_date = None

        db.session.commit()

        return jsonify(
            {
                "success": True,
                "land_id": land_id,
                "status": land.listing_status,
                "status_source": land.listing_status_source,
                "previous_status": old_status,
            }
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to set status for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/property/<int:property_id>/set-status", methods=["POST"])
def set_property_status(property_id):
    """Manually set listing status for a universal Property."""
    try:
        from datetime import datetime
        from models import Property

        prop = db.get_or_404(Property, property_id)
        data = request.get_json() or {}

        new_status = data.get("status", "removed")
        if new_status not in ("active", "removed", "sold"):
            return jsonify(
                {
                    "success": False,
                    "error": "Invalid status. Must be 'active', 'removed', or 'sold'",
                }
            ), 400

        old_status = prop.listing_status or "active"
        prop.listing_status = new_status
        # Deliberately NOT listing_last_checked -- see set_land_status above.
        prop.listing_status_source = "manual"

        if new_status in ("removed", "sold") and old_status == "active":
            prop.listing_removed_date = datetime.now(timezone.utc)
        elif new_status == "active" and old_status in ("removed", "sold"):
            prop.listing_removed_date = None

        db.session.commit()

        return jsonify(
            {
                "success": True,
                "property_id": property_id,
                "status": prop.listing_status,
                "status_source": prop.listing_status_source,
            }
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to set status for property %s", property_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/land/<int:land_id>/check-status", methods=["POST"])
@limiter.limit("5 per minute")
def check_land_status(land_id):
    """Check if a listing is still active on Idealista.

    Rate-limited and queue-only, for the same two reasons its Property twin
    below is (#136, #258): every call spends one outbound request against
    idealista from a page the owner can hold down, and `?sync=1` would hold a
    worker for the whole outbound timeout — a handful of concurrent calls is
    all it takes to exhaust the pool of an app that has no authentication in
    front of it. The scraper's own throttle stays where it is; this keeps the
    endpoint from being a way around it.
    """
    try:
        from services.listing_status_service import ListingStatusService

        land = db.get_or_404(Land, land_id)

        if not land.url:
            return jsonify(
                {"success": False, "error": "No URL available for this listing"}
            ), 400

        def _run():
            land_local = db.session.get(Land, land_id)
            if not land_local or not land_local.url:
                return {"success": False, "error": "No URL available for this listing"}

            service = ListingStatusService()
            result = service.check_land_status(land_local)

            return {
                "success": True,
                "land_id": land_id,
                "status": land_local.listing_status,
                # The observed answer, which is not the stored status when the
                # fetch was blocked or failed: that case reports "error" here
                # while `status` keeps whatever we already knew. Same contract
                # as the property endpoint (issue #136) -- without it the page
                # reads success:true and says "no change" for a check that
                # never reached the listing.
                "observed": result.get("new_status"),
                # Why the check learned nothing, when it learned nothing.
                # 'blocked' and 'backing_off' are idealista refusing this
                # machine, a standing condition the reader can act on; a
                # 'timeout' is a bad moment they can retry. Without the
                # distinction every refusal reads as a fault in here.
                "refusal": result.get("refusal"),
                "previous_status": result.get("previous_status"),
                "changed": result.get("changed", False),
                "last_checked": land_local.listing_last_checked.isoformat()
                if land_local.listing_last_checked
                else None,
                "removed_date": land_local.listing_removed_date.isoformat()
                if land_local.listing_removed_date
                else None,
            }

        if _should_run_sync(allow_request_override=False):
            return jsonify(_run())

        job_id = _enqueue(_run, job_type="land_check_status", meta={"land_id": land.id})
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Listing status check queued",
            }
        ), 202

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to check status for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/property/<int:property_id>/check-status", methods=["POST"])
@limiter.limit("5 per minute")
def check_property_status(property_id):
    """Check whether a universal Property listing is still live on Idealista.

    Rate-limited on purpose: this endpoint is unauthenticated and CSRF-exempt
    like the rest of the JSON API, and every call spends one outbound request
    on idealista. Without a cap, a page left in a loop would drive the scraper
    straight past the throttle the lands sweep is careful to respect.

    It also refuses `?sync=1`. The fetch can hold a worker for its full 15s
    timeout, and the per-IP limit bounds the rate but not the concurrency: five
    simultaneous calls would tie up five workers while idealista stalls.
    """
    try:
        from models import Property
        from services.listing_status_service import ListingStatusService

        prop = db.get_or_404(Property, property_id)

        if not prop.url:
            return jsonify(
                {"success": False, "error": "No URL available for this listing"}
            ), 400

        def _run():
            prop_local = db.session.get(Property, property_id)
            if not prop_local or not prop_local.url:
                return {"success": False, "error": "No URL available for this listing"}

            service = ListingStatusService()
            result = service.check_property_status(prop_local)

            return {
                "success": True,
                "property_id": property_id,
                "status": prop_local.listing_status,
                # The observed answer, which is not the stored status when the
                # fetch was blocked or failed: that case reports "error" here
                # while `status` keeps whatever we already knew.
                "observed": result.get("new_status"),
                # Why the check learned nothing, when it learned nothing.
                # 'blocked' and 'backing_off' are idealista refusing this
                # machine, a standing condition the reader can act on; a
                # 'timeout' is a bad moment they can retry. Without the
                # distinction every refusal reads as a fault in here.
                "refusal": result.get("refusal"),
                # The standing condition itself: how many refusals in a row and
                # until when the service has stopped dialling. A reader pressing
                # the button on the tenth listing should learn that the wall is
                # the site, not this listing.
                "breaker": result.get("breaker"),
                "previous_status": result.get("previous_status"),
                "changed": result.get("changed", False),
                "last_checked": prop_local.listing_last_checked.isoformat()
                if prop_local.listing_last_checked
                else None,
                "removed_date": prop_local.listing_removed_date.isoformat()
                if prop_local.listing_removed_date
                else None,
            }

        if _should_run_sync(allow_request_override=False):
            return jsonify(_run())

        job_id = _enqueue(
            _run, job_type="property_check_status", meta={"property_id": prop.id}
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Listing status check queued",
            }
        ), 202

    except HTTPException:
        # get_or_404 on an unknown id is an answer, not a server fault; the
        # blanket handler below would turn it into a 500.
        raise
    except Exception:
        logger.error(
            "Failed to check status for property %s", property_id, exc_info=True
        )
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/listings/check-favorites", methods=["POST"])
def check_favorites_status():
    """Check status of all favorite listings"""
    try:
        from services.listing_status_service import ListingStatusService

        limit = request.args.get("limit", 50, type=int)

        def _run():
            service = ListingStatusService()
            results = service.check_favorites_status(limit=limit)
            return {"success": True, **results}

        if _should_run_sync():
            return jsonify(_run())

        job_id = _enqueue(
            _run, job_type="listings_check_favorites", meta={"limit": limit}
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Favorites status check queued",
            }
        ), 202

    except Exception:
        logger.error("Failed to check favorites status", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/listings/check-all", methods=["POST"])
def check_all_listings_status():
    """Check status of all active listings that need checking"""
    try:
        from services.listing_status_service import ListingStatusService

        limit = request.args.get("limit", 50, type=int)
        days = request.args.get("days", 7, type=int)

        def _run():
            service = ListingStatusService()
            results = service.check_all_active_listings(
                limit=limit, days_since_check=days
            )
            return {"success": True, **results}

        if _should_run_sync():
            return jsonify(_run())

        job_id = _enqueue(
            _run,
            job_type="listings_check_all",
            meta={"limit": limit, "days": days},
        )
        return jsonify(
            {
                "success": True,
                "status": "queued",
                "job_id": job_id,
                "message": "Listings status check queued",
            }
        ), 202

    except Exception:
        logger.error("Failed to check all listings status", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/land/<int:land_id>/history")
def get_land_history(land_id):
    """Get change history for a land property"""
    try:
        land = db.get_or_404(Land, land_id)

        # Get all history records for this land, ordered by date desc
        history = (
            LandHistory.query.filter_by(land_id=land_id)
            .order_by(LandHistory.snapshot_date.desc())
            .all()
        )

        return jsonify(
            {
                "success": True,
                "land_id": land_id,
                "is_favorite": land.is_favorite,
                "history_count": len(history),
                "history": [h.to_dict() for h in history],
            }
        )

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to get history for land %s", land_id, exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500


@api_bp.route("/stats")
def get_stats():
    """Get application statistics"""
    try:
        # Basic statistics
        total_lands = Land.query.count()
        developed_lands = Land.query.filter_by(land_type="developed").count()
        buildable_lands = Land.query.filter_by(land_type="buildable").count()

        # Score statistics
        avg_score = db.session.query(db.func.avg(Land.score_total)).scalar()
        max_score = db.session.query(db.func.max(Land.score_total)).scalar()
        min_score = db.session.query(db.func.min(Land.score_total)).scalar()

        # Municipality distribution
        municipality_stats = (
            db.session.query(Land.municipality, db.func.count(Land.id))
            .group_by(Land.municipality)
            .all()
        )

        municipality_distribution = {
            municipality: count
            for municipality, count in municipality_stats
            if municipality
        }

        # Get last sync information (nulls last to get most recent completed sync)
        last_sync = SyncHistory.query.order_by(
            SyncHistory.completed_at.desc().nullslast()
        ).first()
        last_sync_info = None

        if last_sync:
            # Include a small list of newly added properties for UI linking/highlighting.
            new_lands = []
            try:
                new_count = int(last_sync.new_properties_added or 0)
            except (TypeError, ValueError):
                new_count = 0

            if new_count > 0:
                start_time = last_sync.started_at
                end_time = last_sync.completed_at or last_sync.started_at

                if start_time and end_time:
                    if start_time > end_time:
                        start_time, end_time = end_time, start_time

                    new_lands_query = (
                        Land.query.filter(
                            Land.created_at >= start_time, Land.created_at <= end_time
                        )
                        .order_by(Land.created_at.desc())
                        .limit(new_count)
                    )
                else:
                    new_lands_query = Land.query.order_by(Land.created_at.desc()).limit(
                        new_count
                    )

                new_lands = [
                    {"id": land.id, "title": land.title or f"Land #{land.id}"}
                    for land in new_lands_query
                ]

            last_sync_info = {
                "sync_type": last_sync.sync_type,
                "backend": last_sync.backend,
                "new_properties": last_sync.new_properties_added,
                "price_updated": getattr(last_sync, "price_updated_count", 0) or 0,
                "expired": getattr(last_sync, "expired_count", 0) or 0,
                "total_emails": last_sync.total_emails_found,
                "status": last_sync.status,
                "completed_at": last_sync.completed_at.isoformat()
                if last_sync.completed_at
                else None,
                "duration": last_sync.sync_duration,
                "new_lands": new_lands,
            }

        return jsonify(
            {
                "success": True,
                "stats": {
                    "total_lands": total_lands,
                    "land_types": {
                        "developed": developed_lands,
                        "buildable": buildable_lands,
                    },
                    "scores": {
                        "average": float(avg_score) if avg_score else 0,
                        "maximum": float(max_score) if max_score else 0,
                        "minimum": float(min_score) if min_score else 0,
                    },
                    "municipality_distribution": municipality_distribution,
                    "last_sync": last_sync_info,
                },
            }
        )

    except Exception:
        logger.error("Failed to get stats", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500
