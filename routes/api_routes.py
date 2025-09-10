import logging
import os
from flask import Blueprint, jsonify, request, send_from_directory
from models import Land, ScoringCriteria, SyncHistory
from app import db

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)

@api_bp.route('/healthz')
def health_check():
    """API health check"""
    return jsonify({"ok": True})

@api_bp.route('/download/project')
def download_project():
    """Download project archive"""
    try:
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
        return send_from_directory(static_dir, 'idealista-project.zip', as_attachment=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 404

@api_bp.route('/lands/enrich-all', methods=['POST'])
def bulk_enrichment():
    """Enrich all properties that are missing extended infrastructure or environment data"""
    try:
        from services.enrichment_service import EnrichmentService
        
        # Find properties missing enrichment data
        lands_to_enrich = Land.query.filter(
            (Land.infrastructure_extended.is_(None)) |
            (Land.environment.is_(None)) |
            (Land.infrastructure_extended == {}) |
            (Land.environment == {})
        ).all()
        
        enrichment_service = EnrichmentService()
        success_count = 0
        total_count = len(lands_to_enrich)
        
        for land in lands_to_enrich:
            try:
                if enrichment_service.enrich_land(land.id):
                    success_count += 1
                    logger.info(f"Enriched land {land.id}: {land.title[:50]}...")
            except Exception as e:
                logger.error(f"Failed to enrich land {land.id}: {str(e)}")
                continue
        
        return jsonify({
            "success": True,
            "message": f"Successfully enriched {success_count} out of {total_count} properties",
            "enriched_count": success_count,
            "total_found": total_count
        })
        
    except Exception as e:
        logger.error(f"Bulk enrichment failed: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/land/<int:land_id>/enrich', methods=['POST'])
def manual_enrichment(land_id):
    """Manually trigger data enrichment for a specific property"""
    try:
        from services.enrichment_service import EnrichmentService
        
        land = Land.query.get_or_404(land_id)
        enrichment_service = EnrichmentService()
        
        success = enrichment_service.enrich_land(land_id)
        
        if success:
            return jsonify({
                "success": True,
                "message": f"Property enriched successfully with Google API data"
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to enrich property data"
            }), 500
            
    except Exception as e:
        logger.error(f"Manual enrichment failed for land {land_id}: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/ingest/email/run', methods=['POST'])
def manual_ingestion():
    """Manually trigger email ingestion"""
    try:
        # Get sync type from request body (support both JSON and form data)
        if request.is_json:
            data = request.get_json() or {}
        else:
            data = request.form.to_dict() or {}
        sync_type = data.get('sync_type', 'incremental')
        
        from config import Config
        
        if Config.EMAIL_BACKEND == "imap":
            from services.imap_service import IMAPService
            service = IMAPService()
            backend_name = "IMAP"
        else:
            from services.gmail_service import GmailService
            service = GmailService()
            backend_name = "Gmail API"
        
        # Choose appropriate method based on sync type
        if sync_type == 'full':
            if hasattr(service, 'run_full_sync'):
                processed_count = service.run_full_sync()
            else:
                # Fallback for services without full sync support
                processed_count = service.run_ingestion()
        else:
            processed_count = service.run_ingestion()
        
        return jsonify({
            "success": True,
            "processed_count": processed_count,
            "backend": backend_name,
            "sync_type": sync_type,
            "message": f"Successfully processed {processed_count} new properties via {backend_name} ({sync_type} sync)"
        })
        
    except Exception as e:
        logger.error(f"Manual ingestion failed: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/analyze/property/<int:land_id>/structured', methods=['POST'])
def analyze_property_structured(land_id):
    """Analyze property using Anthropic Claude AI with structured 5-block format"""
    try:
        land = Land.query.get_or_404(land_id)
        
        # Import Anthropic service
        from services.anthropic_service import get_anthropic_service
        anthropic_service = get_anthropic_service()
        
        # Prepare comprehensive property data
        property_data = {
            'title': land.title,
            'price': float(land.price) if land.price else None,
            'area': float(land.area) if land.area else None,
            'municipality': land.municipality,
            'land_type': land.land_type,
            'score_total': float(land.score_total) if land.score_total else None,
            'description': land.description,
            'travel_time_nearest_beach': land.travel_time_nearest_beach,
            'nearest_beach_name': land.nearest_beach_name,
            'travel_time_oviedo': land.travel_time_oviedo,
            'travel_time_gijon': land.travel_time_gijon,
            'travel_time_airport': land.travel_time_airport,
            'infrastructure_basic': land.infrastructure_basic or {}
        }
        
        # Get structured AI analysis
        result = anthropic_service.analyze_property_structured(property_data)
        
        if result and result.get('status') == 'success':
            # Store the structured analysis
            land.ai_analysis = result.get('structured_analysis')
            db.session.commit()
            
            return jsonify({
                "success": True,
                "analysis": result.get('structured_analysis'),
                "model": result.get('model')
            })
        else:
            error_msg = result.get('error', 'Analysis failed') if result else 'Analysis service unavailable'
            status_code = 503 if 'overloaded' in error_msg.lower() or 'temporarily' in error_msg.lower() else 500
            
            return jsonify({
                "success": False,
                "error": error_msg,
                "raw_analysis": result.get('raw_analysis') if result else None
            }), status_code
            
    except Exception as e:
        logger.error(f"Structured AI analysis failed for land {land_id}: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/analyze/property/<int:land_id>', methods=['POST'])
def analyze_property_ai(land_id):
    """Analyze property using Anthropic Claude AI"""
    try:
        land = Land.query.get_or_404(land_id)
        
        # Import Anthropic service
        from services.anthropic_service import get_anthropic_service
        anthropic_service = get_anthropic_service()
        
        # Prepare property data for analysis
        property_data = {
            'title': land.title,
            'price': land.price,
            'area': land.area,
            'municipality': land.municipality,
            'land_type': land.land_type,
            'score_total': land.score_total,
            'description': land.description
        }
        
        # Get AI analysis
        result = anthropic_service.analyze_property(property_data)
        
        if result and result.get('status') == 'success':
            # Store the analysis in ai_analysis field
            land.ai_analysis = result.get('analysis')
            db.session.commit()
            
            return jsonify({
                "success": True,
                "analysis": result.get('analysis'),
                "model": result.get('model')
            })
        else:
            error_msg = 'Analysis failed'
            if result:
                error_msg = result.get('error', 'Analysis failed')
            return jsonify({
                "success": False,
                "error": error_msg
            }), 500
            
    except Exception as e:
        logger.error(f"AI analysis failed for land {land_id}: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/lands')
def get_lands():
    """Get lands with optional filtering and sorting"""
    try:
        # Get query parameters
        sort_by = request.args.get('sort', 'score_total')
        sort_order = request.args.get('order', 'desc')
        land_type_filter = request.args.get('filter')
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        
        # Build query
        query = Land.query
        
        # Apply land type filter
        if land_type_filter and land_type_filter in ['developed', 'buildable']:
            query = query.filter(Land.land_type == land_type_filter)
        
        # Apply sorting
        if hasattr(Land, sort_by):
            sort_column = getattr(Land, sort_by)
            if sort_order == 'asc':
                query = query.order_by(sort_column.asc())
            else:
                query = query.order_by(sort_column.desc())
        
        # Apply pagination
        lands = query.offset(offset).limit(limit).all()
        
        # Convert to JSON
        lands_data = [land.to_dict() for land in lands]
        
        return jsonify({
            "success": True,
            "count": len(lands_data),
            "lands": lands_data
        })
        
    except Exception as e:
        logger.error(f"Failed to get lands: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/lands/<int:land_id>')
def get_land_detail(land_id):
    """Get detailed information about a specific land"""
    try:
        land = Land.query.get(land_id)
        
        if not land:
            return jsonify({
                "success": False,
                "error": "Land not found"
            }), 404
        
        land_data = land.to_dict()
        
        # Add score breakdown if available
        if land.environment and 'score_breakdown' in land.environment:
            land_data['score_breakdown'] = land.environment['score_breakdown']
        
        return jsonify({
            "success": True,
            "land": land_data
        })
        
    except Exception as e:
        logger.error(f"Failed to get land detail {land_id}: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/criteria')
def get_criteria():
    """Get current scoring criteria weights"""
    try:
        from services.scoring_service import ScoringService
        
        scoring_service = ScoringService()
        weights = scoring_service.get_current_weights()
        
        return jsonify({
            "success": True,
            "criteria": weights
        })
        
    except Exception as e:
        logger.error(f"Failed to get criteria: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/criteria', methods=['PUT'])
def update_criteria():
    """Update scoring criteria weights"""
    try:
        data = request.get_json()
        
        if not data or 'criteria' not in data:
            return jsonify({
                "success": False,
                "error": "Missing criteria data"
            }), 400
        
        weights = data['criteria']
        
        # Validate weights
        for criteria_name, weight in weights.items():
            if not isinstance(weight, (int, float)) or weight < 0:
                return jsonify({
                    "success": False,
                    "error": f"Invalid weight for {criteria_name}: must be a positive number"
                }), 400
        
        # Update weights
        from services.scoring_service import ScoringService
        scoring_service = ScoringService()
        
        if scoring_service.update_weights(weights):
            return jsonify({
                "success": True,
                "message": "Criteria updated successfully and all lands rescored"
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to update criteria"
            }), 500
        
    except Exception as e:
        logger.error(f"Failed to update criteria: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/scheduler/status')
def scheduler_status():
    """Get scheduler status"""
    try:
        from services.scheduler_service import get_scheduler_status
        
        status = get_scheduler_status()
        
        return jsonify({
            "success": True,
            "scheduler": status
        })
        
    except Exception as e:
        logger.error(f"Failed to get scheduler status: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@api_bp.route('/stats')
def get_stats():
    """Get application statistics"""
    try:
        # Basic statistics
        total_lands = Land.query.count()
        developed_lands = Land.query.filter_by(land_type='developed').count()
        buildable_lands = Land.query.filter_by(land_type='buildable').count()
        
        # Score statistics
        avg_score = db.session.query(db.func.avg(Land.score_total)).scalar()
        max_score = db.session.query(db.func.max(Land.score_total)).scalar()
        min_score = db.session.query(db.func.min(Land.score_total)).scalar()
        
        # Municipality distribution
        municipality_stats = db.session.query(
            Land.municipality,
            db.func.count(Land.id)
        ).group_by(Land.municipality).all()
        
        municipality_distribution = {
            municipality: count for municipality, count in municipality_stats if municipality
        }
        
        # Get last sync information
        last_sync = SyncHistory.query.order_by(SyncHistory.completed_at.desc()).first()
        last_sync_info = None
        
        if last_sync:
            last_sync_info = {
                "sync_type": last_sync.sync_type,
                "backend": last_sync.backend,
                "new_properties": last_sync.new_properties_added,
                "total_emails": last_sync.total_emails_found,
                "status": last_sync.status,
                "completed_at": last_sync.completed_at.isoformat() if last_sync.completed_at else None,
                "duration": last_sync.sync_duration
            }
        
        return jsonify({
            "success": True,
            "stats": {
                "total_lands": total_lands,
                "land_types": {
                    "developed": developed_lands,
                    "buildable": buildable_lands
                },
                "scores": {
                    "average": float(avg_score) if avg_score else 0,
                    "maximum": float(max_score) if max_score else 0,
                    "minimum": float(min_score) if min_score else 0
                },
                "municipality_distribution": municipality_distribution,
                "last_sync": last_sync_info
            }
        })
        
    except Exception as e:
        logger.error(f"Failed to get stats: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
