"""Classification of Google Maps Platform responses.

Every API this app calls (Places Nearby Search, Distance Matrix, Geocoding)
answers HTTP 200 both for "there is nothing there" and for "this project is not
allowed to ask". A caller that only looks at the result list cannot tell the two
apart, and #98 is what happens when it does not: `REQUEST_DENIED` was recorded
as `status: "not_found"` and the enrichment run reported success.

`read_api_payload` is the single place that draws the line. It hands back the
payload only when Google actually answered, and a `GoogleApiFailure` carrying a
stable reason code when the request was refused, throttled or never made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Reason codes are persisted in JSON columns and matched by tests: keep stable.
REASON_NO_API_KEY = "no_api_key"
REASON_NETWORK_ERROR = "network_error"
REASON_HTTP_ERROR = "http_error"
REASON_MALFORMED_RESPONSE = "malformed_response"
REASON_REQUEST_DENIED = "request_denied"
REASON_OVER_QUERY_LIMIT = "over_query_limit"
REASON_INVALID_REQUEST = "invalid_request"
REASON_UNKNOWN_ERROR = "unknown_error"
# The caller's wall-clock budget ran out before this lookup could finish
# (#434). Distinct from `network_error` because it is the one refusal that
# says nothing about the endpoint: the host may be perfectly healthy and the
# clock simply spent by whatever ran before it. A caller walking a fallback
# list must read it as "stop", never as "this instance is bad, try the next" --
# the next one would answer the same, one gate wait later.
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

# Top-level payload statuses that mean "Google answered". ZERO_RESULTS is an
# answer: there really is nothing matching nearby. An absent status is treated
# as an answer too - Distance Matrix omits it in some responses.
ANSWERED_STATUSES = frozenset({"OK", "ZERO_RESULTS"})

_STATUS_REASONS: Dict[str, str] = {
    "REQUEST_DENIED": REASON_REQUEST_DENIED,
    "OVER_QUERY_LIMIT": REASON_OVER_QUERY_LIMIT,
    "OVER_DAILY_LIMIT": REASON_OVER_QUERY_LIMIT,
    "RESOURCE_EXHAUSTED": REASON_OVER_QUERY_LIMIT,
    "INVALID_REQUEST": REASON_INVALID_REQUEST,
    "MAX_ELEMENTS_EXCEEDED": REASON_INVALID_REQUEST,
    "MAX_DIMENSIONS_EXCEEDED": REASON_INVALID_REQUEST,
    "MAX_WAYPOINTS_EXCEEDED": REASON_INVALID_REQUEST,
    "UNKNOWN_ERROR": REASON_UNKNOWN_ERROR,
}

# Google's error_message goes to the server log only, never into a JSON column
# rendered back to the browser.
MAX_MESSAGE_LENGTH = 200


@dataclass(frozen=True)
class GoogleApiFailure:
    """A Google request that did not produce an answer.

    Distinct from an empty answer on purpose: a failure must never be stored as
    a search result.
    """

    reason: str
    status: Optional[str] = None
    http_status: Optional[int] = None
    message: Optional[str] = None

    def describe(self) -> str:
        """Log-friendly one-liner with the code Google actually returned."""
        parts = [self.reason]
        detail = []
        if self.status:
            detail.append(self.status)
        if self.http_status is not None:
            detail.append(f"HTTP {self.http_status}")
        if self.message:
            text = " ".join(str(self.message).split())
            if len(text) > MAX_MESSAGE_LENGTH:
                text = text[: MAX_MESSAGE_LENGTH - 3] + "..."
            detail.append(text)
        if detail:
            parts.append("(" + ": ".join(detail) + ")")
        return " ".join(parts)


def failure_from_exception(exc: BaseException) -> GoogleApiFailure:
    """Classify a transport-level exception raised while calling Google."""
    # Imported lazily so this module stays importable without requests.
    import requests

    from utils.http import LookupBudgetExceeded

    if isinstance(exc, LookupBudgetExceeded):
        # Checked first: it subclasses RequestException, so the branch below
        # would otherwise call a spent clock a network error and send a
        # fallback walk on to the next instance for nothing.
        return GoogleApiFailure(reason=REASON_BUDGET_EXHAUSTED, message=str(exc))
    reason = (
        REASON_NETWORK_ERROR
        if isinstance(exc, requests.RequestException)
        else REASON_UNKNOWN_ERROR
    )
    return GoogleApiFailure(reason=reason, message=str(exc))


def read_api_payload(
    response: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[GoogleApiFailure]]:
    """Split a Google response into (payload, failure).

    Exactly one of the two is set. A payload means Google answered and the
    caller may read its results - including an empty result list, which is a
    legitimate "nothing nearby". A failure means the answer never arrived and
    the caller must not record anything as a search result.
    """
    if response is None:
        return None, GoogleApiFailure(reason=REASON_NETWORK_ERROR)

    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        return None, GoogleApiFailure(
            reason=REASON_HTTP_ERROR,
            http_status=status_code if isinstance(status_code, int) else None,
        )

    try:
        payload = response.json()
    except Exception as exc:
        return None, GoogleApiFailure(
            reason=REASON_MALFORMED_RESPONSE, message=str(exc)
        )

    if not isinstance(payload, dict):
        return None, GoogleApiFailure(
            reason=REASON_MALFORMED_RESPONSE,
            message=f"expected an object, got {type(payload).__name__}",
        )

    status = str(payload.get("status") or "").strip().upper()
    if status and status not in ANSWERED_STATUSES:
        return None, GoogleApiFailure(
            reason=_STATUS_REASONS.get(status, REASON_UNKNOWN_ERROR),
            status=status,
            message=payload.get("error_message"),
        )

    return payload, None
