from marshmallow import Schema, fields, validate, ValidationError
from typing import Dict, Any

class LandFilterSchema(Schema):
    """Schema for validating land filter parameters"""
    municipality = fields.Str(allow_none=True, validate=validate.Length(max=100))
    land_type = fields.Str(allow_none=True, validate=validate.OneOf(['developed', 'buildable']))
    min_price = fields.Decimal(allow_none=True, validate=validate.Range(min=0))
    max_price = fields.Decimal(allow_none=True, validate=validate.Range(min=0))
    min_area = fields.Decimal(allow_none=True, validate=validate.Range(min=0))
    max_area = fields.Decimal(allow_none=True, validate=validate.Range(min=0))
    search = fields.Str(allow_none=True, validate=validate.Length(max=200))
    sea_view = fields.Bool(allow_none=True)
    sort = fields.Str(allow_none=True, validate=validate.OneOf([
        'score_total', 'score_investment', 'score_lifestyle', 
        'price', 'area', 'created_at', 'municipality', 'travel_time_nearest_beach'
    ]))
    order = fields.Str(allow_none=True, validate=validate.OneOf(['asc', 'desc']))
    page = fields.Int(missing=1, validate=validate.Range(min=1))
    per_page = fields.Int(missing=25, validate=validate.Range(min=10, max=100))

class LandUpdateSchema(Schema):
    """Schema for validating land updates"""
    title = fields.Str(validate=validate.Length(max=500))
    price = fields.Decimal(validate=validate.Range(min=0))
    area = fields.Decimal(validate=validate.Range(min=0))
    municipality = fields.Str(validate=validate.Length(max=255))
    location_lat = fields.Decimal(validate=validate.Range(min=-90, max=90))
    location_lon = fields.Decimal(validate=validate.Range(min=-180, max=180))
    land_type = fields.Str(validate=validate.OneOf(['developed', 'buildable']))
    legal_status = fields.Str(allow_none=True, validate=validate.Length(max=50))

class ScoringCriteriaSchema(Schema):
    """Schema for validating scoring criteria updates"""
    criteria_name = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    profile = fields.Str(required=True, validate=validate.OneOf(['investment', 'lifestyle', 'combined']))
    weight = fields.Decimal(required=True, validate=validate.Range(min=0, max=1))
    active = fields.Bool(missing=True)

class SearchQuerySchema(Schema):
    """Schema for validating search queries"""
    query = fields.Str(required=True, validate=validate.Length(min=2, max=200))
    limit = fields.Int(missing=20, validate=validate.Range(min=1, max=100))

def validate_filters(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and clean filter data"""
    schema = LandFilterSchema()
    try:
        # Remove empty strings and None values
        cleaned_data = {k: v for k, v in data.items() if v not in [None, '', 'None']}
        result = schema.load(cleaned_data)
        return dict(result)  # Ensure Dict type
    except ValidationError as err:
        raise ValueError(f"Invalid filters: {err.messages}")

def validate_land_update(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate land update data"""
    schema = LandUpdateSchema()
    try:
        result = schema.load(data)
        return dict(result)  # Ensure Dict type
    except ValidationError as err:
        raise ValueError(f"Invalid land data: {err.messages}")

def validate_scoring_criteria(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate scoring criteria data"""
    schema = ScoringCriteriaSchema()
    try:
        result = schema.load(data)
        return dict(result)  # Ensure Dict type
    except ValidationError as err:
        raise ValueError(f"Invalid scoring criteria: {err.messages}")

def validate_search_query(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate search query data"""
    schema = SearchQuerySchema()
    try:
        result = schema.load(data)
        return dict(result)  # Ensure Dict type
    except ValidationError as err:
        raise ValueError(f"Invalid search query: {err.messages}")

def validate_price_range(min_price: Any, max_price: Any) -> tuple:
    """Validate price range ensuring min <= max"""
    try:
        if min_price is not None:
            min_price = float(min_price)
            if min_price < 0:
                raise ValueError("Minimum price cannot be negative")
        
        if max_price is not None:
            max_price = float(max_price)
            if max_price < 0:
                raise ValueError("Maximum price cannot be negative")
        
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("Minimum price cannot be greater than maximum price")
        
        return (min_price, max_price)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid price range: {str(e)}")

def validate_area_range(min_area: Any, max_area: Any) -> tuple:
    """Validate area range ensuring min <= max"""
    try:
        if min_area is not None:
            min_area = float(min_area)
            if min_area < 0:
                raise ValueError("Minimum area cannot be negative")
        
        if max_area is not None:
            max_area = float(max_area)
            if max_area < 0:
                raise ValueError("Maximum area cannot be negative")
        
        if min_area is not None and max_area is not None and min_area > max_area:
            raise ValueError("Minimum area cannot be greater than maximum area")
        
        return (min_area, max_area)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid area range: {str(e)}")