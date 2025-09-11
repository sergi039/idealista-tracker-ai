"""Authentication utilities for admin endpoints"""
import os
import hashlib
import hmac
import time
from functools import wraps
from flask import request, jsonify, current_app
import logging

logger = logging.getLogger(__name__)

# Simple in-memory rate limiting
rate_limit_storage = {}

def check_admin_auth():
    """Check if the request has valid admin authentication"""
    # Get admin token from environment
    admin_token = os.environ.get('ADMIN_API_TOKEN')
    
    # If no admin token is configured, log warning and allow access (for backward compatibility)
    if not admin_token:
        logger.warning("ADMIN_API_TOKEN not configured - admin endpoints are unprotected!")
        return True
    
    # Check Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return False
    
    # Support both Bearer token and API-Key formats
    if auth_header.startswith('Bearer '):
        provided_token = auth_header[7:]
    elif auth_header.startswith('API-Key '):
        provided_token = auth_header[8:]
    else:
        provided_token = auth_header
    
    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(provided_token, admin_token)

def admin_required(f):
    """Decorator to require admin authentication for endpoints"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_admin_auth():
            logger.warning(f"Unauthorized access attempt to {request.endpoint} from {request.remote_addr}")
            return jsonify({
                "success": False,
                "error": "Unauthorized. Admin authentication required."
            }), 401
        return f(*args, **kwargs)
    return decorated_function

def rate_limit(max_requests=10, window_seconds=60):
    """Rate limiting decorator
    
    Args:
        max_requests: Maximum number of requests allowed
        window_seconds: Time window in seconds
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Get client identifier (IP address)
            client_id = request.remote_addr
            endpoint = request.endpoint
            key = f"{client_id}:{endpoint}"
            
            current_time = time.time()
            
            # Clean up old entries
            if key in rate_limit_storage:
                rate_limit_storage[key] = [
                    timestamp for timestamp in rate_limit_storage[key]
                    if current_time - timestamp < window_seconds
                ]
            
            # Check rate limit
            if key in rate_limit_storage:
                if len(rate_limit_storage[key]) >= max_requests:
                    logger.warning(f"Rate limit exceeded for {client_id} on {endpoint}")
                    return jsonify({
                        "success": False,
                        "error": f"Rate limit exceeded. Maximum {max_requests} requests per {window_seconds} seconds."
                    }), 429
            else:
                rate_limit_storage[key] = []
            
            # Record this request
            rate_limit_storage[key].append(current_time)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def cleanup_rate_limits():
    """Clean up old rate limit entries (call periodically)"""
    current_time = time.time()
    window = 3600  # Clean entries older than 1 hour
    
    keys_to_delete = []
    for key in rate_limit_storage:
        rate_limit_storage[key] = [
            timestamp for timestamp in rate_limit_storage[key]
            if current_time - timestamp < window
        ]
        if not rate_limit_storage[key]:
            keys_to_delete.append(key)
    
    for key in keys_to_delete:
        del rate_limit_storage[key]