import contextvars
import logging
import random
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Optional, Tuple, Union

import requests

# What a caller may hand to `timeout=`. `requests` accepts a scalar, which
# urllib3 expands to `connect=read=value` -- so a 60 s read allowance for an
# Overpass query that genuinely computes for a minute also granted 60 s to
# learn that a host is not answering its SYN. The tuple form separates the
# two; see `request_with_retries` and the Overpass transports for the
# measurements behind the numbers they pass.
Timeout = Union[float, Tuple[float, float]]

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


class LookupBudgetExceeded(requests.RequestException):
    """The wall-clock budget for this lookup ran out before it could finish.

    A `RequestException` on purpose: every caller of `request_with_retries`
    already classifies one as a transport refusal and records an honest
    absence for it, so a budget that runs out reads as "nobody looked" rather
    than as a measurement (#98). What it must never become is a *different*
    kind of answer -- the cause it interrupted is chained onto it so an
    operator reading the log sees which host was silent, not only that a
    ceiling was reached.
    """


# The deadline the free lookups inside a `lookup_budget(...)` block share.
#
# A context variable rather than an argument threaded through eleven call
# sites: the steps of one enrichment run reach Overpass through four
# different services, and a parameter every one of them has to forward is a
# parameter one of them will not. A thread started by the background-job
# executor begins with this unset, which is the safe default -- no budget, and
# today's behaviour.
#
# **Only the free lookups read it.** `services/enrichment_service.py`'s
# Overpass transport, the coastline and elevation queries in
# `services/sea_view_service.py`. Google's paid transports deliberately do not,
# and the reason is #178's: a Distance Matrix request abandoned because a free
# source spent the clock is a paid measurement nobody made, and the owner's
# next press pays for it again. The budget exists to stop an advisory step
# holding a paid one hostage; spending it on the paid one would be the same
# defect with the roles swapped.
_LOOKUP_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "lookup_deadline", default=None
)


@contextmanager
def lookup_budget(seconds: float):
    """Bound the wall-clock time the free lookups inside may spend waiting.

    Nested budgets take the *earlier* deadline: an inner block may ask for
    less than the run it sits in, never for more, so no single lookup can
    extend the ceiling its caller stated.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    outer = _LOOKUP_DEADLINE.get()
    if outer is not None:
        deadline = min(deadline, outer)
    token = _LOOKUP_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _LOOKUP_DEADLINE.reset(token)


def lookup_deadline() -> Optional[float]:
    """The ambient deadline, or None when no budget is open."""
    return _LOOKUP_DEADLINE.get()


def earliest_deadline(*deadlines: Optional[float]) -> Optional[float]:
    """The soonest of the deadlines given, ignoring the absent ones."""
    present = [d for d in deadlines if d is not None]
    return min(present) if present else None


def _is_silence(exc: BaseException) -> bool:
    """Whether no HTTP response arrived at all.

    The distinction the retry policy turns on. A server that answers `429` or
    `504` is alive and asking for a moment -- #144 measured that an Overpass
    slot frees up in about a minute, which is what the patient 8-16-32 backoff
    was sized for, and what it is still right for. Silence is the opposite
    fact: the host is unreachable or hung, waiting 56 s changes nothing about
    it, and the next instance in the fallback list is the thing worth trying.

    `ConnectionError` covers a refused or blackholed connect (and
    `ConnectTimeout`, which subclasses both); `Timeout` covers a handshake
    that completed onto a host that then never sent a byte -- measured on the
    mini 2026-08-20, kumi.systems connected in 0.109 s and said nothing for
    30 s, so a connect-only rule would have missed the instance that cost the
    most.
    """
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _clamped_timeout(timeout: Optional[Timeout], remaining: float) -> Timeout:
    """`timeout`, with no leg allowed to outlive the remaining budget."""
    if timeout is None:
        return remaining
    if isinstance(timeout, tuple):
        connect, read = timeout
        return (min(connect, remaining), min(read, remaining))
    return min(timeout, remaining)


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
    timeout: Optional[Timeout] = None,
    silence_max_attempts: Optional[int] = None,
    deadline: Optional[float] = None,
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

    `silence_max_attempts` splits the retry policy by what the failure *means*
    (#434). The patient budget above answers a server that spoke: `429` and
    `504` mean "come back shortly", and #144 measured that shortly is about a
    minute. Silence means the host is unreachable or hung, and retrying it is
    the one thing that cannot help -- a caller with somewhere else to go says
    `silence_max_attempts=1` and gets there after one attempt instead of
    four. Left unset, silence keeps the same budget as everything else, which
    is what every caller that has no second instance still wants.

    `deadline` is a `time.monotonic()` ceiling for the whole call, retries and
    backoff included. It clamps each attempt's timeout to what is left, never
    sleeps past it, and raises `LookupBudgetExceeded` rather than starting an
    attempt it cannot finish -- so a budget already spent costs no socket at
    all. A caller inside a `lookup_budget(...)` block passes
    `earliest_deadline(lookup_deadline(), its_own)`; nothing here reads the
    ambient budget on a caller's behalf, because the paid transports must not
    honour it (see `_LOOKUP_DEADLINE`).
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
    # The timeout as the caller stated it. Each attempt is clamped from *this*
    # rather than from the previous attempt's clamp, so an early attempt that
    # ran close to the deadline cannot shrink the budget of a retry made after
    # a backoff that the deadline still had room for.
    stated_timeout = kwargs.get("timeout")

    def _budget_left():
        """Seconds until the deadline, or None when there is no deadline."""
        return None if deadline is None else deadline - time.monotonic()

    def _out_of_budget(left):
        raise LookupBudgetExceeded(
            f"lookup budget exhausted with {left:.1f}s to spare"
            if left is not None and left > 0
            else "lookup budget exhausted"
        ) from last_exc

    for attempt in range(1, max_attempts + 1):
        # Checked before the gate as well as after it: the gate can sleep for
        # a whole interval, and a caller walking a fallback list would pay one
        # of those per remaining instance to learn something it already knew.
        left = _budget_left()
        if left is not None and left <= 0:
            _out_of_budget(left)
        if gate is not None:
            gate.wait()
        left = _budget_left()
        if left is not None:
            if left <= 0:
                _out_of_budget(left)
            kwargs["timeout"] = _clamped_timeout(stated_timeout, left)
        try:
            # The inner `finally` marks the moment the attempt is over, however
            # it ended: `request_fn` is an arbitrary callable, so a session
            # adapter, a hook or a test transport can raise something that is
            # not a RequestException, and a handler that named only that one
            # left the gate waited-but-never-marked. The next slot would then be
            # measured from the *start* of a call that ran for ten seconds. It
            # is deliberately inside the backoff sleep below rather than around
            # it: the interval runs from the end of the call, not from the end
            # of the wait that follows it.
            try:
                response = request_fn(*args, **kwargs)
            finally:
                if gate is not None:
                    gate.mark()
        except requests.RequestException as exc:
            last_exc = exc
            allowed = max_attempts
            if silence_max_attempts is not None and _is_silence(exc):
                allowed = min(max_attempts, max(1, silence_max_attempts))
            if attempt >= allowed:
                raise
            delay = _compute_backoff(attempt, backoff_base, backoff_max)
            left = _budget_left()
            if left is not None and left <= delay:
                # Sleeping out the rest of the budget and then raising the same
                # failure buys the caller nothing but the wait. Raise the cause
                # now, so a fallback list still has the seconds to try the next
                # instance with.
                raise
            if logger:
                logger.warning(
                    "Request failed (%s). Retrying %s/%s.", exc, attempt, allowed
                )
            time.sleep(delay)
            continue

        if response.status_code in statuses and attempt < max_attempts:
            if logger:
                logger.warning(
                    "Request returned %s. Retrying %s/%s.",
                    response.status_code,
                    attempt,
                    max_attempts,
                )
            delay = _compute_backoff(attempt, backoff_base, backoff_max)
            left = _budget_left()
            if left is not None and left <= delay:
                # The budget cannot fit the wait this server asked for. Return
                # its own refusal rather than a budget error: a `504` says the
                # instance is alive and busy, which is what decides whether the
                # caller tries somewhere else (#144), and a budget error would
                # erase that.
                return response
            # A discarded response has to release its connection. Buffered bodies
            # are already read, so this is a no-op for them, but a stream=True
            # caller would otherwise leak the socket on every retry.
            response.close()
            time.sleep(delay)
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
