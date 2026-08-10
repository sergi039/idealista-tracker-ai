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

# overpass-api.de grants two query slots per IP, answers 504 while both are
# busy, and answers 429 when the *rate* is too high for the slots it is willing
# to hand out.
#
# Two seconds was a guess at the slot count. Measured on 2026-08-09 by pacing
# 20 amenity lookups at that interval: 39 requests for 20 answers -- 16 served,
# 8 refused with 504, and 15 with 429. Nothing was mis-recorded, because
# `_DEFAULT_RETRY_STATUSES` above already retries both, but more than half the
# traffic was the server telling us to slow down, and a run over the whole
# table would have spent hours doing it. Five seconds is what that measurement
# supports, and it costs an interactive Enrich nothing: the gate is idle
# between presses, so a single lookup never waits at all.
OVERPASS_MIN_INTERVAL_S = 5.0


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
    gate: Optional[RateGate] = None,
    **kwargs,
) -> requests.Response:
    """Issue a request, retrying the statuses the server retries on.

    `gate` paces **every attempt**, not just the first. A caller that took the
    gate itself and then handed the retry loop a free hand paced its lookups
    while leaving the retries unpaced -- and a retry storm is precisely when
    the endpoint is asking for less traffic, so the pacing came off exactly
    where it was needed. Measured during the #152 backfill: 5 s between
    lookups still drew more 429s than 504s, because each refusal was answered
    by a burst the gate never saw.

    The backoff and the gate are not redundant. The backoff is what *this*
    server just asked for; the gate is what this process allows itself across
    every caller. Waiting for the gate after the backoff yields the longer of
    the two: a backoff already past the next slot costs nothing extra.
    """
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
        if gate is not None:
            gate.wait()
        try:
            response = request_fn(*args, **kwargs)
        except requests.RequestException as exc:
            if gate is not None:
                gate.mark()
            last_exc = exc
            if attempt >= max_attempts:
                raise
            if logger:
                logger.warning(
                    "Request failed (%s). Retrying %s/%s.", exc, attempt, max_attempts
                )
            time.sleep(_compute_backoff(attempt, backoff_base, backoff_max))
            continue

        if gate is not None:
            gate.mark()

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
    # Unreachable: the loop either returns, raises, or continues. Kept as a
    # belt-and-braces final attempt, and it takes the gate like any other.
    if gate is not None:
        gate.wait()
    try:
        return request_fn(*args, **kwargs)
    finally:
        if gate is not None:
            gate.mark()
