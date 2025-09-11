import logging
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import or_, and_
from models import Land, ScoringCriteria
from app import db

logger = logging.getLogger(__name__)

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    """Home page redirects to lands listing"""
    return redirect(url_for('main.lands'))

@main_bp.route('/lands')
def lands():
    """Main lands listing page with filtering and sorting"""
    try:
        # Get query parameters
        mode = request.args.get('mode', 'combined')  # combined, investment, lifestyle
        
        # Smart sorting defaults based on mode
        mode_sort_defaults = {
            'combined': 'score_total',
            'investment': 'score_investment', 
            'lifestyle': 'score_lifestyle'
        }
        default_sort = mode_sort_defaults.get(mode, 'score_total')
        
        sort_by = request.args.get('sort', default_sort)
        sort_order = request.args.get('order', 'desc')
        land_type_filter = request.args.get('land_type', '')
        municipality_filter = request.args.get('municipality', '')
        search_query = request.args.get('search', '')
        sea_view_filter = request.args.get('sea_view', '') == 'on'
        view_type = request.args.get('view_type', 'cards')  # Default to cards
        
        # Pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 25, type=int)
        # Limit per_page to reasonable values
        per_page = min(max(per_page, 10), 100)
        
        # Build query
        query = Land.query
        
        # Apply filters
        if land_type_filter:
            query = query.filter(Land.land_type == land_type_filter)
        
        if municipality_filter:
            query = query.filter(Land.municipality.ilike(f'%{municipality_filter}%'))
        
        if search_query:
            search_pattern = f'%{search_query}%'
            query = query.filter(
                or_(
                    Land.title.ilike(search_pattern),
                    Land.description.ilike(search_pattern),
                    Land.municipality.ilike(search_pattern)
                )
            )
        
        if sea_view_filter:
            query = query.filter(Land.environment['sea_view'].astext == 'true')
        
        # Apply sorting with NULL values last
        if hasattr(Land, sort_by):
            sort_column = getattr(Land, sort_by)
            if sort_order == 'asc':
                # For ascending, NULLs go last
                query = query.order_by(sort_column.asc().nullslast())
            else:
                # For descending (default for score), NULLs go last
                query = query.order_by(sort_column.desc().nullslast())
        
        # Get paginated results
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )
        lands = pagination.items
        
        # Get unique municipalities for filter dropdown
        municipalities = db.session.query(Land.municipality).distinct().filter(
            Land.municipality.isnot(None)
        ).all()
        municipalities = [m[0] for m in municipalities if m[0]]
        municipalities.sort()
        
        return render_template(
            'lands.html',
            lands=lands,
            pagination=pagination,
            municipalities=municipalities,
            current_filters={
                'mode': mode,
                'sort_by': sort_by,
                'order': sort_order,
                'land_type': land_type_filter,
                'municipality': municipality_filter,
                'search': search_query,
                'sea_view': sea_view_filter,
                'view_type': view_type,
                'page': page,
                'per_page': per_page
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to load lands page: {str(e)}")
        flash(f"Error loading lands: {str(e)}", 'error')
        return render_template('lands.html', lands=[], municipalities=[], current_filters={})

@main_bp.route('/lands/<int:land_id>')
def land_detail(land_id):
    """Detailed view of a specific land"""
    try:
        land = Land.query.get_or_404(land_id)
        
        # Normalize property_details to dict format for template compatibility
        from utils.property_data import normalize_property_details
        land.property_details = normalize_property_details(land.property_details)
        
        
        
        # Get score breakdown from environment field
        score_breakdown = {}
        if land.environment and 'score_breakdown' in land.environment:
            score_breakdown = land.environment['score_breakdown']
        
        return render_template(
            'land_detail.html',
            land=land,
            score_breakdown=score_breakdown
        )
        
    except Exception as e:
        logger.error(f"Failed to load land detail {land_id}: {str(e)}")
        flash(f"Error loading land details: {str(e)}", 'error')
        return redirect(url_for('main.lands'))

@main_bp.route('/criteria')
def criteria():
    """Scoring criteria management page"""
    try:
        criteria = ScoringCriteria.query.filter_by(active=True).all()
        
        # If no criteria exist, create defaults
        if not criteria:
            from config import Config
            for criteria_name, weight in Config.DEFAULT_SCORING_WEIGHTS.items():
                criterion = ScoringCriteria()
                criterion.criteria_name = criteria_name
                criterion.weight = weight
                criterion.active = True
                db.session.add(criterion)
            db.session.commit()
            criteria = ScoringCriteria.query.filter_by(active=True).all()
        
        return render_template('criteria.html', criteria=criteria)
        
    except Exception as e:
        logger.error(f"Failed to load criteria page: {str(e)}")
        flash(f"Error loading criteria: {str(e)}", 'error')
        return render_template('criteria.html', criteria=[])

@main_bp.route('/criteria/update', methods=['POST'])
def update_criteria():
    """Update scoring criteria weights"""
    try:
        # Get form data
        weights = {}
        for key, value in request.form.items():
            if key.startswith('weight_'):
                criteria_name = key.replace('weight_', '')
                try:
                    weights[criteria_name] = float(value)
                except ValueError:
                    flash(f"Invalid weight value for {criteria_name}", 'error')
                    return redirect(url_for('main.criteria'))
        
        # Update weights using scoring service
        from services.scoring_service import ScoringService
        scoring_service = ScoringService()
        
        if scoring_service.update_weights(weights):
            flash('Scoring criteria updated successfully. All lands have been rescored.', 'success')
        else:
            flash('Failed to update scoring criteria', 'error')
        
        return redirect(url_for('main.criteria'))
        
    except Exception as e:
        logger.error(f"Failed to update criteria: {str(e)}")
        flash(f"Error updating criteria: {str(e)}", 'error')
        return redirect(url_for('main.criteria'))

@main_bp.route('/land/<int:land_id>/edit-environment', methods=['GET', 'POST'])
def edit_environment(land_id):
    """Edit environment data for a land"""
    try:
        land = Land.query.get_or_404(land_id)
        
        if request.method == 'POST':
            # Update environment data
            environment = {
                'sea_view': request.form.get('sea_view') == 'on',
                'mountain_view': request.form.get('mountain_view') == 'on', 
                'forest': request.form.get('forest') == 'on',
                'orientation': request.form.get('orientation', ''),
                'buildable_floors': request.form.get('buildable_floors', ''),
                'access_type': request.form.get('access_type', ''),
                'certified_for': request.form.get('certified_for', '')
            }
            
            land.environment = environment
            
            # Update property details if provided
            property_details = request.form.get('property_details', '').strip()
            if property_details:
                land.property_details = property_details
            
            db.session.commit()
            flash('Environment data updated successfully', 'success')
            return redirect(url_for('main.land_detail', land_id=land_id))
        
        return render_template('edit_environment.html', land=land)
        
    except Exception as e:
        logger.error(f"Failed to edit environment for land {land_id}: {str(e)}")
        flash(f"Error editing environment: {str(e)}", 'error')
        return redirect(url_for('main.land_detail', land_id=land_id))

@main_bp.route('/land/<int:land_id>/update-score', methods=['POST'])
def update_score(land_id):
    """Update manual score for a land"""
    try:
        land = Land.query.get_or_404(land_id)
        
        # Get the new score from form
        new_score = request.form.get('score')
        if new_score:
            try:
                # Guard against NaN and infinity injection - validate before conversion
                new_score_lower = new_score.lower().strip()
                if new_score_lower in ('nan', 'inf', 'infinity', '-inf', '-infinity'):
                    raise ValueError("NaN and infinity values not allowed")
                
                score_value = float(new_score)
                # Validate score is between 0 and 100
                if 0 <= score_value <= 100:
                    # Convert to Decimal for proper database storage
                    land.score_total = Decimal(str(score_value))
                    db.session.commit()
                    flash(f'Score updated to {score_value:.1f}', 'success')
                else:
                    flash('Score must be between 0 and 100', 'error')
            except ValueError:
                flash('Invalid score value', 'error')
        else:
            flash('Score is required', 'error')
        
        return redirect(url_for('main.land_detail', land_id=land_id))
        
    except Exception as e:
        logger.error(f"Failed to update score for land {land_id}: {str(e)}")
        flash(f"Error updating score: {str(e)}", 'error')
        return redirect(url_for('main.land_detail', land_id=land_id))

@main_bp.route('/export.csv')
def export_csv():
    """Export current land selection to CSV"""
    try:
        from flask import make_response
        from defusedcsv import csv
        import io
        
        # Get same filters as lands page
        mode = request.args.get('mode', 'combined')
        
        # Smart sorting defaults based on mode (same as main lands route)
        mode_sort_defaults = {
            'combined': 'score_total',
            'investment': 'score_investment', 
            'lifestyle': 'score_lifestyle'
        }
        default_sort = mode_sort_defaults.get(mode, 'score_total')
        
        sort_by = request.args.get('sort', default_sort)
        sort_order = request.args.get('order', 'desc')
        land_type_filter = request.args.get('land_type', '')
        municipality_filter = request.args.get('municipality', '')
        search_query = request.args.get('search', '')
        sea_view_filter = request.args.get('sea_view', '') == 'on'
        
        # Build query with same filters
        query = Land.query
        
        if land_type_filter:
            query = query.filter(Land.land_type == land_type_filter)
        
        if municipality_filter:
            query = query.filter(Land.municipality.ilike(f'%{municipality_filter}%'))
        
        if search_query:
            search_pattern = f'%{search_query}%'
            query = query.filter(
                or_(
                    Land.title.ilike(search_pattern),
                    Land.description.ilike(search_pattern),
                    Land.municipality.ilike(search_pattern)
                )
            )
        
        if sea_view_filter:
            query = query.filter(Land.environment['sea_view'].astext == 'true')
        
        # Apply sorting with same logic as main lands route
        if hasattr(Land, sort_by):
            sort_column = getattr(Land, sort_by)
            if sort_order == 'asc':
                # For ascending, NULLs go last
                lands = query.order_by(sort_column.asc().nullslast()).all()
            else:
                # For descending (default for scores), NULLs go last
                lands = query.order_by(sort_column.desc().nullslast()).all()
        else:
            # Fallback to mode default if invalid sort field
            fallback_column = getattr(Land, default_sort)
            if sort_order == 'asc':
                lands = query.order_by(fallback_column.asc().nullslast()).all()
            else:
                lands = query.order_by(fallback_column.desc().nullslast()).all()
        
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header - include dual scores
        writer.writerow([
            'ID', 'Title', 'URL', 'Price (€)', 'Area (m²)', 'Municipality',
            'Land Type', 'Legal Status', 'Score Total', 'Score Investment', 'Score Lifestyle', 
            'Latitude', 'Longitude', 'Created At'
        ])
        
        # Data rows - include dual scores
        for land in lands:
            writer.writerow([
                land.id,
                land.title,
                land.url,
                land.price,
                land.area,
                land.municipality,
                land.land_type,
                land.legal_status,
                land.score_total,
                land.score_investment,
                land.score_lifestyle,
                land.location_lat,
                land.location_lon,
                land.created_at.isoformat() if land.created_at else ''
            ])
        
        # Create response
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=idealista_lands.csv'
        
        return response
        
    except Exception as e:
        logger.error(f"Failed to export CSV: {str(e)}")
        flash(f"Error exporting CSV: {str(e)}", 'error')
        return redirect(url_for('main.lands'))

@main_bp.route('/healthz')
def health_check():
    """Health check endpoint"""
    return jsonify({"ok": True})
