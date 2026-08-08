"""
Language switching routes
"""

import logging
from flask import Blueprint, request, jsonify
from utils.i18n import set_language

logger = logging.getLogger(__name__)

language_bp = Blueprint("language", __name__)


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

    except Exception:
        logger.error("Failed to set language", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "An internal error occurred. Check server logs for details.",
            }
        ), 500
