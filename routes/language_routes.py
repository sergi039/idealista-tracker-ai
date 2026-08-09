"""
Language switching routes
"""

import logging
from flask import Blueprint, Response, request, jsonify
from werkzeug.exceptions import HTTPException

from utils.api_errors import json_http_error
from utils.i18n import set_language

logger = logging.getLogger(__name__)

language_bp = Blueprint("language", __name__)


@language_bp.errorhandler(HTTPException)
def handle_language_http_exception(error: HTTPException) -> tuple[Response, int]:
    """This blueprint is mounted under /api too, so it answers JSON (#140)."""
    return json_http_error(error)


@language_bp.route("/set-language", methods=["POST"])
def set_user_language():
    """Set user's preferred language"""
    try:
        data = request.get_json() or {}
        language = data.get("language", "en")

        if set_language(language):
            return jsonify(
                {
                    "success": True,
                    "message": "Language updated successfully",
                    "language": language,
                }
            )
        else:
            return jsonify({"success": False, "error": "Invalid language code"}), 400

    except HTTPException:
        # A body that is not JSON is a 415 from get_json(), not a server fault;
        # the blueprint handler above turns it into the JSON envelope (#140).
        raise
    except Exception:
        logger.error("Failed to set language", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500
