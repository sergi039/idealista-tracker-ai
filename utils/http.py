import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Optional
from urllib.parse import urlsplit

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

# --- refusing to keep dialling a host that has said no -----------------------
#
# Moved here from `services/listing_status_service.py` (#399), unchanged. It
# was written for idealista's DataDome wall and it is not about idealista: it
# needs no model, no session and no table, and the second caller that wanted it
# is Overpass, whose failure mode on 2026-08-17 was the same shape -- a host
# that answers nothing, asked again on every press. Keeping one copy beside
# `RateGate` is the alternative to a second implementation that drifts; the
# service imports it back, so its own tests and call sites are untouched.


class RefusalBreaker:
    """Stop dialling a host that has already said no, and say so.

    idealista answers this machine with DataDome bot protection -- measured
    2026-08-15 over 76 consecutive properties, every one of them a captcha, not
    one listing page reached. The service was right to record nothing, but it
    kept spending a request per press to learn the same thing, and the reader
    got a generic failure each time.

    So refusals are counted across calls. After `threshold` in a row the
    breaker opens and later checks return immediately, spending nothing, and
    reporting the refusal as the standing condition it is. When the cooldown
    expires exactly one request goes out -- the breaker does not heal on a
    timer, it heals on evidence -- and a refusal re-arms it.

    Deliberately process-local and in-memory. It paces outbound traffic, so it
    must be cheap and must not need a table; each gunicorn worker keeping its
    own count means at worst `workers x threshold` requests before everything
    is quiet, which is a handful, not a sweep. It is a class attribute rather
    than an instance one because every caller builds a fresh service.
    """

    def __init__(self, threshold: int, cooldown_s: int):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._consecutive = 0
        self._blocked_until: Optional[datetime] = None
        self._last_reason: Optional[str] = None
        self._last_refusal_at: Optional[datetime] = None

    def should_skip(self, now: Optional[datetime] = None) -> bool:
        """May this caller dial? Answering False **claims** the probe.

        Deliberately not a pure query, and named in `observe` as the gate it is.
        A read-only version had a race an independent review found: with the
        cooldown expiring at 12:30:00, two request threads calling at 12:30:01
        both saw "not blocked" before either recorded a result, and both dialled
        a host that is refusing us. Exactly the failure this class exists to
        prevent, one level up -- and the docstring above promised "exactly one
        request goes out", which the code did not deliver.

        So the expiry is consumed inside the lock: the first caller through
        re-arms the window and dials, and everyone behind it keeps skipping
        until that probe reports. `record_success` clears the window if the
        probe reached the listing; `record_refusal` re-arms it if it did not,
        which is what it would have done anyway.
        """
        now = now or datetime.now(timezone.utc)
        with self._lock:
            if self._blocked_until is None:
                return False
            if now < self._blocked_until:
                return True
            # The cooldown has expired and this caller is the one probe.
            self._blocked_until = now + timedelta(seconds=self.cooldown_s)
            return False

    def record_refusal(self, reason: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._consecutive += 1
            self._last_reason = reason
            self._last_refusal_at = now
            if self._consecutive >= self.threshold:
                self._blocked_until = now + timedelta(seconds=self.cooldown_s)

    def record_success(self, now: Optional[datetime] = None) -> None:
        """A fetch reached the listing page: the host is answering us again."""
        with self._lock:
            self._consecutive = 0
            self._blocked_until = None
            self._last_reason = None

    def state(self) -> Dict:
        with self._lock:
            return {
                "open": self._blocked_until is not None,
                "consecutive_refusals": self._consecutive,
                "last_reason": self._last_reason,
                "last_refusal_at": self._last_refusal_at.isoformat()
                if self._last_refusal_at
                else None,
                "blocked_until": self._blocked_until.isoformat()
                if self._blocked_until
                else None,
            }

    def reset(self) -> None:
        """For tests and for a deliberate retry after the owner changes the route."""
        with self._lock:
            self._consecutive = 0
            self._blocked_until = None
            self._last_reason = None
            self._last_refusal_at = None


class HostBreakers:
    """One `RefusalBreaker` per host, because a refusal is about one host.

    There used to be a single process-wide breaker, which was right while
    every listing was on idealista.com and became wrong the moment a second
    site arrived. idealista refuses this machine *permanently* -- measured
    2026-08-15 over 76 consecutive properties, every one a DataDome block --
    so its breaker is open essentially always. A shared breaker therefore does
    not degrade fotocasa checks, it forbids them: three idealista refusals,
    which arrive the moment anybody presses anything, and the next fotocasa
    check returns `backing_off` for half an hour without a request going out.
    One host's wall would have become every host's.

    Keyed on the hostname rather than the full URL: the refusal is the site
    saying no, and per-URL counting would need `threshold` refusals from each
    listing before it stopped, which is the sweep the breaker exists to stop.
    An unparseable URL keys on the empty string -- one bucket for the
    malformed, which cannot be reached by a real fetch anyway.
    """

    def __init__(self, threshold: int, cooldown_s: int):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._by_host: Dict[str, RefusalBreaker] = {}

    @staticmethod
    def host_of(url: Optional[str]) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        if "//" not in raw:
            raw = "https://" + raw
        try:
            return (urlsplit(raw).hostname or "").lower()
        except ValueError:
            return ""

    def for_url(self, url: Optional[str]) -> RefusalBreaker:
        host = self.host_of(url)
        with self._lock:
            breaker = self._by_host.get(host)
            if breaker is None:
                breaker = RefusalBreaker(
                    threshold=self.threshold, cooldown_s=self.cooldown_s
                )
                self._by_host[host] = breaker
            return breaker

    def state(self) -> Dict:
        """Every host that has been dialled, and what it is saying.

        A report over hosts rather than one aggregate: "the breaker is open"
        was a true sentence about idealista and a false one about everything
        else, and a reader cannot tell those apart from a single flag.
        """
        with self._lock:
            hosts = dict(self._by_host)
        return {host or "(no host)": breaker.state() for host, breaker in hosts.items()}

    def reset(self) -> None:
        """Forget every host. `tests/conftest.py` calls this between tests."""
        with self._lock:
            breakers = list(self._by_host.values())
            self._by_host.clear()
        for breaker in breakers:
            breaker.reset()


# Overpass, for both transports that dial it: the shared amenity/places client
# in `services/enrichment_service.py` and the coastline client in
# `services/sea_view_service.py`. One registry, keyed by host, because the two
# dial the same instances and each re-discovering an outage costs a full
# cascade -- measured 2026-08-20 at 780 s per call site against three dead
# instances, eleven sites in one Enrich press.
#
# Three refusals in a row, then five minutes of answering from what is already
# known. The cooldown buys exactly one probe back and a refusal re-arms it:
# this heals on evidence, not on a timer. Five minutes rather than the
# listing-status thirty, because an Overpass outage is usually load and clears
# on its own, and every skipped call is a *free* measurement not taken -- the
# cost of being wrong here is a row that says `unavailable` and comes back into
# scope on the next press, not a permanent hole.
OVERPASS_BREAKERS = HostBreakers(threshold=3, cooldown_s=300)


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
    # Unreachable: the loop either returns, raises, or continues. Kept as a
    # belt-and-braces final attempt, and it takes the gate like any other.
    if gate is not None:
        gate.wait()
    try:
        return request_fn(*args, **kwargs)
    finally:
        if gate is not None:
            gate.mark()
