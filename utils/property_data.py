"""
Property data utilities for JSON normalization and data handling
"""
import json
import logging
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

def normalize_property_details(property_details: Any) -> Dict:
    """
    Normalize property_details field to ensure it's always a dict.
    Handles conversion from JSON string to dict if needed.
    
    Args:
        property_details: Can be None, dict, or JSON string
        
    Returns:
        dict: Normalized property_details as a dictionary
    """
    try:
        # If None or empty, return empty dict
        if not property_details:
            return {}
        
        # If already a dict, return as-is
        if isinstance(property_details, dict):
            return property_details
        
        # If it's a string, try to parse as JSON
        if isinstance(property_details, str):
            try:
                parsed = json.loads(property_details)
                if isinstance(parsed, dict):
                    return parsed
                else:
                    logger.warning(f"Property details JSON parsed to non-dict type: {type(parsed)}")
                    return {}
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse property_details JSON string: {e}")
                return {}
        
        # For any other type, log warning and return empty dict
        logger.warning(f"Property details has unexpected type: {type(property_details)}")
        return {}
        
    except Exception as e:
        logger.error(f"Error normalizing property_details: {e}")
        return {}

def ensure_property_details_dict(land) -> None:
    """
    Ensure a Land model's property_details field is normalized to a dict.
    Modifies the land object in place.
    
    Args:
        land: Land model instance
    """
    try:
        land.property_details = normalize_property_details(land.property_details)
    except Exception as e:
        logger.error(f"Error ensuring property_details dict for land {getattr(land, 'id', 'unknown')}: {e}")
        land.property_details = {}

def update_property_details_section(property_details: Dict, section_key: str, section_data: Dict) -> Dict:
    """
    Safely update a section within property_details dict.
    
    Args:
        property_details: Existing property_details dict
        section_key: Key for the section to update (e.g., 'idealista')
        section_data: Data to set for this section
        
    Returns:
        dict: Updated property_details
    """
    try:
        if not isinstance(property_details, dict):
            property_details = {}
        
        property_details[section_key] = section_data
        return property_details
        
    except Exception as e:
        logger.error(f"Error updating property_details section '{section_key}': {e}")
        return property_details or {}