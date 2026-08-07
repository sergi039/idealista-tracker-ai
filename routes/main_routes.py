import logging
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from sqlalchemy import or_, and_, case, func
from sqlalchemy.orm import defer
from models import Land, Property, ScoringCriteria, SearchProfile
from app import db
from utils.auth import admin_required, login_admin, logout_admin

logger = logging.getLogger(__name__)

main_bp = Blueprint('main', __name__)

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometers."""
    r = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return r * c

@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Admin login page"""
    if session.get('admin_authenticated'):
        return redirect(request.args.get('next') or url_for('main.lands'))

    if request.method == 'POST':
        token = request.form.get('token', '').strip()
        if login_admin(token):
            next_url = request.form.get('next') or url_for('main.lands')
            return redirect(next_url)
        flash('Invalid admin token.', 'error')

    return render_template('login.html', next=request.args.get('next', ''))


@main_bp.route('/logout')
def logout():
    """Log out and clear session"""
    logout_admin()
    return redirect(url_for('main.login'))


@main_bp.route('/')
def index():
    """Home page redirects to the lands listing (the working UI)."""
    return redirect(url_for('main.lands'))

@main_bp.route('/properties')
def properties():
    """Universal properties listing page (new model, migration-in-progress)."""
    try:
        from services.search_profile_service import SearchProfileService
        from utils.auth import check_admin_auth

        is_admin = check_admin_auth()

        profiles = SearchProfileService.list_profiles(active_only=True)
        default_profile = SearchProfileService.get_default_profile(create=True)

        requested_profile_id = request.args.get('profile_id', type=int)
        selected_profile_id = requested_profile_id

        # If the default profile has no properties (common when profiles are auto-created from email search names),
        # auto-select the most recently active profile so the list isn't empty by default.
        if selected_profile_id is None:
            default_has_props = False
            if default_profile:
                default_has_props = (
                    Property.query.filter(Property.search_profile_id == default_profile.id)
                    .limit(1)
                    .first()
                    is not None
                )

            if default_profile and default_has_props:
                selected_profile_id = default_profile.id
            else:
                recent = (
                    db.session.query(Property.search_profile_id, func.max(Property.created_at).label("latest"))
                    .join(SearchProfile, SearchProfile.id == Property.search_profile_id)
                    .filter(SearchProfile.is_active.is_(True))
                    .group_by(Property.search_profile_id)
                    .order_by(func.max(Property.created_at).desc())
                    .first()
                )
                if recent and recent[0] is not None:
                    selected_profile_id = int(recent[0])
                elif default_profile:
                    selected_profile_id = default_profile.id
                elif profiles:
                    selected_profile_id = profiles[0].id
                else:
                    selected_profile_id = None

        # Filters
        category_filter = request.args.get('category', '')
        subtype_filter = request.args.get('subtype', '')
        municipality_filter = request.args.get('municipality', '')
        search_query = request.args.get('search', '')
        favorites_filter = request.args.get('favorites', '') == 'on'

        # Hide removed: ON by default (similar to /lands)
        hide_removed_param = request.args.get('hide_removed', None)
        form_submitted = any(
            request.args.get(p)
            for p in [
                'profile_id',
                'category',
                'subtype',
                'municipality',
                'search',
                'sort',
                'order',
                'favorites',
                'hide_removed',
            ]
        )
        if form_submitted:
            hide_removed_filter = hide_removed_param == 'on'
        else:
            hide_removed_filter = True

        # Sorting
        sort_by = request.args.get('sort', 'created_at')
        sort_order = request.args.get('order', 'desc')

        # Pagination
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 25, type=int)
        per_page = min(max(per_page, 10), 100)

        query = Property.query
        if selected_profile_id is not None:
            query = query.filter(Property.search_profile_id == selected_profile_id)

        if category_filter:
            if category_filter == "__none__":
                query = query.filter(or_(Property.property_category.is_(None), Property.property_category == ""))
            else:
                query = query.filter(Property.property_category == category_filter)
        if subtype_filter:
            if subtype_filter == "__none__":
                query = query.filter(or_(Property.property_subtype.is_(None), Property.property_subtype == ""))
            else:
                query = query.filter(Property.property_subtype == subtype_filter)
        if municipality_filter:
            query = query.filter(Property.municipality.ilike(f'%{municipality_filter}%'))
        if search_query:
            pattern = f'%{search_query}%'
            query = query.filter(
                or_(
                    Property.title.ilike(pattern),
                    Property.description.ilike(pattern),
                    Property.municipality.ilike(pattern),
                )
            )

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed_filter:
            query = query.filter(Property.listing_status.notin_(["removed", "sold"]))

        # Sorting (safe allow-list)
        sort_columns = {
            'title': Property.title,
            'created_at': Property.created_at,
            'price': Property.price,
            'area': Property.area,
            'score_total': Property.score_total,
        }
        sort_column = sort_columns.get(sort_by, Property.created_at)
        if sort_order == 'asc':
            query = query.order_by(sort_column.asc().nullslast())
        else:
            query = query.order_by(sort_column.desc().nullslast())

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Profile-driven travel targets for consistent UI display.
        travel_display_targets = []
        if selected_profile_id is not None:
            try:
                selected_profile = db.session.get(SearchProfile, selected_profile_id)
            except Exception:
                selected_profile = None

            travel_config = SearchProfileService.get_travel_targets_config(selected_profile)
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = travel_config.get("presets") if isinstance(travel_config, dict) else {}

            icon_map = {
                "airport": "fa-plane",
                "train_station": "fa-train",
                "hospital": "fa-hospital",
                "police": "fa-shield-halved",
                "supermarket": "fa-cart-shopping",
                "school": "fa-school",
            }

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = bool((presets_cfg.get(key) or {}).get("enabled", True)) if isinstance(presets_cfg, dict) else True
                if not enabled:
                    continue
                travel_display_targets.append(
                    {
                        "key": key,
                        "label": preset.get("label") or key,
                        "icon": icon_map.get(key) or "fa-route",
                        "kind": "preset",
                    }
                )

            for item in (travel_config.get("custom") or []) if isinstance(travel_config, dict) else []:
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append(
                    {
                        "key": f"custom:{target_id}",
                        "label": name,
                        "icon": "fa-location-dot",
                        "kind": "custom",
                    }
                )

        # Distinct lists for dropdowns (small, best-effort)
        categories = [
            r[0]
            for r in db.session.query(Property.property_category)
            .distinct()
            .filter(Property.property_category.isnot(None))
            .all()
            if r and r[0]
        ]
        categories.sort()

        subtypes = [
            r[0]
            for r in db.session.query(Property.property_subtype)
            .distinct()
            .filter(Property.property_subtype.isnot(None))
            .all()
            if r and r[0]
        ]
        subtypes.sort()

        municipalities = [
            r[0]
            for r in db.session.query(Property.municipality)
            .distinct()
            .filter(Property.municipality.isnot(None))
            .all()
            if r and r[0]
        ]
        municipalities.sort()

        return render_template(
            'properties.html',
            properties=pagination.items,
            pagination=pagination,
            profiles=profiles,
            selected_profile_id=selected_profile_id,
            is_admin=is_admin,
            travel_display_targets=travel_display_targets,
            categories=categories,
            subtypes=subtypes,
            municipalities=municipalities,
            current_filters={
                'profile_id': selected_profile_id,
                'category': category_filter,
                'subtype': subtype_filter,
                'municipality': municipality_filter,
                'search': search_query,
                'favorites': favorites_filter,
                'hide_removed': hide_removed_filter,
                'sort_by': sort_by,
                'order': sort_order,
                'page': page,
                'per_page': per_page,
            },
        )
    except Exception as e:
        logger.error("Failed to load properties page", exc_info=True)
        flash("An error occurred while loading properties. Check server logs.", 'error')
        return render_template('properties.html', properties=[], pagination=None, profiles=[], selected_profile_id=None, categories=[], subtypes=[], municipalities=[], current_filters={})

@main_bp.route('/lands')
def lands():
    """Main lands listing page with filtering and sorting"""
    try:
        from services.settings_service import SettingsService

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
        investment_metrics_filter = request.args.get('inv_metr', '')
        sea_view_filter = request.args.get('sea_view', '') == 'on'
        favorites_filter = request.args.get('favorites', '') == 'on'
        # Hide removed: ON by default. Checkbox sends 'on' when checked, absent when unchecked.
        # We need special handling: if 'hide_removed' param is missing AND no other filters applied, default to True
        # If form was submitted (has any filter param), absence means unchecked (False)
        hide_removed_param = request.args.get('hide_removed', None)
        form_submitted = any(request.args.get(p) for p in ['search', 'land_type', 'municipality', 'inv_metr', 'sea_view', 'favorites', 'sort', 'hide_removed'])
        if form_submitted:
            hide_removed_filter = hide_removed_param == 'on'
        else:
            hide_removed_filter = True  # Default: hide removed
        view_type = request.args.get('view_type', 'cards')  # Default to cards
        
        # Pagination parameters (clamp to valid ranges)
        page = max(1, request.args.get('page', 1, type=int))
        per_page = min(max(request.args.get('per_page', 25, type=int), 10), 100)
        
        # Build query - defer heavy JSONB columns for listing view performance
        query = Land.query.options(
            defer(Land.infrastructure_basic),
            defer(Land.infrastructure_extended),
            defer(Land.transport),
            defer(Land.environment),
            defer(Land.neighborhood),
            defer(Land.services_quality),
            defer(Land.enhanced_description),
            defer(Land.property_details),
            defer(Land.description)
        )
        
        # Apply filters
        if land_type_filter:
            query = query.filter(Land.land_type == land_type_filter)

        if municipality_filter:
            query = query.filter(Land.municipality.ilike(f'%{municipality_filter}%'))

        if search_query:
            # Split search query into words for flexible matching
            # Filter out common words and short terms
            stop_words = {'for', 'in', 'the', 'a', 'an', 'of', 'to', 'and', 'or', 'sale', 'plot'}
            words = [w.strip(',.;:!?()[]{}') for w in search_query.split()]
            words = [w for w in words if w and len(w) > 1 and w.lower() not in stop_words]

            if words:
                # Each word must match at least one field (title, description, or municipality)
                for word in words:
                    word_pattern = f'%{word}%'
                    query = query.filter(
                        or_(
                            Land.title.ilike(word_pattern),
                            Land.description.ilike(word_pattern),
                            Land.municipality.ilike(word_pattern)
                        )
                    )

        if investment_metrics_filter:
            rating_text = func.coalesce(
                Land.ai_analysis['rental_market_analysis']['investment_rating'].as_string(),
                ''
            )
            upper_rating = func.upper(rating_text)
            filter_value = investment_metrics_filter.strip().upper()
            if filter_value == 'EXCELLENT':
                query = query.filter(upper_rating.like('EXCELLENT%'))
            elif filter_value == 'GOOD':
                query = query.filter(upper_rating.like('GOOD%'))
            elif filter_value == 'MODERATE':
                query = query.filter(upper_rating.like('MODERATE%'))
            elif filter_value == 'BELOW':
                query = query.filter(upper_rating.like('BELOW%'))
        
        if sea_view_filter:
            # SQLAlchemy 2.x: .astext removed; use JSON accessors
            query = query.filter(Land.environment['sea_view'].as_boolean().is_(True))

        if favorites_filter:
            query = query.filter(Land.is_favorite == True)

        if hide_removed_filter:
            query = query.filter(
                or_(
                    Land.listing_status == 'active',
                    Land.listing_status.is_(None)
                )
            )

        # Apply sorting with NULL values last
        if sort_by == 'investment_metrics':
            rating_text = func.coalesce(
                Land.ai_analysis['rental_market_analysis']['investment_rating'].as_string(),
                ''
            )
            upper_rating = func.upper(rating_text)
            rank = case(
                (upper_rating.like('EXCELLENT%'), 4),
                (upper_rating.like('GOOD%'), 3),
                (upper_rating.like('MODERATE%'), 2),
                (upper_rating.like('BELOW%'), 1),
                else_=None
            )
            if sort_order == 'asc':
                query = query.order_by(rank.asc().nullslast(), Land.score_total.desc().nullslast())
            else:
                query = query.order_by(rank.desc().nullslast(), Land.score_total.desc().nullslast())
        elif hasattr(Land, sort_by):
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
        
        # Derive active_mode from sort_by for reliable UI state synchronization
        if sort_by in ['score_investment']:
            active_mode = 'investment'
        elif sort_by in ['score_lifestyle']:
            active_mode = 'lifestyle'
        elif sort_by in ['score_total']:
            active_mode = 'combined'
        else:
            # For non-score sorts (price, area, etc.), use explicit mode
            active_mode = mode
        
        # Debug logging (temporary)
        logger.debug(f"UI params mode={mode!r} sort={sort_by!r} -> active_mode={active_mode}")

        reference_cities = []
        try:
            reference_cities = SettingsService.get_reference_cities()
        except Exception:
            reference_cities = []
        
        return render_template(
            'lands.html',
            lands=lands,
            pagination=pagination,
            municipalities=municipalities,
            reference_cities=reference_cities,
                current_filters={
                    'mode': mode,
                    'sort_by': sort_by,
                    'order': sort_order,
                    'land_type': land_type_filter,
                    'municipality': municipality_filter,
                    'search': search_query,
                    'inv_metr': investment_metrics_filter,
                    'sea_view': sea_view_filter,
                    'favorites': favorites_filter,
                    'hide_removed': hide_removed_filter,
                    'active_mode': active_mode,
                    'view_type': view_type,
                    'page': page,
                    'per_page': per_page
                }
        )
        
    except Exception as e:
        logger.error("Failed to load lands page", exc_info=True)
        flash("An error occurred while loading lands. Check server logs.", 'error')
        return render_template('lands.html', lands=[], municipalities=[], current_filters={})

@main_bp.route('/properties/<int:property_id>')
def property_detail(property_id):
    """Detailed view of a specific property (new universal model)."""
    try:
        from utils.auth import check_admin_auth
        from config import Config
        from services.search_profile_service import SearchProfileService

        prop = db.get_or_404(Property, property_id)
        from models import PropertyAiAnalysisVariant

        openai_variant = (
            PropertyAiAnalysisVariant.query.filter_by(property_id=property_id, provider="openai")
            .order_by(PropertyAiAnalysisVariant.created_at.desc())
            .first()
        )

        # Profile-driven travel targets for consistent UI display (sidebar).
        travel_display_targets = []
        try:
            selected_profile = prop.search_profile
            travel_config = SearchProfileService.get_travel_targets_config(selected_profile)
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = travel_config.get("presets") if isinstance(travel_config, dict) else {}

            icon_map = {
                "airport": "fa-plane",
                "train_station": "fa-train",
                "hospital": "fa-hospital",
                "police": "fa-shield-halved",
                "supermarket": "fa-cart-shopping",
                "school": "fa-school",
            }

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = bool((presets_cfg.get(key) or {}).get("enabled", True)) if isinstance(presets_cfg, dict) else True
                if not enabled:
                    continue
                travel_display_targets.append(
                    {
                        "key": key,
                        "label": preset.get("label") or key,
                        "icon": icon_map.get(key) or "fa-route",
                        "kind": "preset",
                    }
                )

            for item in (travel_config.get("custom") or []) if isinstance(travel_config, dict) else []:
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append(
                    {
                        "key": f"custom:{target_id}",
                        "label": name,
                        "icon": "fa-location-dot",
                        "kind": "custom",
                    }
                )
        except Exception:
            travel_display_targets = []

        return render_template(
            'property_detail.html',
            property=prop,
            is_admin=check_admin_auth(),
            openai_configured=bool(getattr(Config, "OPENAI_API_KEY", None)),
            openai_analysis=(openai_variant.analysis if openai_variant else None),
            openai_model=(openai_variant.model if openai_variant else None),
            travel_display_targets=travel_display_targets,
            profiles=SearchProfileService.list_profiles(active_only=False),
        )
    except Exception as e:
        logger.error("Failed to load property detail %s", property_id, exc_info=True)
        flash("An error occurred while loading property details. Check server logs.", 'error')
        return redirect(url_for('main.properties'))

@main_bp.route('/profiles')
def profiles():
    """List search profiles (MVP; editing comes later)."""
    try:
        from services.search_profile_service import SearchProfileService
        from utils.auth import check_admin_auth

        profiles = SearchProfileService.list_profiles(active_only=False)
        # Ensure at least one profile exists.
        SearchProfileService.get_default_profile(create=True)
        profiles = SearchProfileService.list_profiles(active_only=False)
        # Lightweight properties count per profile for display.
        counts = {
            pid: cnt
            for pid, cnt in db.session.query(Property.search_profile_id, func.count(Property.id))
            .group_by(Property.search_profile_id)
            .all()
            if pid is not None
        }
        return render_template(
            'profiles.html',
            profiles=profiles,
            property_counts=counts,
            is_admin=check_admin_auth(),
        )
    except Exception as e:
        logger.error("Failed to load profiles page", exc_info=True)
        flash("An error occurred while loading profiles. Check server logs.", 'error')
        return render_template('profiles.html', profiles=[], property_counts={}, is_admin=False)


@main_bp.route('/profiles/new', methods=['GET', 'POST'])
def create_profile():
    """Create a new search profile."""
    from utils.auth import check_admin_auth
    from services.search_profile_service import SearchProfileService

    if request.method == 'POST':
        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for('main.create_profile'))

        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        is_active = request.form.get('is_active') == 'on'
        make_default = request.form.get('is_default') == 'on'

        if not name:
            flash("Name is required", "error")
            return redirect(url_for('main.create_profile'))

        if make_default and not is_active:
            # Keep defaults visible in the UI (active-only lists).
            is_active = True

        # Create with default travel targets.
        profile = SearchProfile(
            name=name[:120],
            description=description[:2000] if description else None,
            is_active=bool(is_active),
            is_default=False,
            travel_targets=SearchProfileService.get_travel_targets_config(None),
        )

        try:
            db.session.add(profile)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash("An error occurred while creating profile. Check server logs.", "error")
            return redirect(url_for('main.create_profile'))

        if make_default:
            try:
                SearchProfile.query.filter(SearchProfile.id != profile.id).update({SearchProfile.is_default: False})
                profile.is_default = True
                profile.is_active = True
                db.session.commit()
            except Exception:
                db.session.rollback()

        flash("Profile created", "success")
        return redirect(url_for('main.edit_profile', profile_id=profile.id))

    return render_template('profile_new.html', is_admin=check_admin_auth())

@main_bp.route('/profiles/<int:profile_id>/edit', methods=['GET', 'POST'])
def edit_profile(profile_id):
    """Edit a search profile (MVP: travel presets + custom targets)."""
    from utils.auth import check_admin_auth
    from utils.geocoding import GeocodingService
    from services.search_profile_service import (
        SearchProfileService,
        normalize_travel_targets_config,
    )
    from services.settings_service import SettingsService

    profile = db.get_or_404(SearchProfile, profile_id)

    if request.method == 'POST':
        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for('main.edit_profile', profile_id=profile_id))

        action = (request.form.get('action') or '').strip()
        config = SearchProfileService.get_travel_targets_config(profile)

        if action == 'save_profile_settings':
            description = (request.form.get('description') or '').strip()
            requested_active = request.form.get('is_active') == 'on'
            requested_default = request.form.get('is_default') == 'on'

            profile.description = description[:2000] if description else None

            if requested_default:
                # Keep defaults visible in active-only UIs.
                profile.is_active = True
                SearchProfile.query.filter(SearchProfile.id != profile.id).update({SearchProfile.is_default: False})
                profile.is_default = True
            else:
                # Do not allow "no default" state from the UI.
                if profile.is_default:
                    flash("At least one default profile is required. Set another profile as default first.", "error")
                    profile.is_default = True
                    profile.is_active = True
                else:
                    profile.is_default = False
                    profile.is_active = bool(requested_active)

            db.session.commit()
            flash("Profile settings saved", "success")
            return redirect(url_for('main.edit_profile', profile_id=profile_id))

        if action == 'save_presets':
            presets = dict(config.get("presets") or {})
            for preset in SearchProfileService.get_travel_preset_defs():
                key = preset.get("key")
                if not key:
                    continue
                enabled = request.form.get(f"preset_{key}") == "on"
                requested_mode = (request.form.get(f"preset_mode_{key}") or "").strip().lower()
                prev = presets.get(key) if isinstance(presets.get(key), dict) else {}
                mode = requested_mode or (prev.get("mode") or "driving")
                presets[key] = {"enabled": enabled, "mode": mode}

            profile.travel_targets = normalize_travel_targets_config({"presets": presets, "custom": config.get("custom") or []})
            db.session.commit()
            flash("Travel presets saved", "success")
            return redirect(url_for('main.edit_profile', profile_id=profile_id))

        if action == 'add_custom':
            name = (request.form.get("target_name") or "").strip()
            address = (request.form.get("target_address") or "").strip()
            lat_raw = (request.form.get("target_lat") or "").strip()
            lon_raw = (request.form.get("target_lon") or "").strip()
            mode = (request.form.get("target_mode") or "driving").strip().lower() or "driving"

            lat = None
            lon = None
            formatted_address = None

            if lat_raw and lon_raw:
                try:
                    lat = float(lat_raw)
                    lon = float(lon_raw)
                except Exception:
                    lat = None
                    lon = None

            if (lat is None or lon is None) and address:
                geo = GeocodingService().geocode_address(address)
                if geo:
                    lat = float(geo.get("lat")) if geo.get("lat") is not None else None
                    lon = float(geo.get("lng")) if geo.get("lng") is not None else None
                    formatted_address = geo.get("formatted_address")

            if not name:
                flash("Target name is required", "error")
                return redirect(url_for('main.edit_profile', profile_id=profile_id))
            if lat is None or lon is None:
                flash("Provide valid coordinates or an address that can be geocoded.", "error")
                return redirect(url_for('main.edit_profile', profile_id=profile_id))

            custom = list(config.get("custom") or [])
            custom.append(
                {
                    "id": str(uuid.uuid4()),
                    "name": name[:120],
                    "lat": lat,
                    "lon": lon,
                    "mode": mode,
                    "address": address or None,
                    "formatted_address": formatted_address or None,
                }
            )

            profile.travel_targets = normalize_travel_targets_config({"presets": config.get("presets") or {}, "custom": custom})
            db.session.commit()
            flash("Custom target added", "success")
            return redirect(url_for('main.edit_profile', profile_id=profile_id))

        if action == 'remove_custom':
            target_id = (request.form.get("target_id") or "").strip()
            custom = [t for t in (config.get("custom") or []) if str(t.get("id") or "") != target_id]
            profile.travel_targets = normalize_travel_targets_config({"presets": config.get("presets") or {}, "custom": custom})
            db.session.commit()
            flash("Custom target removed", "success")
            return redirect(url_for('main.edit_profile', profile_id=profile_id))

        if action == 'save_ai_context':
            market_context = (request.form.get("ai_market_context") or "").strip()
            ai_config = profile.ai_config if isinstance(profile.ai_config, dict) else {}
            if market_context:
                ai_config["market_context"] = market_context[:20000]
            else:
                ai_config.pop("market_context", None)

            profile.ai_config = ai_config or None
            db.session.commit()
            flash("AI context saved", "success")
            return redirect(url_for('main.edit_profile', profile_id=profile_id))

        if action == 'save_scoring_config':
            raw = (request.form.get("scoring_config_json") or "").strip()
            if not raw:
                profile.scoring_config = None
                db.session.commit()
                flash("Scoring config cleared (defaults apply)", "success")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            import json

            try:
                parsed = json.loads(raw)
            except Exception as e:
                flash("Invalid JSON for scoring config. Check syntax and try again.", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            if not isinstance(parsed, dict):
                flash("Scoring config must be a JSON object", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            profile.scoring_config = parsed
            db.session.commit()
            flash("Scoring config saved", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == 'save_classification_rules':
            raw = (request.form.get("classification_rules_json") or "").strip()
            if not raw:
                profile.classification_rules = None
                db.session.commit()
                flash("Classification rules override cleared (global defaults apply)", "success")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            import json

            try:
                parsed = json.loads(raw)
            except Exception as e:
                flash("Invalid JSON for classification rules. Check syntax and try again.", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            if not isinstance(parsed, list):
                flash("Classification rules must be a JSON array", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            from services.settings_service import _validate_classification_rule

            validated = []
            for item in parsed:
                if isinstance(item, dict):
                    valid = _validate_classification_rule(item)
                    if valid:
                        validated.append(valid)

            if not validated:
                flash("No valid rules found. Provide at least one rule with category/subtype/pattern/priority.", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            validated.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
            profile.classification_rules = validated
            db.session.commit()
            flash("Classification rules saved", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        if action == 'save_email_matchers':
            raw = (request.form.get("email_matchers_json") or "").strip()
            if not raw:
                profile.email_matchers = None
                db.session.commit()
                flash("Email matchers cleared (saved-search name extraction will be used)", "success")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            import json
            import re

            try:
                parsed = json.loads(raw)
            except Exception as e:
                flash("Invalid JSON for email matchers. Check syntax and try again.", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            if not isinstance(parsed, list):
                flash("Email matchers must be a JSON array", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            validated = []
            for item in parsed:
                if isinstance(item, str):
                    pattern = item.strip()
                    if not pattern:
                        continue
                    try:
                        re.compile(pattern)
                    except re.error:
                        continue
                    validated.append(pattern)
                elif isinstance(item, dict):
                    pattern = str(item.get("pattern") or "").strip()
                    if not pattern:
                        continue
                    try:
                        re.compile(pattern)
                    except re.error:
                        continue
                    try:
                        priority = int(item.get("priority") or 0)
                    except Exception:
                        priority = 0
                    validated.append({"pattern": pattern, "priority": priority})

            if not validated:
                flash("No valid email matchers found. Provide regex strings or {pattern,priority} objects.", "error")
                return redirect(url_for("main.edit_profile", profile_id=profile_id))

            profile.email_matchers = validated
            db.session.commit()
            flash("Email matchers saved", "success")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        flash("Unknown action", "error")
        return redirect(url_for('main.edit_profile', profile_id=profile_id))

    from services.search_profile_service import SearchProfileService

    config = SearchProfileService.get_travel_targets_config(profile)
    preset_defs = SearchProfileService.get_travel_preset_defs()
    travel_presets = []
    for preset in preset_defs:
        key = preset.get("key")
        if not key:
            continue
        enabled = bool((config.get("presets") or {}).get(key, {}).get("enabled", True))
        mode = str((config.get("presets") or {}).get(key, {}).get("mode") or "driving")
        travel_presets.append({"key": key, "label": preset.get("label") or key, "enabled": enabled, "mode": mode})

    ai_market_context = ""
    if isinstance(getattr(profile, "ai_config", None), dict):
        ai_market_context = str((profile.ai_config or {}).get("market_context") or "").strip()
    scoring_config_json = ""
    if isinstance(getattr(profile, "scoring_config", None), dict):
        try:
            import json

            scoring_config_json = json.dumps(profile.scoring_config, indent=2, ensure_ascii=False)
        except Exception:
            scoring_config_json = ""

    classification_rules_json = ""
    if isinstance(getattr(profile, "classification_rules", None), list):
        try:
            import json

            classification_rules_json = json.dumps(profile.classification_rules, indent=2, ensure_ascii=False)
        except Exception:
            classification_rules_json = ""

    email_matchers_json = ""
    if isinstance(getattr(profile, "email_matchers", None), list):
        try:
            import json

            email_matchers_json = json.dumps(profile.email_matchers, indent=2, ensure_ascii=False)
        except Exception:
            email_matchers_json = ""

    return render_template(
        "profile_edit.html",
        profile=profile,
        travel_presets=travel_presets,
        custom_targets=config.get("custom") or [],
        ai_market_context=ai_market_context,
        scoring_config_json=scoring_config_json,
        classification_rules_json=classification_rules_json,
        email_matchers_json=email_matchers_json,
        global_ai_market_context=SettingsService.get_ai_market_context(),
        is_admin=check_admin_auth(),
    )


@main_bp.route("/properties/<int:property_id>/travel/recalculate", methods=["POST"])
def recalculate_property_travel(property_id: int):
    """Recalculate travel for a single property."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

        prop = db.get_or_404(Property, property_id)
        from services.property_travel_service import PropertyTravelService

        ok = PropertyTravelService().calculate_for_property(prop, commit=True)
        if ok:
            flash("Travel recalculated", "success")
        else:
            flash("Travel not updated (missing coordinates or targets).", "error")

        return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))
    except Exception as e:
        logger.error("Failed to recalculate travel for property %s", property_id, exc_info=True)
        flash("An error occurred while recalculating travel. Check server logs.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/<int:property_id>/score/recalculate", methods=["POST"])
def recalculate_property_score(property_id: int):
    """Recalculate scoring for a single property."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

        prop = db.get_or_404(Property, property_id)
        from services.property_scoring_service import PropertyScoringService

        ok = PropertyScoringService().calculate_for_property(prop, commit=True)
        if ok:
            flash("Scoring recalculated", "success")
        else:
            flash("Scoring not updated.", "error")

        return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))
    except Exception as e:
        logger.error("Failed to recalculate score for property %s", property_id, exc_info=True)
        flash("An error occurred while recalculating scoring. Check server logs.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/profiles/<int:profile_id>/travel/recalculate", methods=["POST"])
def recalculate_profile_travel(profile_id: int):
    """Recalculate travel for properties in a profile (bounded, best-effort)."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        profile = db.get_or_404(SearchProfile, profile_id)

        mode = (request.form.get("mode") or "all").strip().lower()
        try:
            limit = int(request.form.get("limit") or 100)
        except Exception:
            limit = 100
        limit = min(max(limit, 1), 500)

        query = Property.query.filter(Property.search_profile_id == profile.id)
        query = query.filter(Property.listing_status == "active")
        if mode == "missing":
            query = query.filter(Property.travel.is_(None))

        properties = query.order_by(Property.created_at.desc()).limit(limit).all()

        from services.property_travel_service import PropertyTravelService

        service = PropertyTravelService()
        updated = 0
        for prop in properties:
            try:
                if service.calculate_for_property(prop, commit=True):
                    updated += 1
            except Exception as inner:
                logger.warning("Travel recalculation failed for property %s: %s", prop.id, inner)
                db.session.rollback()
                continue

        flash(f"Recalculated travel for {updated} / {len(properties)} properties", "success")
        return redirect(request.referrer or url_for("main.properties", profile_id=profile_id))
    except Exception as e:
        logger.error("Failed to recalculate travel for profile %s", profile_id, exc_info=True)
        flash("An error occurred while recalculating profile travel. Check server logs.", "error")
        return redirect(url_for("main.edit_profile", profile_id=profile_id))


@main_bp.route("/profiles/<int:profile_id>/score/recalculate", methods=["POST"])
def recalculate_profile_scoring(profile_id: int):
    """Recalculate scoring for properties in a profile (bounded, best-effort)."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        profile = db.get_or_404(SearchProfile, profile_id)

        mode = (request.form.get("mode") or "all").strip().lower()
        try:
            limit = int(request.form.get("limit") or 100)
        except Exception:
            limit = 100
        limit = min(max(limit, 1), 500)

        query = Property.query.filter(Property.search_profile_id == profile.id)
        query = query.filter(Property.listing_status == "active")
        if mode == "missing":
            query = query.filter(Property.score_total.is_(None))

        properties = query.order_by(Property.created_at.desc()).limit(limit).all()

        from services.property_scoring_service import PropertyScoringService

        service = PropertyScoringService()
        updated = 0
        for prop in properties:
            try:
                if service.calculate_for_property(prop, commit=True):
                    updated += 1
            except Exception as inner:
                logger.warning("Scoring recalculation failed for property %s: %s", prop.id, inner)
                db.session.rollback()
                continue

        flash(f"Recalculated scoring for {updated} / {len(properties)} properties", "success")
        return redirect(request.referrer or url_for("main.properties", profile_id=profile_id))

    except Exception as e:
        logger.error("Failed to recalculate scoring for profile %s", profile_id, exc_info=True)
        flash("An error occurred while recalculating profile scoring. Check server logs.", "error")
        return redirect(url_for("main.edit_profile", profile_id=profile_id))


@main_bp.route("/profiles/<int:profile_id>/classification/recalculate", methods=["POST"])
def recalculate_profile_classification(profile_id: int):
    """Re-run classification for properties in a profile (bounded, best-effort)."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.edit_profile", profile_id=profile_id))

        profile = db.get_or_404(SearchProfile, profile_id)

        mode = (request.form.get("mode") or "all").strip().lower()
        try:
            limit = int(request.form.get("limit") or 200)
        except Exception:
            limit = 200
        limit = min(max(limit, 1), 1000)

        recalc_scoring = (request.form.get("recalc_scoring") or "").strip().lower() in ("1", "true", "on", "yes")

        query = Property.query.filter(Property.search_profile_id == profile.id)
        query = query.filter(Property.listing_status == "active")
        if mode == "missing":
            query = query.filter(or_(Property.property_category.is_(None), Property.property_subtype.is_(None)))

        properties = query.order_by(Property.created_at.desc()).limit(limit).all()

        from services.property_classification_service import PropertyClassificationService

        scoring_service = None
        if recalc_scoring:
            from services.property_scoring_service import PropertyScoringService

            scoring_service = PropertyScoringService()

        updated = 0
        changed_and_scored = 0
        for prop in properties:
            try:
                changed = PropertyClassificationService.apply_classification(prop, profile)
                if changed:
                    updated += 1
                    if scoring_service:
                        if scoring_service.calculate_for_property(prop, commit=False):
                            changed_and_scored += 1

                db.session.commit()
            except Exception as inner:
                logger.warning("Profile classification failed for property %s: %s", prop.id, inner)
                db.session.rollback()
                continue

        if scoring_service:
            flash(
                f"Reclassified {updated} / {len(properties)} properties (scoring updated for {changed_and_scored})",
                "success",
            )
        else:
            flash(f"Reclassified {updated} / {len(properties)} properties", "success")
        return redirect(request.referrer or url_for("main.edit_profile", profile_id=profile_id))

    except Exception as e:
        logger.error("Failed to reclassify profile %s", profile_id, exc_info=True)
        flash("An error occurred while reclassifying profile. Check server logs.", "error")
        return redirect(url_for("main.edit_profile", profile_id=profile_id))


@main_bp.route("/properties/<int:property_id>/set-status", methods=["POST"])
def set_property_status_form(property_id: int):
    """Set listing status for a Property (admin-only)."""
    try:
        from utils.auth import check_admin_auth
        from datetime import datetime

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

        prop = db.get_or_404(Property, property_id)
        new_status = (request.form.get("status") or "removed").strip().lower()
        if new_status not in ("active", "removed", "sold"):
            flash("Invalid status. Must be active/removed/sold.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

        old_status = prop.listing_status or "active"
        prop.listing_status = new_status
        prop.listing_last_checked = datetime.now(timezone.utc)
        if new_status in ("removed", "sold") and old_status == "active":
            prop.listing_removed_date = datetime.now(timezone.utc)
        elif new_status == "active" and old_status in ("removed", "sold"):
            prop.listing_removed_date = None

        db.session.commit()
        flash(f"Status updated to {new_status}", "success")
        return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))
    except Exception as e:
        logger.error("Failed to set property status %s", property_id, exc_info=True)
        db.session.rollback()
        flash("An error occurred while setting status. Check server logs.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/<int:property_id>/classification", methods=["POST"])
def set_property_classification_form(property_id: int):
    """Set category/subtype (manual override) or auto-classify for a Property."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

        prop = db.get_or_404(Property, property_id)

        action = (request.form.get("action") or "").strip().lower()
        lock = (request.form.get("classification_locked") or "").strip().lower() in ("1", "true", "on", "yes")
        recalc_scoring = (request.form.get("recalc_scoring") or "").strip().lower() in ("1", "true", "on", "yes")

        attrs = prop.attributes if isinstance(prop.attributes, dict) else {}
        attrs["classification_locked"] = bool(lock)
        prop.attributes = attrs

        if action == "set_manual":
            category = (request.form.get("property_category") or "").strip()
            subtype = (request.form.get("property_subtype") or "").strip()

            prop.property_category = category or None
            prop.property_subtype = subtype or None

            if (prop.property_category or "").strip().lower() == "land":
                prop.area_type = "plot"
            elif prop.area is not None:
                if (prop.area_type or "unknown").strip().lower() in ("", "unknown"):
                    prop.area_type = "built"
            else:
                if not prop.area_type:
                    prop.area_type = "unknown"

            if recalc_scoring:
                from services.property_scoring_service import PropertyScoringService

                PropertyScoringService().calculate_for_property(prop, commit=False)

            db.session.commit()
            flash("Classification updated", "success")
            return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))

        if action == "auto":
            from services.property_classification_service import PropertyClassificationService

            profile = prop.search_profile if prop.search_profile else None
            changed = PropertyClassificationService.apply_classification(prop, profile, respect_lock=False)

            if recalc_scoring:
                from services.property_scoring_service import PropertyScoringService

                PropertyScoringService().calculate_for_property(prop, commit=False)

            db.session.commit()
            if changed:
                flash("Auto-classified using current rules", "success")
            else:
                flash("No classification changes (no match or already up-to-date)", "success")
            return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))

        flash("Unknown action", "error")
        return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))

    except Exception as e:
        logger.error("Failed to update classification for property %s", property_id, exc_info=True)
        db.session.rollback()
        flash("An error occurred while updating classification. Check server logs.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route("/properties/<int:property_id>/profile", methods=["POST"])
def set_property_profile_form(property_id: int):
    """Assign a property to a specific profile (admin-only)."""
    try:
        from utils.auth import check_admin_auth

        if not check_admin_auth():
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for("main.property_detail", property_id=property_id))

        prop = db.get_or_404(Property, property_id)
        profile_id = request.form.get("profile_id", type=int)
        recalc_travel = (request.form.get("recalc_travel") or "").strip().lower() in ("1", "true", "on", "yes")
        recalc_scoring = (request.form.get("recalc_scoring") or "").strip().lower() in ("1", "true", "on", "yes")

        profile = None
        if profile_id:
            profile = db.session.get(SearchProfile, profile_id)
            if not profile:
                flash("Selected profile not found", "error")
                return redirect(url_for("main.property_detail", property_id=property_id))

            prop.search_profile_id = profile.id
            enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
            assignment_meta = enrichment.get("profile_assignment") if isinstance(enrichment.get("profile_assignment"), dict) else {}
            assignment_meta.update(
                {
                    "method": "manual_override",
                    "profile_id": profile.id,
                    "profile_name": profile.name,
                    "assigned_at": datetime.now(timezone.utc).isoformat(),
                    "manual_override": True,
                }
            )
            enrichment["profile_assignment"] = assignment_meta
            prop.enrichment = enrichment
        else:
            try:
                from services.profile_assignment_service import ProfileAssignmentService

                result = ProfileAssignmentService().assign_nearest_profile(prop, commit=False, force=True)
                if not result.get("assigned"):
                    enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
                    assignment_meta = enrichment.get("profile_assignment") if isinstance(enrichment.get("profile_assignment"), dict) else {}
                    assignment_meta.update(
                        {
                            "method": "auto_enabled",
                            "assigned_at": datetime.now(timezone.utc).isoformat(),
                            "manual_override": False,
                        }
                    )
                    enrichment["profile_assignment"] = assignment_meta
                    prop.enrichment = enrichment
            except Exception as e:
                logger.warning("Auto assignment failed during profile update for %s: %s", property_id, e)

        if recalc_travel:
            from services.property_travel_service import PropertyTravelService

            PropertyTravelService().calculate_for_property(prop, commit=False)

        if recalc_scoring:
            from services.property_scoring_service import PropertyScoringService

            PropertyScoringService().calculate_for_property(prop, commit=False)

        db.session.commit()
        flash("Profile updated", "success")
        return redirect(request.referrer or url_for("main.property_detail", property_id=property_id))
    except Exception as e:
        logger.error("Failed to set profile for property %s", property_id, exc_info=True)
        db.session.rollback()
        flash("An error occurred while updating profile. Check server logs.", "error")
        return redirect(url_for("main.property_detail", property_id=property_id))


@main_bp.route('/lands/<int:land_id>')
def land_detail(land_id):
    """Detailed view of a specific land"""
    try:
        land = db.get_or_404(Land, land_id)

        # Backfill missing municipality from the title (common case when the email parser can't extract it).
        if not land.municipality and land.title:
            from utils.email_parser import EmailParser

            derived = EmailParser()._extract_municipality_from_title(land.title)
            if derived:
                land.municipality = derived
                db.session.commit()
        
        # Normalize property_details to dict format for template compatibility
        from utils.property_data import normalize_property_details
        land.property_details = normalize_property_details(land.property_details)
        
        
        
        # Get score breakdown from environment field
        score_breakdown = {}
        if land.environment and 'scoring' in land.environment:
            score_breakdown = land.environment['scoring']

        # Reference cities (configurable via Scoring Criteria page)
        reference_cities_for_view = []
        try:
            from services.settings_service import SettingsService

            cities = SettingsService.get_reference_cities()
            if land.location_lat and land.location_lon:
                lat = float(land.location_lat)
                lon = float(land.location_lon)

                for idx, city in enumerate(cities):
                    distance_km = round(_haversine_km(lat, lon, float(city["lat"]), float(city["lon"])))
                    travel_time = land.travel_time_oviedo if idx == 0 else land.travel_time_gijon if idx == 1 else None
                    reference_cities_for_view.append(
                        {
                            "name": city["name"],
                            "distance_km": distance_km,
                            "travel_time_min": travel_time,
                        }
                    )
        except Exception as e:
            logger.warning("Failed to load reference cities for detail view: %s", e)

        # Latest ChatGPT/OpenAI structured analysis variant (if present)
        openai_analysis = None
        openai_model = None
        try:
            from models import AiAnalysisVariant

            openai_variant = (
                AiAnalysisVariant.query.filter_by(land_id=land.id, provider="openai")
                .order_by(AiAnalysisVariant.created_at.desc())
                .first()
            )
            if openai_variant:
                openai_analysis = openai_variant.analysis
                openai_model = openai_variant.model
        except Exception as e:
            logger.warning("Failed to load OpenAI analysis variant for land %s: %s", land.id, e)
        
        return render_template(
            'land_detail.html',
            land=land,
            score_breakdown=score_breakdown,
            reference_cities=reference_cities_for_view,
            openai_analysis=openai_analysis,
            openai_model=openai_model,
        )
        
    except Exception as e:
        logger.error("Failed to load land detail %s", land_id, exc_info=True)
        flash("An error occurred while loading land details. Check server logs.", 'error')
        return redirect(url_for('main.lands'))

@main_bp.route('/map')
def map_view():
    """Interactive map view of all properties with coordinates"""
    try:
        from services.search_profile_service import SearchProfileService

        profiles = SearchProfileService.list_profiles(active_only=True)
        default_profile = SearchProfileService.get_default_profile(create=True)

        requested_profile_id = request.args.get('profile_id', type=int)
        selected_profile_id = requested_profile_id

        # For better UX, auto-select the profile with the most mappable properties (coords present).
        # If none have coords yet, fall back to the most recently active profile with any properties.
        if selected_profile_id is None:
            mappable = (
                db.session.query(
                    Property.search_profile_id,
                    func.count(Property.id).label("cnt"),
                    func.max(Property.created_at).label("latest"),
                )
                .join(SearchProfile, SearchProfile.id == Property.search_profile_id)
                .filter(SearchProfile.is_active.is_(True))
                .filter(Property.location_lat.isnot(None), Property.location_lon.isnot(None))
                .filter(Property.listing_status.notin_(["removed", "sold"]))
                .group_by(Property.search_profile_id)
                .order_by(func.count(Property.id).desc(), func.max(Property.created_at).desc())
                .first()
            )
            if mappable and mappable[0] is not None:
                selected_profile_id = int(mappable[0])
            else:
                recent = (
                    db.session.query(Property.search_profile_id, func.max(Property.created_at).label("latest"))
                    .join(SearchProfile, SearchProfile.id == Property.search_profile_id)
                    .filter(SearchProfile.is_active.is_(True))
                    .group_by(Property.search_profile_id)
                    .order_by(func.max(Property.created_at).desc())
                    .first()
                )
                if recent and recent[0] is not None:
                    selected_profile_id = int(recent[0])
                elif default_profile:
                    selected_profile_id = default_profile.id
                elif profiles:
                    selected_profile_id = profiles[0].id
                else:
                    selected_profile_id = None

        query = Property.query.filter(
            Property.location_lat.isnot(None),
            Property.location_lon.isnot(None),
        )
        if selected_profile_id is not None:
            query = query.filter(Property.search_profile_id == selected_profile_id)

        query = query.filter(Property.listing_status.notin_(["removed", "sold"]))
        props = query.all()

        # Profile-driven travel targets for popup display.
        travel_display_targets = []
        if selected_profile_id is not None:
            try:
                selected_profile = db.session.get(SearchProfile, selected_profile_id)
            except Exception:
                selected_profile = None

            travel_config = SearchProfileService.get_travel_targets_config(selected_profile)
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = travel_config.get("presets") if isinstance(travel_config, dict) else {}

            icon_map = {
                "airport": "fa-plane",
                "train_station": "fa-train",
                "hospital": "fa-hospital",
                "police": "fa-shield-halved",
                "supermarket": "fa-cart-shopping",
                "school": "fa-school",
            }

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = bool((presets_cfg.get(key) or {}).get("enabled", True)) if isinstance(presets_cfg, dict) else True
                if not enabled:
                    continue
                travel_display_targets.append(
                    {
                        "key": key,
                        "label": preset.get("label") or key,
                        "icon": icon_map.get(key) or "fa-route",
                        "kind": "preset",
                    }
                )

            for item in (travel_config.get("custom") or []) if isinstance(travel_config, dict) else []:
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append(
                    {
                        "key": f"custom:{target_id}",
                        "label": name,
                        "icon": "fa-location-dot",
                        "kind": "custom",
                    }
                )

        markers = []
        for prop in props:
            markers.append(
                {
                    "id": prop.id,
                    "lat": float(prop.location_lat),
                    "lon": float(prop.location_lon),
                    "title": prop.title or f"Property #{prop.id}",
                    "price": float(prop.price) if prop.price else None,
                    "area": float(prop.area) if prop.area else None,
                    "score": float(prop.score_total) if prop.score_total else None,
                    "category": prop.property_category,
                    "subtype": prop.property_subtype,
                    "url": prop.url,
                    "municipality": prop.municipality,
                    "travel": prop.travel if isinstance(prop.travel, dict) else None,
                }
            )

        return render_template(
            "map.html",
            markers=markers,
            profiles=profiles,
            selected_profile_id=selected_profile_id,
            travel_display_targets=travel_display_targets,
        )

    except Exception as e:
        logger.error("Failed to load map view", exc_info=True)
        flash("An error occurred while loading map. Check server logs.", 'error')
        return render_template('map.html', markers=[], profiles=[], selected_profile_id=None, travel_display_targets=[])

@main_bp.route('/criteria')
def criteria():
    """Scoring criteria management page with dual scoring profiles"""
    try:
        from config import Config
        from services.scoring_service import ScoringService
        from services.settings_service import SettingsService
        from utils.city_registry import all_city_names
        from models import MarketSettings

        # Load profile weights using ScoringService for consistency
        scoring_service = ScoringService()

        # Get investment profile weights (DB first, Config fallback)
        investment_weights = scoring_service._load_profile_weights('investment')
        if not investment_weights and hasattr(Config, 'SCORING_PROFILES'):
            investment_weights = Config.SCORING_PROFILES.get('investment', {})

        # Get lifestyle profile weights (DB first, Config fallback)
        lifestyle_weights = scoring_service._load_profile_weights('lifestyle')
        if not lifestyle_weights and hasattr(Config, 'SCORING_PROFILES'):
            lifestyle_weights = Config.SCORING_PROFILES.get('lifestyle', {})

        # Get combined mix ratio (DB first, Config fallback)
        combined_mix = scoring_service._load_combined_mix()

        # Get criteria descriptions for display
        criteria_descriptions = {
            'investment_yield': 'Rental yield, cap rate, investment metrics and return potential',
            'location_quality': 'Proximity to urban centers, municipality prestige, neighborhood quality',
            'transport': 'Public transport access, highways, airports, train stations',
            'infrastructure_basic': 'Water, electricity, sewerage, internet connectivity',
            'infrastructure_extended': 'Gas, telecommunications, public services infrastructure',
            'environment': 'Environmental quality, views, natural features, orientation',
            'physical_characteristics': 'Land size, shape, topography, price per square meter',
            'services_quality': 'Schools, hospitals, shopping, restaurants quality ratings',
            'legal_status': 'Zoning status, building permissions, land classification',
            'development_potential': 'Future development possibilities, urbanization plans'
        }

        reference_cities = SettingsService.get_reference_cities()
        city_registry_names = all_city_names()

        # Get market settings
        market_settings = MarketSettings.get_settings()

        return render_template('criteria.html',
                             investment_weights=investment_weights,
                             lifestyle_weights=lifestyle_weights,
                             combined_mix=combined_mix,
                             criteria_descriptions=criteria_descriptions,
                             reference_cities=reference_cities,
                             city_registry_names=city_registry_names,
                             market_settings=market_settings)

    except Exception as e:
        logger.error("Failed to load criteria page", exc_info=True)
        flash("An error occurred while loading criteria. Check server logs.", 'error')
        return render_template('criteria.html',
                             investment_weights={},
                             lifestyle_weights={},
                             combined_mix={'investment': 0.32, 'lifestyle': 0.68},
                             criteria_descriptions={},
                             market_settings=None)

@main_bp.route('/settings/properties', methods=['GET', 'POST'])
def property_settings():
    """Global settings for universal properties (ingestion + classification rules + AI context)."""
    from utils.auth import check_admin_auth
    from services.settings_service import SettingsService, DEFAULT_PROPERTY_CLASSIFICATION_RULES, DEFAULT_AI_MARKET_CONTEXT

    is_admin = check_admin_auth()

    if request.method == 'POST':
        if not is_admin:
            flash("Unauthorized. Admin authentication required.", "error")
            return redirect(url_for('main.property_settings'))

        action = (request.form.get("action") or "").strip()

        if action == "save_reference_cities":
            try:
                city_names = [n for n in request.form.getlist("reference_city_name") if (n or "").strip()]
                SettingsService.set_reference_city_names(city_names)
                flash("Reference cities updated. Re-enrich properties to update travel metrics.", "success")
            except Exception as e:
                logger.error("Failed to update reference cities", exc_info=True)
                flash("An error occurred while updating reference cities. Check server logs.", "error")
            return redirect(url_for("main.property_settings"))

        if action == "save_ai_context":
            raw = (request.form.get("ai_market_context") or "").strip()
            SettingsService.set_ai_market_context(raw or DEFAULT_AI_MARKET_CONTEXT)
            flash("Global AI market context saved", "success")
            return redirect(url_for("main.property_settings"))

        if action == "save_classification_rules":
            raw = (request.form.get("classification_rules_json") or "").strip()
            if not raw:
                SettingsService.set_property_classification_rules([dict(r) for r in DEFAULT_PROPERTY_CLASSIFICATION_RULES])
                flash("Classification rules reset to defaults", "success")
                return redirect(url_for("main.property_settings"))

            import json

            try:
                parsed = json.loads(raw)
            except Exception as e:
                flash("Invalid JSON for classification rules. Check syntax and try again.", "error")
                return redirect(url_for("main.property_settings"))

            if not isinstance(parsed, list):
                flash("Classification rules must be a JSON array", "error")
                return redirect(url_for("main.property_settings"))

            from services.settings_service import _validate_classification_rule

            validated = []
            for item in parsed:
                if isinstance(item, dict):
                    valid = _validate_classification_rule(item)
                    if valid:
                        validated.append(valid)

            if not validated:
                flash("No valid rules found. Provide at least one rule with category/subtype/pattern/priority.", "error")
                return redirect(url_for("main.property_settings"))

            validated.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
            SettingsService.set_property_classification_rules(validated)
            flash("Classification rules saved", "success")
            return redirect(url_for("main.property_settings"))

        if action == "save_ingestion_settings":
            sale_only = request.form.get("sale_only") == "on"
            raw_excluded = (request.form.get("excluded_categories") or "").strip()
            excluded = [part.strip() for part in raw_excluded.split(",") if part.strip()] if raw_excluded else []

            SettingsService.set_sale_only(sale_only)
            SettingsService.set_excluded_property_categories(excluded)

            flash("Ingestion settings saved", "success")
            return redirect(url_for("main.property_settings"))

        flash("Unknown action", "error")
        return redirect(url_for("main.property_settings"))

    # GET
    try:
        import json

        classification_rules_json = json.dumps(
            SettingsService.get_property_classification_rules(),
            indent=2,
            ensure_ascii=False,
        )
    except Exception:
        classification_rules_json = ""

    # Reference cities registry (for datalist/autocomplete).
    reference_cities = []
    city_registry_names = []
    try:
        from utils.city_registry import all_city_names

        reference_cities = SettingsService.get_reference_cities()
        city_registry_names = all_city_names()
    except Exception:
        reference_cities = []
        city_registry_names = []

    # Diagnostic/ops info for troubleshooting ingestion cursors.
    last_seen_uid = None
    try:
        import os

        from config import Config

        uid_path = getattr(Config, "LAST_SEEN_UID_PROPERTIES_PATH", None) or getattr(Config, "LAST_SEEN_UID_PATH", None)
        if uid_path and os.path.exists(uid_path):
            with open(uid_path, "r") as f:
                last_seen_uid = int((f.read() or "").strip() or "0")
    except Exception:
        last_seen_uid = None

    try:
        from models import Property

        property_count = Property.query.count()
    except Exception:
        property_count = None

    return render_template(
        "property_settings.html",
        is_admin=is_admin,
        ai_market_context=SettingsService.get_ai_market_context(),
        classification_rules_json=classification_rules_json,
        default_ai_market_context=DEFAULT_AI_MARKET_CONTEXT,
        sale_only=SettingsService.get_sale_only(),
        excluded_categories_csv=", ".join(SettingsService.get_excluded_property_categories()),
        last_seen_uid=last_seen_uid,
        property_count=property_count,
        reference_cities=reference_cities,
        city_registry_names=city_registry_names,
    )

@main_bp.route('/criteria/update', methods=['POST'])
@admin_required
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
        
        if scoring_service.update_weights(weights, profile='combined'):
            flash('Scoring criteria updated successfully. All lands have been rescored.', 'success')
        else:
            flash('Failed to update scoring criteria', 'error')
        
        return redirect(url_for('main.criteria'))
        
    except Exception as e:
        logger.error("Failed to update criteria", exc_info=True)
        flash("An error occurred while updating criteria. Check server logs.", 'error')
        return redirect(url_for('main.criteria'))

@main_bp.route('/criteria/update_profile/<profile>', methods=['POST'])
@admin_required
def update_criteria_profile(profile):
    """Update scoring criteria weights for a specific profile (investment/lifestyle)"""
    try:
        # Validate profile
        if profile not in ['investment', 'lifestyle']:
            flash(f'Invalid profile: {profile}', 'error')
            return redirect(url_for('main.criteria'))
        
        # Parse form data to get new weights
        weights = {}
        for key, value in request.form.items():
            if key.startswith('weight_'):
                criteria_name = key.replace('weight_', '')
                try:
                    weights[criteria_name] = float(value)
                except ValueError:
                    flash(f"Invalid weight value for {criteria_name}", 'error')
                    return redirect(url_for('main.criteria'))
        
        logger.info(f"Updating {profile} profile weights: {weights}")
        
        # Use ScoringService to update weights for specific profile
        from services.scoring_service import ScoringService
        scoring_service = ScoringService()
        
        if scoring_service.update_weights(weights, profile=profile):
            flash(f'{profile.title()} profile weights updated and all properties rescored successfully!', 'success')
        else:
            flash(f'Failed to update {profile} profile weights.', 'error')
            
    except Exception as e:
        logger.error("Failed to update %s profile criteria", profile, exc_info=True)
        flash("An error occurred while updating profile criteria. Check server logs.", 'error')
        
    return redirect(url_for('main.criteria'))

@main_bp.route('/criteria/update_combined_mix', methods=['POST'])
@admin_required
def update_combined_mix():
    """Update the Investment vs Lifestyle balance for combined scoring"""
    try:
        # Parse form data
        investment_weight = float(request.form.get('investment_weight', 0.32))
        lifestyle_weight = float(request.form.get('lifestyle_weight', 0.68))
        
        # Validate weights sum to 1.0
        total_weight = investment_weight + lifestyle_weight
        if abs(total_weight - 1.0) > 0.01:
            flash(f'Combined mix weights must sum to 1.0, got {total_weight:.3f}', 'error')
            return redirect(url_for('main.criteria'))
        
        logger.info(f"Updating combined mix: Investment={investment_weight:.3f}, Lifestyle={lifestyle_weight:.3f}")

        # Persist combined mix to database
        from models import ScoringCriteria
        from decimal import Decimal as D
        for key, val in [('investment', investment_weight), ('lifestyle', lifestyle_weight)]:
            row = ScoringCriteria.query.filter_by(
                criteria_name=key, profile='combined'
            ).first()
            if row:
                row.weight = D(str(val))
                row.active = True
            else:
                db.session.add(ScoringCriteria(
                    criteria_name=key,
                    profile='combined',
                    weight=D(str(val)),
                    active=True,
                ))
        db.session.commit()
        
        # Rescore all lands with new mix in batches
        from models import Land
        from services.scoring_service import ScoringService
        from app import db

        scoring_service = ScoringService()
        batch_size = 100
        offset = 0
        total_rescored = 0

        while True:
            lands = Land.query.limit(batch_size).offset(offset).all()
            if not lands:
                break

            for land in lands:
                scoring_service.calculate_score(land)

            db.session.commit()
            total_rescored += len(lands)
            offset += batch_size

        flash(f'Combined mix updated to {investment_weight*100:.0f}% Investment + {lifestyle_weight*100:.0f}% Lifestyle. {total_rescored} properties rescored!', 'success')
        
    except Exception as e:
        logger.error("Failed to update combined mix", exc_info=True)
        flash("An error occurred while updating combined mix. Check server logs.", 'error')
        
    return redirect(url_for('main.criteria'))


@main_bp.route('/criteria/update_reference_cities', methods=['POST'])
@admin_required
def update_reference_cities():
    """Update reference cities used for travel-time scoring/display."""
    try:
        from services.settings_service import SettingsService

        city_names = [n for n in request.form.getlist("reference_city_name") if (n or "").strip()]
        SettingsService.set_reference_city_names(city_names)
        flash("Reference cities updated. Re-enrich properties to update travel times.", "success")
    except Exception as e:
        logger.error("Failed to update reference cities", exc_info=True)
        flash("An error occurred while updating reference cities. Check server logs.", "error")

    return redirect(url_for('main.criteria'))


@main_bp.route('/criteria/update_market_settings', methods=['POST'])
@admin_required
def update_market_settings():
    """Update market analysis settings used for AI property enrichment."""
    try:
        from models import MarketSettings

        settings = MarketSettings.get_settings()

        # Construction costs
        settings.construction_basic_min = int(request.form.get('construction_basic_min', 1100))
        settings.construction_basic_avg = int(request.form.get('construction_basic_avg', 1300))
        settings.construction_basic_max = int(request.form.get('construction_basic_max', 1500))
        settings.construction_premium_min = int(request.form.get('construction_premium_min', 1500))
        settings.construction_premium_avg = int(request.form.get('construction_premium_avg', 1800))
        settings.construction_premium_max = int(request.form.get('construction_premium_max', 2200))

        # Purchase costs (convert from percentage to ratio)
        settings.purchase_costs_ratio = float(request.form.get('purchase_costs_ratio', 11)) / 100

        # Urban adjustments (convert from percentage to ratio)
        settings.urban_vacancy_rate = float(request.form.get('urban_vacancy_rate', 5)) / 100
        settings.urban_operating_expenses = float(request.form.get('urban_operating_expenses', 15)) / 100
        settings.urban_management_fee = float(request.form.get('urban_management_fee', 0)) / 100

        # Suburban adjustments
        settings.suburban_vacancy_rate = float(request.form.get('suburban_vacancy_rate', 8)) / 100
        settings.suburban_operating_expenses = float(request.form.get('suburban_operating_expenses', 15)) / 100
        settings.suburban_management_fee = float(request.form.get('suburban_management_fee', 0)) / 100

        # Rural adjustments
        settings.rural_vacancy_rate = float(request.form.get('rural_vacancy_rate', 20)) / 100
        settings.rural_operating_expenses = float(request.form.get('rural_operating_expenses', 18)) / 100
        settings.rural_management_fee = float(request.form.get('rural_management_fee', 10)) / 100

        # Rental prices
        settings.urban_rental_min = int(request.form.get('urban_rental_min', 8))
        settings.urban_rental_avg = int(request.form.get('urban_rental_avg', 10))
        settings.urban_rental_max = int(request.form.get('urban_rental_max', 13))
        settings.suburban_rental_min = int(request.form.get('suburban_rental_min', 6))
        settings.suburban_rental_avg = int(request.form.get('suburban_rental_avg', 8))
        settings.suburban_rental_max = int(request.form.get('suburban_rental_max', 10))
        settings.rural_rental_min = int(request.form.get('rural_rental_min', 5))
        settings.rural_rental_avg = int(request.form.get('rural_rental_avg', 7))
        settings.rural_rental_max = int(request.form.get('rural_rental_max', 9))

        db.session.commit()
        flash("Market settings updated successfully. New AI analyses will use these values.", "success")

    except Exception as e:
        logger.error("Failed to update market settings", exc_info=True)
        db.session.rollback()
        flash("An error occurred while updating market settings. Check server logs.", "error")

    return redirect(url_for('main.criteria'))


@main_bp.route('/land/<int:land_id>/edit-environment', methods=['GET', 'POST'])
@admin_required
def edit_environment(land_id):
    """Edit environment data for a land"""
    try:
        land = db.get_or_404(Land, land_id)
        
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
        logger.error("Failed to edit environment for land %s", land_id, exc_info=True)
        flash("An error occurred while editing environment. Check server logs.", 'error')
        return redirect(url_for('main.land_detail', land_id=land_id))

@main_bp.route('/land/<int:land_id>/update-score', methods=['POST'])
@admin_required
def update_score(land_id):
    """Update manual score for a land"""
    try:
        land = db.get_or_404(Land, land_id)
        
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
        logger.error("Failed to update score for land %s", land_id, exc_info=True)
        flash("An error occurred while updating score. Check server logs.", 'error')
        return redirect(url_for('main.land_detail', land_id=land_id))

@main_bp.route('/export.csv')
def export_csv():
    """Export current land selection to CSV"""
    try:
        from flask import make_response
        try:
            from defusedcsv import csv
        except ImportError:  # pragma: no cover
            import csv
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
        investment_metrics_filter = request.args.get('inv_metr', '')
        sea_view_filter = request.args.get('sea_view', '') == 'on'
        favorites_filter = request.args.get('favorites', '') == 'on'

        # Build query with same filters - defer heavy columns for export performance
        query = Land.query.options(
            defer(Land.infrastructure_basic),
            defer(Land.infrastructure_extended),
            defer(Land.transport),
            defer(Land.environment),
            defer(Land.neighborhood),
            defer(Land.services_quality),
            defer(Land.ai_analysis),
            defer(Land.enhanced_description)
        )

        if land_type_filter:
            query = query.filter(Land.land_type == land_type_filter)

        if municipality_filter:
            query = query.filter(Land.municipality.ilike(f'%{municipality_filter}%'))

        if search_query:
            # Split search query into words for flexible matching
            # Filter out common words and short terms
            stop_words = {'for', 'in', 'the', 'a', 'an', 'of', 'to', 'and', 'or', 'sale', 'plot'}
            words = [w.strip(',.;:!?()[]{}') for w in search_query.split()]
            words = [w for w in words if w and len(w) > 1 and w.lower() not in stop_words]

            if words:
                # Each word must match at least one field (title, description, or municipality)
                for word in words:
                    word_pattern = f'%{word}%'
                    query = query.filter(
                        or_(
                            Land.title.ilike(word_pattern),
                            Land.description.ilike(word_pattern),
                            Land.municipality.ilike(word_pattern)
                        )
                    )

        if investment_metrics_filter:
            rating_text = func.coalesce(
                Land.ai_analysis['rental_market_analysis']['investment_rating'].as_string(),
                ''
            )
            upper_rating = func.upper(rating_text)
            filter_value = investment_metrics_filter.strip().upper()
            if filter_value == 'EXCELLENT':
                query = query.filter(upper_rating.like('EXCELLENT%'))
            elif filter_value == 'GOOD':
                query = query.filter(upper_rating.like('GOOD%'))
            elif filter_value == 'MODERATE':
                query = query.filter(upper_rating.like('MODERATE%'))
            elif filter_value == 'BELOW':
                query = query.filter(upper_rating.like('BELOW%'))
        
        if sea_view_filter:
            # SQLAlchemy 2.x: .astext removed; use JSON accessors
            query = query.filter(Land.environment['sea_view'].as_boolean().is_(True))

        if favorites_filter:
            query = query.filter(Land.is_favorite == True)

        # Apply sorting with same logic as main lands route
        if sort_by == 'investment_metrics':
            rating_text = func.coalesce(
                Land.ai_analysis['rental_market_analysis']['investment_rating'].as_string(),
                ''
            )
            upper_rating = func.upper(rating_text)
            rank = case(
                (upper_rating.like('EXCELLENT%'), 4),
                (upper_rating.like('GOOD%'), 3),
                (upper_rating.like('MODERATE%'), 2),
                (upper_rating.like('BELOW%'), 1),
                else_=None
            )
            if sort_order == 'asc':
                lands = query.order_by(rank.asc().nullslast(), Land.score_total.desc().nullslast()).all()
            else:
                lands = query.order_by(rank.desc().nullslast(), Land.score_total.desc().nullslast()).all()
        elif hasattr(Land, sort_by):
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
        logger.error("Failed to export CSV", exc_info=True)
        flash("An error occurred while exporting CSV. Check server logs.", 'error')
        return redirect(url_for('main.lands'))

@main_bp.route('/properties/export.csv')
def export_properties_csv():
    """Export current property selection to CSV (profile-aware)."""
    try:
        from flask import make_response
        try:
            from defusedcsv import csv
        except ImportError:  # pragma: no cover
            import csv
        import io
        import re

        from services.search_profile_service import SearchProfileService

        requested_profile_id = request.args.get('profile_id', type=int)
        default_profile = SearchProfileService.get_default_profile(create=True)
        profiles = SearchProfileService.list_profiles(active_only=True)

        if requested_profile_id:
            selected_profile_id = requested_profile_id
        elif default_profile:
            selected_profile_id = default_profile.id
        elif profiles:
            selected_profile_id = profiles[0].id
        else:
            selected_profile_id = None

        category_filter = request.args.get('category', '')
        subtype_filter = request.args.get('subtype', '')
        municipality_filter = request.args.get('municipality', '')
        search_query = request.args.get('search', '')
        favorites_filter = request.args.get('favorites', '') == 'on'

        hide_removed_param = request.args.get('hide_removed', None)
        form_submitted = any(
            request.args.get(p)
            for p in [
                'profile_id',
                'category',
                'subtype',
                'municipality',
                'search',
                'sort',
                'order',
                'favorites',
                'hide_removed',
            ]
        )
        if form_submitted:
            hide_removed_filter = hide_removed_param == 'on'
        else:
            hide_removed_filter = True

        sort_by = request.args.get('sort', 'created_at')
        sort_order = request.args.get('order', 'desc')

        query = Property.query.options(
            defer(Property.description),
            defer(Property.enhanced_description),
            defer(Property.ai_analysis),
        )
        if selected_profile_id is not None:
            query = query.filter(Property.search_profile_id == selected_profile_id)

        if category_filter:
            query = query.filter(Property.property_category == category_filter)
        if subtype_filter:
            query = query.filter(Property.property_subtype == subtype_filter)
        if municipality_filter:
            query = query.filter(Property.municipality.ilike(f'%{municipality_filter}%'))
        if search_query:
            pattern = f'%{search_query}%'
            query = query.filter(
                or_(
                    Property.title.ilike(pattern),
                    Property.description.ilike(pattern),
                    Property.municipality.ilike(pattern),
                )
            )

        if favorites_filter:
            query = query.filter(Property.is_favorite.is_(True))

        if hide_removed_filter:
            query = query.filter(Property.listing_status.notin_(["removed", "sold"]))

        sort_columns = {
            'created_at': Property.created_at,
            'price': Property.price,
            'area': Property.area,
            'score_total': Property.score_total,
        }
        sort_column = sort_columns.get(sort_by, Property.created_at)
        if sort_order == 'asc':
            props = query.order_by(sort_column.asc().nullslast()).all()
        else:
            props = query.order_by(sort_column.desc().nullslast()).all()

        travel_display_targets = []
        if selected_profile_id is not None:
            try:
                selected_profile = db.session.get(SearchProfile, selected_profile_id)
            except Exception:
                selected_profile = None

            travel_config = SearchProfileService.get_travel_targets_config(selected_profile)
            preset_defs = SearchProfileService.get_travel_preset_defs()
            presets_cfg = travel_config.get("presets") if isinstance(travel_config, dict) else {}

            for preset in preset_defs:
                key = preset.get("key")
                if not key:
                    continue
                enabled = bool((presets_cfg.get(key) or {}).get("enabled", True)) if isinstance(presets_cfg, dict) else True
                if not enabled:
                    continue
                travel_display_targets.append({"key": key, "label": preset.get("label") or key})

            for item in (travel_config.get("custom") or []) if isinstance(travel_config, dict) else []:
                if not isinstance(item, dict):
                    continue
                target_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not target_id or not name:
                    continue
                travel_display_targets.append({"key": f"custom:{target_id}", "label": name})

        def _csv_key(value: str) -> str:
            return re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "")).strip("_") or "target"

        output = io.StringIO()
        writer = csv.writer(output)

        base_header = [
            "ID",
            "Profile ID",
            "Title",
            "URL",
            "Price (EUR)",
            "Area (m²)",
            "Area Type",
            "Price per m²",
            "Bedrooms",
            "Bathrooms",
            "Municipality",
            "Category",
            "Subtype",
            "Status",
            "Favorite",
            "Latitude",
            "Longitude",
            "Created At",
        ]

        travel_headers = []
        for tt in travel_display_targets:
            key = tt.get("key") or ""
            sk = _csv_key(key)
            travel_headers.extend(
                [
                    f"travel_{sk}_duration_min",
                    f"travel_{sk}_distance_km",
                    f"travel_{sk}_mode",
                    f"travel_{sk}_place",
                ]
            )

        writer.writerow(base_header + travel_headers)

        for prop in props:
            price = float(prop.price) if prop.price else None
            area = float(prop.area) if prop.area else None
            price_per_m2 = (price / area) if (price is not None and area) else None

            attrs = prop.attributes if isinstance(prop.attributes, dict) else {}
            bedrooms = attrs.get("bedrooms") if isinstance(attrs, dict) else None
            bathrooms = attrs.get("bathrooms") if isinstance(attrs, dict) else None

            row = [
                prop.id,
                prop.search_profile_id,
                prop.title,
                prop.url,
                price,
                area,
                prop.area_type,
                price_per_m2,
                bedrooms,
                bathrooms,
                prop.municipality,
                prop.property_category,
                prop.property_subtype,
                prop.listing_status,
                bool(prop.is_favorite),
                float(prop.location_lat) if prop.location_lat else None,
                float(prop.location_lon) if prop.location_lon else None,
                prop.created_at.isoformat() if prop.created_at else "",
            ]

            travel = prop.travel if isinstance(prop.travel, dict) else {}
            targets = travel.get("targets") if isinstance(travel, dict) else None
            targets = targets if isinstance(targets, dict) else {}

            for tt in travel_display_targets:
                key = tt.get("key")
                tdata = targets.get(key) if key else None
                if not isinstance(tdata, dict):
                    row.extend([None, None, None, None])
                    continue
                duration_min = tdata.get("duration_min")
                distance_km = tdata.get("distance_km")
                mode = tdata.get("mode")
                place = None
                place_data = tdata.get("place")
                if isinstance(place_data, dict):
                    place = place_data.get("name")
                row.extend([duration_min, distance_km, mode, place])

            writer.writerow(row)

        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv"
        response.headers["Content-Disposition"] = "attachment; filename=idealista_properties.csv"
        return response

    except Exception as e:
        logger.error("Failed to export properties CSV", exc_info=True)
        flash("An error occurred while exporting CSV. Check server logs.", "error")
        return redirect(url_for("main.properties"))

@main_bp.route('/healthz')
def health_check():
    """Health check endpoint"""
    return jsonify({"ok": True})
