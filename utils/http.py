import logging
import random
import threading
import time
from typing import Callable, Iterable, Optional

import requests

_DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)

# The User-Agent to send to public, unauthenticated APIs.
#
# overpass-api.de answers `406 Not Acceptable` to the default
# `python-requests/x.y.z` User-Agent, and also to any UA carrying a
# parenthetical comment -- measured against the live instance, not guessed:
# `IdealistaRank/1.0 (personal property tracker)` is refused while the bare
# product token is served. Keep this a plain token.
HTTP_USER_AGENT = "IdealistaRank/1.0"

# overpass-api.de grants two query slots per IP and answers 504 while both are
# busy. Two seconds between calls keeps a bulk run inside that budget.
OVERPASS_MIN_INTERVAL_S = 2.0


class RateGate:
    """Pace the calls this process makes to one shared endpoint.

    Each caller *reserves* the next slot under the lock and then sleeps until
    it, outside the lock. Reserving is what makes this work across threads: two
    workers arriving together would otherwise measure the same gap, both decide
    it had passed, and fire together. Sleeping outside the lock is what keeps a
    finishing call's `mark()` from blocking for a whole interval behind
    somebody else's wait.

    It paces when a call is *allowed to start*, not when it reaches the wire:
    a thread descheduled between its permit and its socket can still overlap
    the next one. Overpass grants two concurrent slots per IP, so that margin
    is the design rather than a hole in it -- this is a courtesy pacer, not an
    admission-control system.

    `wait()` before the request, `mark()` when it returns, so a call slower
    than the interval still spaces the next one from its end rather than its
    start.
    """

    def __init__(self, min_interval_s: float, name: str = ""):
        self.min_interval_s = min_interval_s
        self.name = name
        self._lock = threading.Lock()
        self._next_slot_at = 0.0

    def _interval(self) -> float:
        """The interval to honour. A negative one would walk the slot backwards
        and hand two callers the same moment, so it reads as no pacing."""
        return max(0.0, self.min_interval_s)

    def wait(self) -> float:
        """Block until this caller's slot. Returns the seconds slept."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot_at)
            self._next_slot_at = slot + self._interval()

        delay = slot - now
        if delay > 0:
            time.sleep(delay)
            return delay
        return 0.0

    def mark(self) -> None:
        """Record that a call has just finished.

        Never pulls an already-reserved slot backwards: a waiter that has been
        promised a time is not overtaken by a call finishing before it.
        """
        with self._lock:
            self._next_slot_at = max(
                self._next_slot_at, time.monotonic() + self._interval()
            )


# One gate for every Overpass caller in the process: the coastline query in
# services/sea_view_service.py and the amenity query in
# services/enrichment_service.py hit the same two per-IP slots, so pacing them
# separately would pace neither.
OVERPASS_GATE = RateGate(OVERPASS_MIN_INTERVAL_S, name="overpass")


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
            # A discarded response has to release its connection. Buffered bodies
            # are already read, so this is a no-op for them, but a stream=True
            # caller would otherwise leak the socket on every retry.
            response.close()
            time.sleep(_compute_backoff(attempt, backoff_base, backoff_max))
            continue

        return response

    if last_exc:
        raise last_exc
    return request_fn(*args, **kwargs)
