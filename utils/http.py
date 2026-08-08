import logging
import random
import time
from typing import Callable, Iterable, Optional

import requests

_DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)


def _compute_backoff(attempt: int, base: float, max_delay: float) -> float:
    jitter = random.uniform(0, 0.2)
    delay = base * (2 ** max(attempt - 1, 0))
    return min(max_delay, delay + jitter)


def request_with_retries(
    request_fn: Callable[..., requests.Response],
    *args,
    max_attempts: int = 3,
    retryable_statuses: Optional[Iterable[int]] = None,
    backoff_base: float = 0.5,
    backoff_max: float = 5.0,
    timeout: Optional[float] = None,
    logger: Optional[logging.Logger] = None,
    **kwargs,
) -> requests.Response:
    if max_attempts < 1:
        max_attempts = 1

    statuses = (
        tuple(retryable_statuses)
        if retryable_statuses is not None
        else _DEFAULT_RETRY_STATUSES
    )
    if timeout is not None and "timeout" not in kwargs:
        kwargs["timeout"] = timeout

    last_exc = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = request_fn(*args, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            if logger:
                logger.warning(
                    "Request failed (%s). Retrying %s/%s.", exc, attempt, max_attempts
                )
            time.sleep(_compute_backoff(attempt, backoff_base, backoff_max))
            continue

        if response.status_code in statuses and attempt < max_attempts:
            if logger:
                logger.warning(
                    "Request returned %s. Retrying %s/%s.",
                    response.status_code,
                    attempt,
                    max_attempts,
                )
            time.sleep(_compute_backoff(attempt, backoff_base, backoff_max))
            continue

        return response

    if last_exc:
        raise last_exc
    return request_fn(*args, **kwargs)
