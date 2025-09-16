from typing import Optional, Dict, Any
from decimal import Decimal
from sqlalchemy import or_
from sqlalchemy.orm import defer
from models import Land, LandTypeEnum
from app import db

class LandService:
    """Service layer for Land model operations"""
    
    @staticmethod
    def get_filtered_lands(filters: Dict[str, Any], page: int, per_page: int, detail_view: bool = False):
        """Get lands with filters applied and optional deferred loading"""
        base_query = Land.query
        
        # Conditional defer for performance - only load heavy JSONB fields when needed
        if not detail_view:
            base_query = base_query.options(
                defer(Land.infrastructure_basic),
                defer(Land.infrastructure_extended),
                defer(Land.transport),
                defer(Land.environment),
                defer(Land.neighborhood),
                defer(Land.ai_analysis),
                defer(Land.enhanced_description),
                defer(Land.property_details),
                defer(Land.description)
            )
        
        # Apply filters
        if filters.get('municipality'):
            base_query = base_query.filter(Land.municipality.ilike(f"%{filters['municipality']}%"))
        
        if filters.get('land_type'):
            # Ensure enum type compatibility
            try:
                land_type_enum = LandTypeEnum(filters['land_type'])
                base_query = base_query.filter(Land.land_type == land_type_enum)
            except ValueError:
                # Skip invalid land type
                pass
        
        if filters.get('min_price'):
            base_query = base_query.filter(Land.price >= Decimal(str(filters['min_price'])))
            
        if filters.get('max_price'):
            base_query = base_query.filter(Land.price <= Decimal(str(filters['max_price'])))
        
        if filters.get('min_area'):
            base_query = base_query.filter(Land.area >= Decimal(str(filters['min_area'])))
            
        if filters.get('max_area'):
            base_query = base_query.filter(Land.area <= Decimal(str(filters['max_area'])))
        
        # Search across title, description, and municipality
        if filters.get('search'):
            search_pattern = f"%{filters['search']}%"
            base_query = base_query.filter(
                or_(
                    Land.title.ilike(search_pattern),
                    Land.description.ilike(search_pattern),
                    Land.municipality.ilike(search_pattern)
                )
            )
        
        # Sea view filter
        if filters.get('sea_view'):
            base_query = base_query.filter(Land.environment['sea_view'].astext == 'true')
        
        # Apply sorting with NULL values last (default to score_total descending)
        sort_field = filters.get('sort', 'score_total')
        sort_order = filters.get('order', 'desc')
        
        if hasattr(Land, sort_field):
            sort_column = getattr(Land, sort_field)
            if sort_order == 'asc':
                base_query = base_query.order_by(sort_column.asc().nullslast())
            else:
                base_query = base_query.order_by(sort_column.desc().nullslast())
        else:
            # Fallback to score_total desc with nulls last
            base_query = base_query.order_by(Land.score_total.desc().nullslast())
            
        return base_query.paginate(page=page, per_page=per_page, error_out=False)
    
    @staticmethod
    def get_land_by_id(land_id: int) -> Optional[Land]:
        """Get single land with all data loaded"""
        return Land.query.filter_by(id=land_id).first()
    
    @staticmethod
    def get_land_summary_stats() -> Dict[str, Any]:
        """Get summary statistics for all lands"""
        total_count = Land.query.count()
        
        # Price statistics
        price_stats = db.session.query(
            db.func.min(Land.price).label('min_price'),
            db.func.max(Land.price).label('max_price'), 
            db.func.avg(Land.price).label('avg_price')
        ).filter(Land.price.isnot(None)).first()
        
        # Municipality distribution
        municipality_count = db.session.query(
            Land.municipality,
            db.func.count(Land.id).label('count')
        ).group_by(Land.municipality).limit(10).all()
        
        return {
            'total_count': total_count,
            'min_price': float(price_stats.min_price) if price_stats and price_stats.min_price else None,
            'max_price': float(price_stats.max_price) if price_stats and price_stats.max_price else None,
            'avg_price': float(price_stats.avg_price) if price_stats and price_stats.avg_price else None,
            'top_municipalities': [
                {'municipality': m.municipality, 'count': m.count} 
                for m in municipality_count if m.municipality
            ]
        }
    
    @staticmethod
    def search_lands(search_term: str, limit: int = 20) -> list[Land]:
        """Search lands by title or description"""
        if not search_term:
            return []
            
        search_pattern = f"%{search_term}%"
        return Land.query.filter(
            or_(
                Land.title.ilike(search_pattern),
                Land.description.ilike(search_pattern),
                Land.municipality.ilike(search_pattern)
            )
        ).limit(limit).all()
    
    @staticmethod
    def get_recent_lands(limit: int = 10) -> list[Land]:
        """Get recently added lands"""
        return Land.query.order_by(Land.created_at.desc()).limit(limit).all()
    
    @staticmethod
    def get_lands_by_score_range(min_score: float, max_score: float = 100.0) -> list[Land]:
        """Get lands within a specific score range"""
        return Land.query.filter(
            Land.score_total >= min_score,
            Land.score_total <= max_score
        ).order_by(Land.score_total.desc()).all()