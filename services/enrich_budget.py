"""What one Enrich press may take, derived once and read by both ends.

`static/js/main.js` used to carry the client's own guess at it -- 300 000 ms,
with the comment *"Several Google calls plus Overpass behind the 5 s gate"*.
That number predated the Overpass fallback list added on 2026-08-19, which
raised the server's own ceiling to about fifteen minutes and re-opened #178:
a job that is still running is announced to the owner as a failure, and the
obvious next move -- press it again -- pays for a second run of work already
in flight.

The rule this module owns is that **the ceiling is derived from what the
server allows, and the server is the one that says it.** The enrich endpoint
returns `poll_timeout_ms` with its `202`, so the page polls for as long as the
work can legitimately take and no longer. A future fallback instance, a longer
AI timeout or a wider budget moves the client's number without anybody
remembering to.

The three terms, each read from `config.py` at call time rather than frozen at
import, so a test can move one and see the total move:

* `ENRICH_LOOKUP_BUDGET_S` -- the free lookups, Overpass and elevation. This
  one is a real deadline: `utils/http.lookup_budget` enforces it.
* `subscription_transport.DEFAULT_TIMEOUT_SECONDS` +
  `AI_BRIDGE_SOCKET_MARGIN_SECONDS` -- the one subscription call an Enrich
  press can make, the sea-view text signal (`enrich_free_sources(use_ai=True)`).
  A single call, not one per step. **Read from the transport's own default and
  not from `AI_ANALYSIS_TIMEOUT_SECONDS`**: `classify_text_with_ai` passes no
  `timeout=` at all, so it gets 300 s and not the analysis endpoint's 180 --
  found in review, and understating a term by 120 s is precisely the shape
  this module exists to stop.
* `ENRICH_PAID_ALLOWANCE_S` -- geocoding, the Places wide search and one
  Distance Matrix request. An allowance and not a deadline, deliberately:
  abandoning a billed request mid-flight is how a press comes to pay for a
  measurement nobody receives. It also carries the one free HTTP fetch that
  is neither Google nor an OSM lookup: `services/advertiser.py` reads a
  fotocasa listing page at `FETCH_TIMEOUT_S = 20` behind a 3 s gate, up to
  three attempts, so about a minute in the worst case. Named here because a
  term nobody wrote down is how this sum comes to be shorter than the run.

Plus `QUEUE_ALLOWANCE_S` for the time a queued job waits before one of the
four `BACKGROUND_WORKERS` picks it up -- the same allowance the AI analysis
poll timeout already carries.
"""

from config import Config

# The wait before a worker takes the job. Not derived from anything: the queue
# has no ceiling of its own, and four workers shared by every job type can be
# busy. Sixty seconds is what `JOB_POLL_TIMEOUTS.aiAnalysis` already assumed,
# kept here so both budgets make the same assumption in one place.
QUEUE_ALLOWANCE_S = 60.0


def lookup_budget_seconds() -> float:
    """The wall-clock budget for one run's free lookups."""
    return float(getattr(Config, "ENRICH_LOOKUP_BUDGET_S", 240.0))


def ai_allowance_seconds() -> float:
    """The one subscription call an Enrich press can make.

    The transport's default, because the caller takes it: `sea_view_service.
    classify_text_with_ai` calls `subscription_transport.complete()` with no
    `timeout=`.
    """
    from services.subscription_transport import DEFAULT_TIMEOUT_SECONDS

    return float(DEFAULT_TIMEOUT_SECONDS) + float(
        getattr(Config, "AI_BRIDGE_SOCKET_MARGIN_SECONDS", 25)
    )


def worst_case_seconds() -> float:
    """The allowance one enrichment run is given, in seconds.

    **Not a proof of a maximum, and the difference matters.** Three of the
    waits a run contains have no ceiling anywhere in this system: the queue is
    unbounded (`BACKGROUND_WORKERS` is 4 and `_EXECUTOR`'s queue is not
    capped), a `FOR UPDATE` refresh can wait on PostgreSQL with no statement
    timeout configured, and `requests` measures its read timeout *between*
    reads, so a body that keeps arriving keeps its socket. Adding a bigger
    constant does not change that -- which is why the client's use of this
    number is a budget for **silence** rather than for duration
    (`static/js/main.js::pollJob`): every poll that finds the job alive resets
    it, so this only has to cover the gap between two pieces of evidence, and
    a queue deeper than expected extends the wait instead of ending it.

    What it does have to be is *generous*, because the harmful direction is
    short: a client that gives up while the server is working sends the owner
    to press again (#178).
    """
    paid = float(getattr(Config, "ENRICH_PAID_ALLOWANCE_S", 120.0))
    return QUEUE_ALLOWANCE_S + lookup_budget_seconds() + ai_allowance_seconds() + paid


def poll_timeout_ms() -> int:
    """`worst_case_seconds()` as the milliseconds a client should poll for."""
    return int(round(worst_case_seconds() * 1000))
