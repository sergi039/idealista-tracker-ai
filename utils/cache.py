"""Caching utilities for the application"""
import os
import hashlib
import json
from flask_caching import Cache
from functools import wraps
import logging

logger = logging.getLogger(__name__)

cache = Cache()

def init_cache(app):
    """Initialize caching with appropriate backend"""
    redis_url = os.environ.get('REDIS_URL')
    
    if redis_url:
        # Use Redis if available
        cache_config = {
            'CACHE_TYPE': 'redis',
            'CACHE_REDIS_URL': redis_url,
            'CACHE_DEFAULT_TIMEOUT': 300  # 5 minutes default
        }
        logger.info("Using Redis for caching")
    else:
        # Fall back to simple in-memory cache
        cache_config = {
            'CACHE_TYPE': 'simple',
            'CACHE_DEFAULT_TIMEOUT': 300
        }
        logger.info("Using in-memory caching (Redis not configured)")
    
    app.config.update(cache_config)
    cache.init_app(app)
    return cache

def cache_key_from_args(*args, **kwargs):
    """Generate a cache key from function arguments"""
    key_parts = []
    
    # Add function arguments
    for arg in args:
        if hasattr(arg, 'id'):
            key_parts.append(f"id:{arg.id}")
        else:
            key_parts.append(str(arg))
    
    # Add keyword arguments
    for k, v in sorted(kwargs.items()):
        key_parts.append(f"{k}:{v}")
    
    # Create hash of all parts for a shorter key
    key_str = "|".join(key_parts)
    return hashlib.md5(key_str.encode()).hexdigest()

def cache_api_response(timeout=300):
    """Decorator to cache API responses"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Generate cache key
            cache_key = f"api:{f.__name__}:{cache_key_from_args(*args, **kwargs)}"
            
            # Try to get from cache
            cached_value = cache.get(cache_key)
            if cached_value is not None:
                logger.debug(f"Cache hit for {cache_key}")
                return cached_value
            
            # Call function and cache result
            result = f(*args, **kwargs)
            cache.set(cache_key, result, timeout=timeout)
            logger.debug(f"Cached result for {cache_key}")
            
            return result
        return decorated_function
    return decorator

def cache_enrichment_data(lat, lon, data_type, data, timeout=86400):
    """Cache enrichment data by location (24 hours default)"""
    # Round coordinates to 4 decimal places for caching (about 11 meters precision)
    lat_rounded = round(lat, 4)
    lon_rounded = round(lon, 4)
    
    cache_key = f"enrichment:{data_type}:{lat_rounded}:{lon_rounded}"
    cache.set(cache_key, data, timeout=timeout)
    logger.debug(f"Cached enrichment data: {cache_key}")

def get_cached_enrichment_data(lat, lon, data_type):
    """Get cached enrichment data if available"""
    lat_rounded = round(lat, 4)
    lon_rounded = round(lon, 4)
    
    cache_key = f"enrichment:{data_type}:{lat_rounded}:{lon_rounded}"
    data = cache.get(cache_key)
    
    if data is not None:
        logger.debug(f"Enrichment cache hit: {cache_key}")
    
    return data

def clear_cache_pattern(pattern):
    """Clear all cache entries matching a pattern (Redis only)"""
    try:
        if cache.config.get('CACHE_TYPE') == 'redis' and hasattr(cache.cache, '_write_client'):
            # Redis backend
            client = cache.cache._write_client
            for key in client.scan_iter(match=pattern):
                client.delete(key)
            logger.info(f"Cleared cache entries matching pattern: {pattern}")
        else:
            # Simple cache doesn't support pattern clearing
            logger.warning("Pattern cache clearing not supported with simple cache backend")
    except AttributeError:
        logger.warning("Cache backend doesn't support pattern clearing")

def get_cache_stats():
    """Get cache statistics"""
    stats = {
        'backend': cache.config.get('CACHE_TYPE', 'unknown') if cache.config else 'unknown',
        'available': True
    }
    
    if stats['backend'] == 'redis':
        try:
            if hasattr(cache.cache, '_write_client'):
                client = cache.cache._write_client
                info = client.info()
                stats.update({
                    'used_memory': info.get('used_memory_human', 'N/A'),
                    'connected_clients': info.get('connected_clients', 0),
                    'total_commands': info.get('total_commands_processed', 0)
                })
        except Exception as e:
            logger.error(f"Failed to get Redis stats: {e}")
            stats['error'] = str(e)
    
    return stats