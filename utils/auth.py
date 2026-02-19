"""Authentication utilities for admin endpoints"""
import os
import hmac
from functools import wraps
from flask import request, jsonify, session, redirect, url_for, current_app
import logging

logger = logging.getLogger(__name__)


def check_admin_auth():
    """Check if the request has valid admin authentication.

    Auth sources (checked in order):
    1. Flask TESTING flag (unit-test bypass)
    2. Authorization header (Bearer / API-Key) with ADMIN_API_TOKEN
    3. Flask session with admin_authenticated flag

    FAIL-CLOSED: if ADMIN_API_TOKEN is not configured, access is denied.
    """
    # 1. Test mode bypass
    try:
        if current_app and current_app.config.get('TESTING'):
            return True
    except Exception:
        pass

    # Get admin token from environment
    admin_token = os.environ.get('ADMIN_API_TOKEN', '').strip()

    # FAIL-CLOSED: no token configured = no access
    if not admin_token:
        logger.error("ADMIN_API_TOKEN not configured - denying access (fail-closed)")
        return False

    # 2. Check Authorization header (API / programmatic access)
    auth_header = request.headers.get('Authorization', '')
    if auth_header:
        if auth_header.startswith('Bearer '):
            provided_token = auth_header[7:]
        elif auth_header.startswith('API-Key '):
            provided_token = auth_header[8:]
        else:
            provided_token = auth_header

        if hmac.compare_digest(provided_token, admin_token):
            return True

    # 3. Check Flask session (browser-based access via login page)
    if session.get('admin_authenticated'):
        return True

    return False


def admin_required(f):
    """Decorator to require admin authentication.

    For API routes (blueprint='api' or JSON request): returns 401 JSON.
    For page routes: redirects to login page.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_admin_auth():
            logger.warning(
                "Unauthorized access attempt to %s from %s",
                request.endpoint,
                request.remote_addr,
            )
            # API routes return JSON 401
            if _is_api_request():
                return jsonify({
                    "success": False,
                    "error": "Unauthorized. Admin authentication required."
                }), 401
            # Page routes redirect to login
            return redirect(url_for('main.login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def _is_api_request():
    """Determine if request expects JSON response."""
    if request.blueprint == 'api':
        return True
    if request.is_json:
        return True
    accept = request.headers.get('Accept', '')
    if 'application/json' in accept:
        return True
    return False


def login_admin(token):
    """Validate token and set admin session. Returns True on success."""
    admin_token = os.environ.get('ADMIN_API_TOKEN', '').strip()
    if not admin_token:
        return False
    if hmac.compare_digest(token, admin_token):
        session['admin_authenticated'] = True
        session.permanent = True
        return True
    return False


def logout_admin():
    """Clear admin session."""
    session.pop('admin_authenticated', None)
