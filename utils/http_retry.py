"""Shared HTTP retry helper with exponential backoff."""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_RETRIES = 3
_DEFAULT_BACKOFF = 1.5  # seconds; doubles each retry
_RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = _DEFAULT_RETRIES,
    backoff: float = _DEFAULT_BACKOFF,
    timeout: int = 20,
    **kwargs,
) -> Optional[requests.Response]:
    """Execute an HTTP request with exponential backoff on transient errors.

    Returns the Response on success, or None if all retries exhausted.
    Caller should inspect .status_code / .json() as usual.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code not in _RETRIABLE_STATUS_CODES:
                return resp
            # Retriable HTTP status — log and retry
            logger.warning(
                "HTTP %s %s returned %s (attempt %s/%s)",
                method.upper(), url, resp.status_code, attempt + 1, max_retries + 1,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            logger.warning(
                "HTTP %s %s failed (%s, attempt %s/%s)",
                method.upper(), url, type(exc).__name__, attempt + 1, max_retries + 1,
            )
            last_exc = exc
            resp = None

        if attempt < max_retries:
            sleep_time = backoff * (2 ** attempt)
            time.sleep(sleep_time)

    # All retries exhausted
    if resp is not None:
        return resp  # Return last response even if retriable status
    if last_exc:
        logger.error("All retries exhausted for %s %s", method.upper(), url)
    return None
