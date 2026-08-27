"""The one door every billed Google request goes through.

Google Maps Platform is the only thing in this repository that turns a line of
code into an invoice, and until now the rule about who may spend was written
down in two booleans that guard exactly one path. `AUTO_TRAVEL_ENRICHMENT` and
`AUTO_GEOCODING` decide what *ingestion* does; they are read in
`services/property_imap_service.py` and nowhere else. Every other way to spend
-- three HTTP endpoints, six CLI tools, the background-job executor, an
`docker exec` one-liner -- asked nobody's permission, because there was nobody
to ask.

What that cost is on record. The invoice for 1-18 August 2026 read **EUR 190**
on a project ingesting about seven listings a day, and the spikes are
attributable day by day: 320 travel runs on the 16th, 197 on the 15th, 123 on
the 10th. On 2026-08-16 four new saved searches delivered 306 listings to a
*throwaway dev database* between 07:00 and 10:00 -- roughly $110 of credit in
one morning that nobody asked for and nobody read. The fix at the time was to
default one flag to false. That was right and it was not enough: it closed the
path that had just been walked, and left the others open.

**Two beliefs died with that invoice, and this is the file where somebody will
look for them next.** Google's per-SKU free tiers -- **5,000 Places calls and
10,000 Route Matrix events a month** -- did *not* absorb this project's
volume; do not reason about cost from published allowances again, read the
bill. And the "$0.36 a listing" this repository carried was arithmetic over
the price list, never a reading from billing. It happened to be about right,
which is luck rather than method. The ledger at the bottom of this file exists
so the next such question is answered from a record instead of from a price
list.

`POST /api/lands/enrich-all` is the shape of what stayed open. It is
unauthenticated (CLAUDE.md: there is no authentication), CSRF-exempt
(`app.py`: `csrf.exempt(api_bp)`), rate-limited at 2 per 5 minutes, and it
selects every `Land` missing any enrichment block and calls `enrich_land` on
each. One request, unbounded spend, no flag in front of it.

**The rule this module owns, in one sentence: money is spent only inside an
authorization somebody opened on purpose, and the default is no.**

Three things make that a gate rather than a note.

**It lives in the transport.** Not at the eleven call sites, and not in a
helper callers are asked to remember -- this repository has written down twice
already what happens to a rule the caller has to reach for ("Pacing is passed
to the transport, never taken around it"; "Do not copy the table into a second
caller; import it"). `billed_get` is the only function in the tree that names
`maps.googleapis.com`, and `tests/test_google_spend_is_authorized.py` greps for
a twelfth one. A future call site cannot spend without meeting this check,
because there is no other way to make the request.

**The authorization is ambient and defaults to absent.** It is a context
variable, for the reason `utils/http.lookup_budget` already gives about
threading a parameter through eleven call sites: "a parameter every one of
them has to forward is a parameter one of them will not". The difference that
matters here is the default. An unset budget means *no ceiling* -- safe,
because a missing budget costs time. An unset authorization means *no* --
safe, because a missing authorization costs money. A path nobody thought about
is refused by arithmetic, not by review.

A thread the background-job executor starts begins with the variable unset, so
a job enqueued inside an authorization does **not** inherit it. That is
deliberate and it is why the routes open theirs *inside* the job closure
rather than around the enqueue: an authorization that outlived the request
that granted it would be exactly the ambient permission this module exists to
remove.

**A refusal is a `RequestException`.** Every one of the eleven call sites
already wraps its request in `try/except` and hands the exception to
`failure_from_exception`, which records an honest "nobody looked" (#98) rather
than a measurement. So refusing costs no new branch anywhere, and a refused
call can never be mistaken for "Google says there is nothing there" -- the
same trick `LookupBudgetExceeded` plays for the free lookups.

What this does **not** close, said plainly because a guard presented as
complete is worse than a guard known to be partial:

* **It is not authentication.** The API has none, by owner decision. Anyone who
  can reach the per-property Enrich endpoint can still cause the spend that
  endpoint authorizes. What changes is that the spend is *bounded* by a cap,
  *attributed* in the ledger, and *impossible* on the paths that open no
  authorization at all -- which is where the unbounded loop was.
* **It cannot see a process that never imports it.** A `curl` straight to
  Google, or a script that builds its own `requests.get`, spends without
  passing here. The boundary is this transport, not the machine.
* **The ledger is written after the attempt.** A process killed mid-request
  leaves a billed call unrecorded. Recording the intent first would inflate
  the ledger with calls that were refused before they were made, and this file
  would rather under-count a crash than over-count a normal day.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, NamedTuple, Optional

import requests

from utils.google_api import (
    REASON_SPEND_CAP_EXCEEDED,
    REASON_SPEND_NOT_AUTHORIZED,
    REASON_SPEND_OFF_ON_THIS_MACHINE,
)
from utils.http import request_with_retries

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# What the billed APIs are, and what one call of each costs in billed units.
# --------------------------------------------------------------------------

#: The Google products this application pays for. A string rather than an enum
#: because it is written into the ledger and read back by eye; keep the values
#: stable.
API_PLACES_NEARBY = "places_nearby"
API_PLACES_TEXT = "places_text"
API_DISTANCE_MATRIX = "distance_matrix"
API_GEOCODING = "geocoding"

BILLED_APIS = frozenset(
    {API_PLACES_NEARBY, API_PLACES_TEXT, API_DISTANCE_MATRIX, API_GEOCODING}
)

#: Every billed endpoint's URL, in the one place that is allowed to name them.
#: The call sites pass an `api` constant and never a URL, so a typo cannot
#: quietly point a metered call somewhere unmetered.
_URLS: Dict[str, str] = {
    API_PLACES_NEARBY: "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
    API_PLACES_TEXT: "https://maps.googleapis.com/maps/api/place/textsearch/json",
    API_DISTANCE_MATRIX: "https://maps.googleapis.com/maps/api/distancematrix/json",
    API_GEOCODING: "https://maps.googleapis.com/maps/api/geocode/json",
}


# --------------------------------------------------------------------------
# What a scope costs, so a cap is arithmetic rather than a guess
# --------------------------------------------------------------------------
#
# Measured against the code rather than chosen. One `enrich_property` on a
# machine where Google still routes (`OSRM_URL` unset) is, worst case:
#
#     geocoding            1  (2 with refresh_coords)
#     Places text search   3  (the `wide_search_query` fallbacks, and the pool
#                              cross-check -- the presets themselves are on
#                              OpenStreetMap since 2026-08-18 and the hospital
#                              on the national register, so Nearby is rare)
#     Distance Matrix     26  (6 presets + up to 20 beaches, chunked at 25)
#      + pool drive times  3
#                        ----
#                          35 billed units
#
# `request_with_retries` may issue each of those up to three times, and a
# retried request is one Google saw, so the ceiling is ~105. With `OSRM_URL`
# set -- the mini since 2026-08-20 -- the 29 routing units are free and the
# real figure is under 10.
#
# 150 is that ceiling with room, and it is a *ceiling*, not a budget: it is
# what stops a runaway loop, not what a press is expected to cost. A press
# that hits it has found a defect and the ledger says which API ran away.
#
# One consequence of reserving the worst case (`_reserve`): a cap has to cover
# the largest single call's `units * MAX_ATTEMPTS_PER_CALL`, not its nominal
# cost. The biggest call one press makes is a 26-element Distance Matrix
# batch, so it must clear 78 at its peak -- which 150 does, because the calls
# are sequential and each refunds before the next begins. Peak reservation
# measured against the sequence above is ~79.
CAP_ONE_PROPERTY = 150
#: The legacy `Land` path still buys its presets from Places (it holds no copy
#: of the OSM lookup), so it is the more expensive of the two.
CAP_ONE_LAND = 200
#: Ingestion geocodes exactly one address per new listing (`AUTO_GEOCODING`,
#: ~$0.005). One unit, and the retries the transport may add.
CAP_INGEST_GEOCODE = 5
#: Ingestion with `AUTO_TRAVEL_ENRICHMENT=true` -- off by default, and the
#: reason this whole file exists. Same ceiling as one press, because it is one.
CAP_INGEST_TRAVEL = CAP_ONE_PROPERTY


#: What `POST /api/lands/enrich-all` answers now. Here rather than in the
#: route so the refusal and the rule it enforces are read together, and so a
#: test can assert the endpoint says this without copying the sentence.
REFUSED_BULK_ENRICH_ALL = (
    "Bulk enrichment over every land is not available from the API: it spends "
    "Google credit on an unbounded number of rows with nobody's name on the "
    "request. Enrich one listing at a time from its page, or run "
    "`python -m utils.recalc_property_travel --reason '<who asked, and why>'` "
    "with an explicit scope."
)


def cap_for_rows(rows: int, per_row: int = CAP_ONE_PROPERTY) -> int:
    """The ceiling for a run over `rows` listings.

    A bulk tool states its scope before it starts, so its cap is arithmetic on
    a number it already has. A tool that cannot say how many rows it will touch
    cannot state a ceiling either, and should not be spending.
    """
    return max(1, int(rows)) * per_row


class PaidCallRefused(requests.RequestException):
    """This billed request was not made, because nobody authorized it.

    A `RequestException` on purpose, exactly as `LookupBudgetExceeded` is: the
    eleven call sites already classify one as a transport refusal and record an
    honest absence for it (#98), so a request refused here reads as "nobody
    looked" and never as "Google says there is nothing here". Refusing costs no
    new branch at any call site.

    `reason` is one of the `REASON_SPEND_*` codes in `utils/google_api.py`, so
    a refusal that reaches a JSON column is distinguishable from a network
    error by something more stable than its message.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------
# The authorization
# --------------------------------------------------------------------------


@dataclass
class SpendAuthorization:
    """One owner request, and the ceiling it carries.

    `cap_units` is the whole of what bounds an authorization: a scope that may
    spend without limit is a scope whose worst case nobody has stated, and the
    incident this module was written about was a loop with no worst case. The
    cap is in *billed units* -- one request for the Places and Geocoding APIs,
    one element per origin-destination pair for Distance Matrix -- because that
    is what the invoice counts.

    `spent` is mutated under `_LOCK` rather than by rebinding, so the four
    gunicorn threads sharing one authorization (they do not today: an
    authorization is per context) and, more importantly, any future caller that
    hands the same object to a worker cannot lose a charge to a lost update.
    """

    reason: str
    actor: str
    cap_units: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    opened_at: float = field(default_factory=time.monotonic)
    spent: int = 0

    def remaining(self) -> int:
        return max(0, self.cap_units - self.spent)


#: The ambient authorization, or None when nobody has opened one.
#:
#: None is the important half. `utils/http._LOOKUP_DEADLINE` defaults to None
#: meaning "no ceiling", which is safe there because the thing it bounds is
#: time. Here None means "no permission", because the thing it bounds is money
#: -- so the path that forgot to ask is refused rather than waved through.
_AUTHORIZATION: contextvars.ContextVar[Optional[SpendAuthorization]] = (
    contextvars.ContextVar("google_spend_authorization", default=None)
)

_LOCK = threading.Lock()


@contextmanager
def authorized_spend(
    reason: str,
    *,
    actor: str,
    cap_units: int,
) -> Iterator[SpendAuthorization]:
    """Open an authorization to spend Google credit inside this block.

    `reason` says which owner request this is -- "enrich button, property 793",
    "recalc_property_travel --ids 1,2,3". It is written into the ledger and it
    is the only thing that makes a line of the invoice traceable back to
    somebody having asked, so it is required and it is free text on purpose:
    no vocabulary invented here would survive the next kind of request.

    `actor` names the surface: a route, a CLI module, a test.

    Nested authorizations take the *smaller* remaining cap, mirroring
    `lookup_budget`: an inner block may ask for less than the run it sits in,
    never for more, so no step can raise the ceiling its caller stated.
    """
    if not reason or not str(reason).strip():
        raise ValueError("an authorization to spend money must say what asked for it")
    if isinstance(cap_units, bool) or not isinstance(cap_units, int):
        # `float("nan") <= 0` is False, so a NaN cap passed the old check and
        # then made every comparison in `_reserve` false as well -- an
        # authorization with no ceiling at all, which is the one thing a cap
        # exists to prevent. Requiring a real `int` refuses NaN, infinity and
        # a string in one line, and every caller in the tree already passes
        # one. (`bool` is excluded explicitly: it is a subclass of `int`, and
        # `cap_units=True` would otherwise authorize exactly one unit.)
        raise ValueError(
            f"a spend cap must be a whole number of units, got {cap_units!r}"
        )
    if cap_units <= 0:
        raise ValueError("an authorization to spend money must carry a positive cap")

    outer = _AUTHORIZATION.get()
    if outer is None:
        effective_cap = cap_units
    else:
        # A nested block takes its cap *out of* the parent's remaining room,
        # atomically, rather than merely being capped by a snapshot of it.
        # The snapshot version was correct while only one child could be open
        # at a time -- which is all production does, since no call site nests
        # and neither `asyncio` nor `contextvars.copy_context` appears in this
        # tree -- and wrong the moment two children share one parent, because
        # both would read the same remaining and both receive a full cap.
        # Reserving makes that impossible by construction instead of by
        # nobody having written the second caller yet.
        with _LOCK:
            effective_cap = max(0, min(cap_units, outer.cap_units - outer.spent))
            outer.spent += effective_cap

    authorization = SpendAuthorization(
        reason=str(reason).strip(), actor=actor, cap_units=effective_cap
    )
    token = _AUTHORIZATION.set(authorization)
    logger.info(
        "google spend authorized [%s] actor=%s cap=%d units reason=%s",
        authorization.id,
        actor,
        effective_cap,
        authorization.reason,
    )
    try:
        yield authorization
    finally:
        _AUTHORIZATION.reset(token)
        # The parent already paid `effective_cap` when this block opened, so
        # what comes back is the part the child did not use. Two nested blocks
        # are one owner request, and a cap a child could reset by returning
        # would bound nothing.
        if outer is not None:
            with _LOCK:
                outer.spent = max(
                    0, outer.spent - (effective_cap - authorization.spent)
                )
        logger.info(
            "google spend closed [%s] spent=%d/%d units",
            authorization.id,
            authorization.spent,
            authorization.cap_units,
        )


def current_authorization() -> Optional[SpendAuthorization]:
    """The authorization in force, or None. For surfaces that report state."""
    return _AUTHORIZATION.get()


# --------------------------------------------------------------------------
# The machine-level switch
# --------------------------------------------------------------------------


class SpendVerdict(NamedTuple):
    """Whether a billed call may be made, and why not when it may not."""

    allowed: bool
    reason: str


def machine_may_spend() -> bool:
    """Whether this machine is allowed to reach a billed Google API at all.

    Defaults to **true**, and that is a decision rather than an oversight. The
    authorization above is the gate; this is a second lock for a machine that
    must never spend whatever its code does -- the laptop, a worktree, a
    throwaway clone -- and it is set false *there*. Defaulting it false here
    would stop the deployment's own Enrich button on the deploy that shipped
    it, which is the mistake CLAUDE.md already records about
    `AUTO_START_SCHEDULER`: "that mistake was made here and nearly stopped
    production ingestion on deploy".
    """
    from config import Config

    value = getattr(Config, "GOOGLE_SPEND_ENABLED", True)
    # `bool("false")` is True, and a cost control that reads the string
    # "false" as permission is the one bug this whole module is about.
    # `config.py` always produces a real bool, but this attribute is also set
    # by tests, by a `docker exec` poking `Config`, and by whatever reads an
    # environment differently next year -- so the coercion happens here, where
    # the answer is used, rather than being assumed of every writer.
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def spend_verdict(units: int = 1) -> SpendVerdict:
    """Would `units` billed units be allowed right now? Advisory only.

    **This is not the gate, and must never be used as one.** It reads the cap
    without holding the lock, so between its answer and any charge a second
    thread may have taken the room -- which is precisely the check-then-act
    race that made `billed_get` bill over its ceiling before `_reserve`
    existed. The authoritative decision is `_reserve`, where the check and the
    charge are one indivisible step.

    What it is for is surfaces: a route refusing before it builds a service, a
    template leaving out a button the endpoint would refuse, the way
    `services/ingest_policy.machine_is_ingester` does -- a control that is
    present and refuses reads as broken. Being briefly wrong in either
    direction costs a redrawn button, not money.
    """
    if not machine_may_spend():
        return SpendVerdict(False, REASON_SPEND_OFF_ON_THIS_MACHINE)
    authorization = _AUTHORIZATION.get()
    if authorization is None:
        return SpendVerdict(False, REASON_SPEND_NOT_AUTHORIZED)
    if authorization.remaining() < max(1, units):
        return SpendVerdict(False, REASON_SPEND_CAP_EXCEEDED)
    return SpendVerdict(True, "authorized")


# --------------------------------------------------------------------------
# What a billed CLI tool must carry
# --------------------------------------------------------------------------


def add_spend_arguments(parser: Any) -> None:
    """Give a billed CLI tool the one flag that lets it spend.

    `--reason` is required at the moment the tool actually opens an
    authorization, not by argparse. That is deliberate: several of these tools
    have a `--dry-run` and a `--restore` that spend nothing, and a required
    argument would make the free half of the tool unusable without inventing a
    reason for work that costs nothing.

    Kept here rather than copied into seven `main()` functions for the reason
    this repository has already written down twice about shared tables: a rule
    in seven places is a rule that eventually ships half-changed.
    """
    parser.add_argument(
        "--reason",
        default=None,
        help=(
            "Who asked for this run and why. Required before any billed Google "
            "call: this project spends money only on the owner's explicit "
            "request, and the reason is what makes a line of the invoice "
            "traceable back to one. Written to data/google_spend.jsonl."
        ),
    )


class SpendNotRequested(RuntimeError):
    """A billed CLI tool was started without a reason, so it will not spend.

    Raised instead of refusing every individual call, so the tool stops at the
    top with one legible message rather than walking its whole scope writing
    an honest absence onto every row. A refusal per row would be *correct* and
    would still be terrible: it would rewrite hundreds of listings to say
    "nobody looked" because an operator forgot a flag.
    """


@contextmanager
def cli_authorization(
    reason: Optional[str],
    *,
    actor: str,
    rows: int,
    per_row: int = CAP_ONE_PROPERTY,
) -> Iterator[SpendAuthorization]:
    """The authorization a billed CLI tool opens around its scope.

    Refuses up front when `--reason` is absent. The cap is arithmetic on the
    scope the tool has already resolved, so a tool that has counted its rows
    has stated its own ceiling and a runaway loop stops inside it.
    """
    if not reason or not str(reason).strip():
        raise SpendNotRequested(
            f"{actor} would spend Google credit on {rows} listing(s) and no "
            "reason was given. This project spends money only on the owner's "
            "explicit request: re-run with --reason '<who asked, and why>'. "
            "If you did not mean to spend anything, you are probably looking "
            "for a free tool (backfill_sea_view, backfill_hazards, "
            "backfill_osm_amenities, backfill_quality_of_life) or --dry-run."
        )
    with authorized_spend(
        str(reason).strip(), actor=actor, cap_units=cap_for_rows(rows, per_row)
    ) as authorization:
        yield authorization


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def ledger_path() -> str:
    """Where the billed calls are written down.

    Under `data/`, which is the one bind mount, so the record survives the
    `COPY . .` rebuild that replaces everything else in the container.
    """
    from config import Config

    root = getattr(Config, "DATA_DIR", None) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    return os.path.join(root, "google_spend.jsonl")


def record_spend(entry: Dict[str, Any]) -> None:
    """Append one line to the ledger, and never fail a caller for trying.

    One `os.write` of one line opened `O_APPEND`: POSIX makes an append of less
    than `PIPE_BUF` atomic, so the gunicorn threads and any `docker compose
    run` sibling interleave whole lines rather than corrupting each other's.
    A line longer than that cannot occur here -- every field is a short scalar
    and `reason` is truncated.

    A ledger that cannot be written must not lose a measurement the owner has
    already paid for, so this degrades to a log line. It is loud about it: an
    unwritable ledger means the next invoice is unattributable, which is the
    state this whole module exists to leave behind.
    """
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        path = ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        logger.warning(
            "google spend ledger unwritable; the call is only in this log: %s",
            line.strip(),
            exc_info=True,
        )


#: How much of a caller-supplied `subject` reaches the ledger.
#:
#: `subject` says which listing or coordinate a call was about, so an operator
#: reading the file can tell one row's spend from another's. It is
#: caller-supplied text landing in a file, exactly like `reason`, and the
#: review that prompted this bound observed that today's geocoding subject is
#: the address string itself. Bounded for the same reason `reason` is, and
#: documented so the next caller knows what this field is for: an identifier,
#: never a payload, and never anything secret -- the API key travels in
#: `params`, which is never written here.
MAX_LEDGER_SUBJECT = 120


def _ledger_subject(subject: Optional[str]) -> Optional[str]:
    """One short identifying string, or nothing."""
    if subject is None:
        return None
    return " ".join(str(subject).split())[:MAX_LEDGER_SUBJECT]


#: `reason` is free text typed by whoever opened the authorization. It rides
#: into a file read back by eye or with `jq` -- there is deliberately no
#: reporting tool here, because one would be a second place that decides what
#: the ledger means. Bound it so a pathological caller cannot write a megabyte
#: a row.
MAX_LEDGER_REASON = 200


# --------------------------------------------------------------------------
# The transport
# --------------------------------------------------------------------------


#: What `request_with_retries` may issue for one logical call. `utils/http.py`
#: defaults `max_attempts=3` and every billed caller here takes that default,
#: so three is the worst case one `billed_get` can put on the invoice.
#:
#: It is a *constant here* rather than a value read from the transport because
#: the reservation below has to be made before the transport is entered. If
#: `utils/http.request_with_retries` ever changes its default, this number is
#: what has to change with it, and `tests/test_google_spend_is_authorized.py`
#: asserts the two agree.
MAX_ATTEMPTS_PER_CALL = 3


def _reserve(authorization: SpendAuthorization, worst_case: int) -> bool:
    """Check the cap and charge the worst case in one atomic step.

    The whole gate is this function being indivisible. An earlier version
    asked `spend_verdict()` whether there was room and *then* charged under a
    separate lock, which is a check-then-act race: with `--workers 1
    --threads 4` two threads both read `remaining() == 1`, both passed, and
    both billed. One unit over a cap of one is not much money; a gate whose
    ceiling can be walked through is not a ceiling, which is the whole claim
    this module makes.

    The worst case is reserved rather than the nominal cost, because
    `request_with_retries` may issue the same request three times and each
    attempt is one Google may bill for. Charging the nominal figure up front
    and the retries afterwards let a cap of one fund three requests -- the cap
    bounded the *intention* and not the spend. What is not used is refunded by
    the caller the moment the attempt count is known, so reserving costs
    headroom during the call and nothing after it.
    """
    with _LOCK:
        if authorization.cap_units - authorization.spent < worst_case:
            return False
        authorization.spent += worst_case
        return True


def _refund(authorization: SpendAuthorization, amount: int) -> None:
    """Give back the reserved attempts that were never issued."""
    if amount <= 0:
        return
    with _LOCK:
        authorization.spent = max(0, authorization.spent - amount)


def billed_get(
    api: str,
    *,
    params: Dict[str, Any],
    units: int,
    subject: Optional[str] = None,
    timeout: Any = 12,
    call_logger: Optional[logging.Logger] = None,
) -> requests.Response:
    """Issue one billed Google request, or refuse before anything leaves.

    The only function in this repository that names a `maps.googleapis.com`
    URL. Every billed call in `services/` and `utils/` goes through it, and
    `tests/test_google_spend_is_authorized.py` fails the suite if a twelfth
    call site appears anywhere else.

    `units` is what one *attempt* costs on the invoice: 1 for Places and
    Geocoding, `origins x destinations` for Distance Matrix. What is charged
    against the cap before the request is `units * MAX_ATTEMPTS_PER_CALL` --
    the worst case, reserved -- and the attempts that never happened are
    refunded the moment the count is known.

    That is a correction, and the thing it corrects is worth stating because
    it is easy to reintroduce. Charging the nominal figure up front and the
    retries afterwards reads as careful accounting and bounds nothing: a cap
    of one unit funded a request `request_with_retries` issued three times,
    because the two extra attempts were charged after they had already been
    sent. A cap that is only checked against the cost the caller *intended*
    is a cap on intentions. Reserving costs headroom during the call and
    nothing after it.

    The ledger still records the attempts actually issued rather than the
    nominal figure -- a caller that budgeted one unit and met two 429s has
    spent what it spent, and hiding that is how a ledger comes to disagree
    with an invoice.

    Raises `PaidCallRefused` when the machine may not spend, no authorization
    is open, or the cap will not cover this call. Everything else -- a network
    error, an HTTP error, a Google `REQUEST_DENIED` -- comes back the way it
    always did, for `read_api_payload` to classify.
    """
    if api not in BILLED_APIS:
        raise ValueError(f"unknown billed API: {api!r}")
    if not isinstance(units, int) or units < 1:
        # A call that costs nothing is a call that was mis-counted. `units=0`
        # used to pass the check -- `spend_verdict` compared `max(1, units)`
        # while the charge added the raw figure -- so a caller whose
        # arithmetic produced 0 billed Google and charged the authorization
        # nothing, which is the one outcome this module exists to make
        # impossible. Refused loudly rather than clamped, because a wrong
        # `units` is a defect at the call site and clamping hides it.
        raise ValueError(f"a billed call must cost at least one unit, got {units!r}")

    log = call_logger or logger

    # The refusal path, in the order that fails closed: the machine switch, the
    # presence of an authorization, then the cap -- and the cap check *is* the
    # charge (`_reserve`), so nothing can pass between asking and paying.
    authorization = _AUTHORIZATION.get()
    worst_case = units * MAX_ATTEMPTS_PER_CALL
    if not machine_may_spend():
        refusal = REASON_SPEND_OFF_ON_THIS_MACHINE
    elif authorization is None:
        refusal = REASON_SPEND_NOT_AUTHORIZED
    elif not _reserve(authorization, worst_case):
        refusal = REASON_SPEND_CAP_EXCEEDED
    else:
        refusal = None

    if refusal is not None:
        detail = (
            f"cap {authorization.cap_units} units, {authorization.spent} already "
            f"spent, {worst_case} needed for this call and its retries"
            if authorization is not None
            else "no authorization is open"
        )
        message = (
            f"refused to spend {units} billed unit(s) on {api}: {refusal} ({detail})"
        )
        log.warning("google spend refused: %s", message)
        record_spend(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "api": api,
                "units": 0,
                "requested_units": units,
                "outcome": "refused",
                "refusal": refusal,
                "subject": _ledger_subject(subject),
                "authorization": authorization.id if authorization else None,
                "actor": authorization.actor if authorization else None,
                "reason": (authorization.reason if authorization else "")[
                    :MAX_LEDGER_REASON
                ],
            }
        )
        raise PaidCallRefused(refusal, message)

    # `worst_case` is already reserved by `_reserve` above. What is left is to
    # find out how many attempts were really issued and give the rest back.
    attempts = 0

    def _counted_get(*args: Any, **kwargs: Any) -> requests.Response:
        nonlocal attempts
        attempts += 1
        return requests.get(*args, **kwargs)

    outcome = "answered"
    try:
        response = request_with_retries(
            _counted_get, _URLS[api], params=params, timeout=timeout, logger=log
        )
        return response
    except Exception:
        outcome = "error"
        raise
    finally:
        # A retried request is a request Google saw, so the ledger records the
        # attempts really issued. The authorization was charged for three of
        # them before the call; the ones that did not happen come back.
        issued = max(1, attempts)
        settlement = worst_case - issued * units
        if settlement >= 0:
            _refund(authorization, settlement)
        else:
            # More attempts than the reservation covered. Unreachable while
            # `MAX_ATTEMPTS_PER_CALL` matches the transport's default (a test
            # asserts it does), but silently ignoring a negative refund would
            # mean under-charging in exactly the case where Google was sent
            # more requests than budgeted -- the failure this whole function
            # exists to prevent, arriving through its own arithmetic.
            with _LOCK:
                authorization.spent += -settlement
        record_spend(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "api": api,
                "units": units * issued,
                "requests_issued": issued,
                "outcome": outcome,
                "subject": _ledger_subject(subject),
                "authorization": authorization.id,
                "actor": authorization.actor,
                "reason": authorization.reason[:MAX_LEDGER_REASON],
            }
        )
