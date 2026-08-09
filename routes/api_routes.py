import logging
from datetime import datetime, timezone
from flask import Blueprint, current_app, jsonify, request

# get_or_404 raises HTTPException, and the blanket `except Exception` handlers
# below would answer 500 for it: every one of them re-raises it first so an
# unknown id stays a 404 (issue #136).
from werkzeug.exceptions import HTTPException
from models import Land, LandHistory, SyncHistory, AiAnalysisVariant
from app import db
from app import limiter

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


def _should_run_sync() -> bool:
    try:
        if current_app and current_app.config.get("TESTING"):
            return True
    except Exception:
        pass
    return request.args.get("sync") in ("1", "true", "yes", "on")


def _enqueue(job_fn, *, job_type: str, meta=None) -> str:
    from services.background_jobs import enqueue_job

    app_obj = current_app._get_current_object()
    return enqueue_job(job_fn, job_type=job_type, meta=meta or {}, app=app_obj)


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
        land = db.get_or_404(Land, land_id)

        # Get existing analysis from request for enrichment
        request_data = request.get_json() if request.is_json else {}
        existing_analysis = request_data.get("existing_analysis")
        is_enrichment = existing_analysis is not None

        def _run():
            from services.anthropic_service import get_anthropic_service

            anthropic_service = get_anthropic_service()

            property_data = {
                "id": land.id,
                "title": land.title,
                "price": float(land.price) if land.price else None,
                "area": float(land.area) if land.area else None,
                "municipality": land.municipality,
                "land_type": land.land_type,
                "score_total": float(land.score_total) if land.score_total else None,
                "description": land.description,
                "travel_time_nearest_beach": land.travel_time_nearest_beach,
                "nearest_beach_name": land.nearest_beach_name,
                "travel_time_oviedo": land.travel_time_oviedo,
                "travel_time_gijon": land.travel_time_gijon,
                "travel_time_airport": land.travel_time_airport,
                "infrastructure_basic": land.infrastructure_basic or {},
                "existing_analysis": existing_analysis,
            }

            result = anthropic_service.analyze_property_structured(property_data)

            if result and result.get("status") == "success":
                new_analysis = result.get("structured_analysis")
                model_used = result.get("model")

                if is_enrichment and existing_analysis and new_analysis:
                    merged_analysis = dict(existing_analysis)
                    merged_analysis.update(new_analysis)
                    land.ai_analysis = merged_analysis
                    final_analysis = merged_analysis
                else:
                    land.ai_analysis = new_analysis
                    final_analysis = new_analysis

                db.session.commit()

                try:
                    existing_variant = (
                        AiAnalysisVariant.query.filter_by(
                            land_id=land_id, provider="claude"
                        )
                        .order_by(AiAnalysisVariant.created_at.desc())
                        .first()
                    )
                    if existing_variant:
                        existing_variant.analysis = final_analysis
                        existing_variant.model = model_used
                        existing_variant.created_at = datetime.now(timezone.utc)
                    else:
                        db.session.add(
                            AiAnalysisVariant(
                                land_id=land_id,
                                provider="claude",
                                model=model_used,
                                analysis=final_analysis,
                            )
                        )
                    db.session.commit()
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

        if _should_run_sync():
            result = _run()
            if result.get("success"):
                return jsonify(result)
            error_msg = result.get("error", "Analysis failed")
            status_code = (
                503
                if "overloaded" in error_msg.lower()
                or "temporarily" in error_msg.lower()
                else 500
            )
            return jsonify(result), status_code

        job_id = _enqueue(
            _run,
            job_type="land_ai_analysis",
            meta={"land_id": land.id, "is_enrichment": is_enrichment},
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


@api_bp.route("/property/<int:property_id>/analyze/structured", methods=["POST"])
@limiter.limit("3 per 5 minutes")
def analyze_universal_property_structured(property_id: int):
    """Analyze a universal Property with a category-aware structured JSON schema."""
    try:
        from datetime import datetime

        from models import Property, PropertyAiAnalysisVariant
        from services.property_ai_service import PropertyAIService

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

            # Claude remains the primary analysis stored on the Property record itself (legacy parity).
            if provider == "claude":
                prop_local.ai_analysis = final

            # Store per-provider analysis for side-by-side comparison in the UI.
            try:
                variant = PropertyAiAnalysisVariant.query.filter_by(
                    property_id=property_id, provider=provider
                ).first()
                if variant:
                    variant.analysis = final
                    variant.model = result.get("model")
                    variant.created_at = datetime.now(timezone.utc)
                else:
                    db.session.add(
                        PropertyAiAnalysisVariant(
                            property_id=property_id,
                            provider=provider,
                            model=result.get("model"),
                            analysis=final,
                        )
                    )
            except Exception as e:
                logger.warning(
                    "Failed to store AI analysis variant for property %s (%s): %s",
                    property_id,
                    provider,
                    e,
                )

            db.session.commit()

            return {
                "success": True,
                "analysis": final,
                "provider": provider,
                "model": result.get("model"),
                "is_enrichment": is_enrichment,
            }

        if _should_run_sync():
            result = _run()
            if result.get("success"):
                return jsonify(result)
            return jsonify(result), 500

        job_id = _enqueue(
            _run,
            job_type="property_ai_analysis",
            meta={"property_id": prop.id, "provider": provider},
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
        from services.openai_service import get_openai_service

        land = db.get_or_404(Land, land_id)

        # Optional: allow overwrite
        request_data = request.get_json() if request.is_json else {}
        force = bool(request_data.get("force"))

        existing = (
            AiAnalysisVariant.query.filter_by(land_id=land_id, provider="openai")
            .order_by(AiAnalysisVariant.created_at.desc())
            .first()
        )
        if existing and not force:
            return jsonify(
                {
                    "success": True,
                    "message": "ChatGPT analysis already exists",
                    "analysis": existing.analysis,
                    "model": existing.model,
                }
            )

        def _run():
            land_local = db.session.get(Land, land_id)
            if not land_local:
                return {"success": False, "error": "Land not found"}

            service = get_openai_service()
            result = service.analyze_property_structured(land_local)

            if not result or result.get("status") != "success":
                return {"success": False, "error": "OpenAI analysis failed"}

            analysis = result.get("structured_analysis") or {}
            model = result.get("model")

            existing_local = (
                AiAnalysisVariant.query.filter_by(land_id=land_id, provider="openai")
                .order_by(AiAnalysisVariant.created_at.desc())
                .first()
            )

            if existing_local:
                existing_local.analysis = analysis
                existing_local.model = model
                existing_local.created_at = datetime.now(timezone.utc)
                variant = existing_local
            else:
                variant = AiAnalysisVariant(
                    land_id=land_id,
                    provider="openai",
                    model=model,
                    analysis=analysis,
                )
                db.session.add(variant)

            db.session.commit()

            return {
                "success": True,
                "analysis": variant.analysis,
                "model": variant.model,
            }

        if _should_run_sync():
            result = _run()
            if result.get("success"):
                return jsonify(result)
            return jsonify(result), 500

        job_id = _enqueue(
            _run,
            job_type="land_openai_analysis",
            meta={"land_id": land.id, "force": force},
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
        from utils.analysis_compare import (
            extract_highlights,
            extract_metrics,
            numeric_fidelity_score,
            overall_score,
            schema_completeness,
        )

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

        # Universal baseline is intentionally light-weight for now (no market model for all property types yet).
        expected = {
            "investment_rating": "BELOW AVERAGE - Consider other options",
            "rental_yield": 0,
            "cap_rate": 0,
            "price_to_rent_ratio": 0,
            "payback_period_years": 0,
        }

        def _evaluate(analysis):
            metrics = extract_metrics(analysis)
            completeness = schema_completeness(analysis)
            fidelity = numeric_fidelity_score(metrics, expected)
            return {
                "metrics": metrics,
                "highlights": extract_highlights(analysis),
                "schema": {"found": completeness[0], "total": completeness[1]},
                "expected": expected,
                "fidelity_score": fidelity,
                "overall_score": overall_score(completeness, fidelity),
            }

        comparison = {
            "claude": _evaluate(claude_analysis),
            "chatgpt": _evaluate(openai_variant.analysis) if openai_variant else None,
            "expected": expected,
        }

        return jsonify(
            {
                "success": True,
                "property_id": property_id,
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
        from services.search_profile_service import SearchProfileService

        # Query params
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
            query = query.filter(
                Property.municipality.ilike(f"%{municipality_filter}%")
            )
        if search_query:
            pattern = f"%{search_query}%"
            query = query.filter(
                (Property.title.ilike(pattern))
                | (Property.description.ilike(pattern))
                | (Property.municipality.ilike(pattern))
            )

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
                        "listing_status": p.listing_status or "active",
                        "created_at": p.created_at.isoformat()
                        if p.created_at
                        else None,
                        "updated_at": p.updated_at.isoformat()
                        if p.updated_at
                        else None,
                    }
                )

        return jsonify(
            {
                "success": True,
                "count": len(properties_data),
                "selected_profile_id": profile_id,
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

        def _run():
            prop_local = db.session.get(Property, property_id)
            if not prop_local:
                return {"success": False, "error": "Property not found"}

            ok = PropertyEnrichmentService().enrich_property(
                prop_local,
                refresh_coords=refresh_coords,
                recalc_scoring=True,
            )
            if ok:
                return {
                    "success": True,
                    "message": "Property enriched successfully with Google API data",
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

        if _should_run_sync():
            result = _run()
            return jsonify(result), 200

        job_id = _enqueue(
            _run,
            job_type="property_enrich",
            meta={"property_id": prop.id, "refresh_coords": refresh_coords},
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
        land.listing_last_checked = datetime.now(timezone.utc)

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
        prop.listing_last_checked = datetime.now(timezone.utc)

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
def check_land_status(land_id):
    """Check if a listing is still active on Idealista"""
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
                "previous_status": result.get("previous_status"),
                "changed": result.get("changed", False),
                "last_checked": land_local.listing_last_checked.isoformat()
                if land_local.listing_last_checked
                else None,
                "removed_date": land_local.listing_removed_date.isoformat()
                if land_local.listing_removed_date
                else None,
            }

        if _should_run_sync():
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
