"""The JSON error envelope for the /api surface (issue #140).

Every handler under `/api` reports its own failures as
`{"success": false, "error": "..."}`, but the 4xx werkzeug raises *for* it
arrived as werkzeug's HTML error page: `db.get_or_404()` on an unknown id
(a real 404 since #138), `request.get_json()` on a body that is not JSON
(415), a URL matching no rule at all (404), a rule that rejects the method
(405). A client parsing the answer got a syntax error instead of the reason.

Two registrations are needed, because they see different failures:

* `HTTPException` on `api_bp` / `language_bp` (registered in the blueprint
  modules themselves) catches what a *view* raises -- everything above
  except the last two.
* `http_error_response` at app level catches the ones raised before dispatch,
  where `request.blueprint` is None because no rule matched. It answers JSON
  only for the API surface and hands everything else back untouched, so the
  pages keep werkzeug's HTML page: this is an API contract, not an
  application-wide one.

`RoutingException` -- the 308 werkzeug raises to append a missing trailing
slash -- reaches neither: Flask returns it ahead of the handler lookup, which
is what keeps its `Location` header intact.
"""

from __future__ import annotations

from flask import Response, jsonify, request
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

# Both JSON blueprints are registered under this prefix in app.py.
API_URL_PREFIX = "/api"

# Blueprints whose views answer JSON. `request.blueprint` is None for an error
# raised before dispatch, which is why the path is checked alongside it.
JSON_BLUEPRINTS = frozenset({"api", "language"})

# An HTTPException carrying no code never reaches a handler (Flask returns it
# directly), so this is a floor rather than a behaviour anyone should see.
FALLBACK_ERROR_CODE = 500


def json_http_error(error: HTTPException) -> tuple[Response, int]:
    """Render an HTTPException as the envelope the /api handlers already use.

    `description` is werkzeug's own wording for the status ("The requested URL
    was not found on the server...") unless the raiser supplied one; `name`
    backs it up so `error` is never empty.
    """
    return jsonify({"success": False, "error": error.description or error.name}), (
        error.code or FALLBACK_ERROR_CODE
    )


def is_json_api_request() -> bool:
    """True when the current request is aimed at the JSON API surface."""
    if request.blueprint in JSON_BLUEPRINTS:
        return True
    path = request.path
    return path == API_URL_PREFIX or path.startswith(f"{API_URL_PREFIX}/")


def http_error_response(error: HTTPException) -> ResponseReturnValue:
    """App-level handler: JSON under /api, werkzeug's HTML page everywhere else.

    Returning the exception itself is Flask's pass-through idiom -- it is a
    WSGI application, so the default error page is rendered exactly as it would
    have been with no handler registered.
    """
    if is_json_api_request():
        return json_http_error(error)
    return error
