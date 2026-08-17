"""Who is selling a listing: its owner, or an agency.

The owner asked for it the way anyone would -- "mark the ones from private
owners so I can see it from the list". What made it worth a module rather than
an `ILIKE` in a template is that the answer arrives from three different places
with three different strengths, and that for a large part of the table it does
not arrive at all. A surface that cannot tell those apart ends up doing what
`services/listing_verification.py` was written to stop: rendering an absence of
knowledge as a fact.

**Where the answer comes from, measured against production on 2026-08-17.**

* **The Idealista alert link already carries it, for free.** Every listing that
  arrived by email keeps its full query string (nothing strips it -- see
  `utils/idealista_extractors.extract_url`), and the campaign that delivered it
  is Idealista's own word for the kind of advert:
  `utm_campaign=express_newAd_sale_particular` against
  `..._sale_professional`. Counted over all 730 rows: 246 + 48 professional,
  113 + 1 particular, 322 rows with no campaign at all. So 408 rows answer this
  question with no request, no key and no cost, out of a string the row is
  already holding. That is why the reading is derived rather than stored: a
  derived value cannot drift out of agreement with the URL it came from, and a
  stored one would have to be written by every future ingest path -- the
  argument `utils/listing_source.py` and `utils/municipality_grouping.py`
  already record for the same decision.
* **The fotocasa page says it outright**, in `publisher.type` and again in
  `agency.type`. Reading it costs one fetch of a page the import already
  fetches, so a link import records it on the way past and nothing extra is
  spent.
* **For the rest, nobody can look from this machine.** The 322 rows with no
  campaign are the hand-imported batches, and 169 of them (subscriptions 14 and
  16) are idealista.com URLs. Idealista answers `403` with a DataDome captcha
  to every request from here -- re-measured 2026-08-17 against
  `https://www.idealista.com/inmueble/103986992/`, still `403 geo.captcha` --
  and defeating that is not on the table (`services/listing_status_service.py`
  documents the same wall and the same decision). Those rows are `unchecked`,
  and `unchecked` is what the page says about them.

**Four presentation states, and the distinction between the last two is the
whole point.**

* `owner`     -- a private seller. This is the one the list badges.
* `agency`    -- a professional advertiser.
* `unknown`   -- somebody looked and the source did not say. A fotocasa page
                 whose `publisher.type` is a spelling nobody here has measured
                 lands on this, never on `owner`.
* `unchecked` -- nobody looked. Not a database value: it is what the absence of
                 every source above reads as, exactly as `unchecked` is
                 `services/listing_verification.py`'s presentation state for a
                 status nothing established.

Both readings of that rule live here -- `read_verdict` for one row and
`state_expression` for a query -- because the badge, the filter and its counts
are on screen together, and a filter that selects rows the badge does not mark
is worse than no filter at all. `tests/test_advertiser.py` runs one matrix
through both.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, literal
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

OWNER = "owner"
AGENCY = "agency"
UNKNOWN = "unknown"
UNCHECKED = "unchecked"

# The states a source can establish. `unchecked` is not among them: nothing
# writes it, it is what nothing-at-all reads as.
ESTABLISHED_STATES: Tuple[str, ...] = (OWNER, AGENCY, UNKNOWN)

# The states worth keeping when a later attempt refuses. `unknown` is not one:
# it says the source was read and had nothing to say, so a fresh reading that
# also fails leaves the row no worse. `owner` and `agency` are measurements and
# a refusal must never overwrite one -- the rule `services/pool_service.py`
# states for the same reason.
MEASURED_STATES: Tuple[str, ...] = (OWNER, AGENCY)

# The order the filter offers them in, and the only values it accepts.
STATES: Tuple[str, ...] = (OWNER, AGENCY, UNKNOWN, UNCHECKED)

# Where a stored verdict came from.
SOURCE_ALERT = "alert_campaign"
SOURCE_PORTAL = "portal_payload"
SOURCE_MANUAL = "manual"

# The block inside `Property.enrichment` this module owns. A JSON sub-key, so
# no migration -- the same shape `sea`, `pool` and `import` already use.
ENRICHMENT_KEY = "advertiser"

# What Idealista's alert campaigns spell the two kinds of advert. The token is
# matched as a literal substring on both sides of this module (Python and SQL)
# rather than by parsing the query string on one side and pattern-matching on
# the other, because the two readings have to agree row for row and the
# cheapest way to guarantee that is for them to be the same reading.
_ALERT_TOKENS: Dict[str, str] = {
    "_sale_particular": OWNER,
    "_rent_particular": OWNER,
    "_sale_professional": AGENCY,
    "_rent_professional": AGENCY,
}

# Backslash, because every token above contains `_`, which LIKE otherwise
# treats as "any character" -- the lesson `utils/listing_search.py` records.
_LIKE_ESCAPE = "\\"

# `publisher.type` / `agency.type` on a fotocasa listing page. Only
# `professional` has been observed: all 5 pages fotocasa served during the
# 2026-08-17 probe carry it, alongside `clientTypeId: 3`. The private spellings
# are accepted on their face rather than measured, and that asymmetry is
# deliberate -- an unrecognised value can only ever fall to `unknown`, never to
# `owner`, so the failure mode of a wrong guess here is a row that says "not
# established" instead of a row that says something false.
_PORTAL_TYPES: Dict[str, str] = {
    "professional": AGENCY,
    "private": OWNER,
    "particular": OWNER,
}

_LABELS: Dict[str, str] = {
    OWNER: "Private owner",
    AGENCY: "Agency",
    UNKNOWN: "Source did not say",
    UNCHECKED: "Not established",
}


def label(state: Optional[str]) -> str:
    """What to print. Never a bare slug."""
    return _LABELS.get((state or "").strip().lower(), _LABELS[UNCHECKED])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def from_alert_url(url: Optional[str]) -> Optional[str]:
    """`owner` / `agency` from an Idealista alert link, or None.

    None means the link carries no campaign token -- a hand-imported row, or a
    link somebody cleaned. It never means "agency": an absent token is an
    absent measurement.
    """
    lowered = (url or "").lower()
    for token, state in _ALERT_TOKENS.items():
        if token in lowered:
            return state
    return None


def from_portal_type(
    portal_type: Optional[str], client_type_id: Optional[int] = None
) -> str:
    """`owner` / `agency` / `unknown` from a portal's own publisher type.

    `client_type_id` is carried for the record and deliberately not used to
    decide: one numeric value has been seen (`3`, alongside `professional`) and
    inventing the meaning of the others would be a guess wearing a measurement's
    clothes. It is stored as evidence so the day a private advert turns up, what
    it carries is already written down.
    """
    key = (portal_type or "").strip().lower()
    state = _PORTAL_TYPES.get(key)
    if state is None:
        logger.info(
            "advertiser: unrecognised portal publisher type %r (clientTypeId=%r); "
            "recording as unknown",
            portal_type,
            client_type_id,
        )
        return UNKNOWN
    return state


def portal_verdict(
    portal_type: Optional[str],
    client_type_id: Optional[int] = None,
    client_name: Optional[str] = None,
    site: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The block to store in `enrichment["advertiser"]` after reading a page."""
    return {
        "state": from_portal_type(portal_type, client_type_id),
        "source": SOURCE_PORTAL,
        "site": site,
        "checked_at": (now or _utcnow()).isoformat(),
        "evidence": {
            "publisher_type": portal_type,
            "client_type_id": client_type_id,
            "client_name": client_name,
        },
    }


def stored_block(record: Any) -> Optional[Dict[str, Any]]:
    """The verdict a reading left on the row, or None."""
    enrichment = getattr(record, "enrichment", None)
    if not isinstance(enrichment, dict):
        return None
    block = enrichment.get(ENRICHMENT_KEY)
    return block if isinstance(block, dict) else None


def is_hand_set(record: Any) -> bool:
    """Did the owner set this by hand?

    A hand-set verdict outranks every computed one and is never overwritten --
    the rule `services/sea_view_service.py` states as "an owner who looked at
    the listing outranks both models".
    """
    block = stored_block(record)
    return bool(block and block.get("source") == SOURCE_MANUAL)


def read_verdict(record: Any) -> Dict[str, Any]:
    """The presentation verdict for one `Property` row.

    A plain dict, so Jinja reads `verdict.state` the way it reads the sea-view
    and listing-status verdicts this deliberately mirrors:

    * `state`      -- 'owner', 'agency', 'unknown' or 'unchecked'
    * `established`-- did anybody establish it, or is this the default silence?
    * `source`     -- 'alert_campaign', 'portal_payload', 'manual' or None
    * `checked_at` -- when a page was read for it (ISO string), or None. The
                      alert reading never stamps one: it is derived from a
                      string the row was born with, not from a reading taken at
                      a moment.
    * `evidence`   -- what the source actually said, for the detail page
    * `note`       -- one sentence a tooltip can carry verbatim

    Precedence is strength, not recency. A hand-set verdict outranks
    everything; a page that was read outranks a campaign token; both outrank
    silence; and a stored `unknown` -- a page that was read and said nothing
    recognisable -- is taken only after the campaign token has been given its
    chance, because "the source did not say" must not bury an answer the row is
    already holding.
    """
    block = stored_block(record)
    stored_state = (block or {}).get("state")
    stored_state = stored_state if stored_state in ESTABLISHED_STATES else None

    if stored_state and is_hand_set(record):
        return {
            "state": stored_state,
            "established": True,
            "source": SOURCE_MANUAL,
            "checked_at": (block or {}).get("checked_at"),
            "evidence": (block or {}).get("evidence") or {},
            "note": _note(stored_state, SOURCE_MANUAL),
        }

    if stored_state in MEASURED_STATES:
        source = (block or {}).get("source") or SOURCE_PORTAL
        return {
            "state": stored_state,
            "established": True,
            "source": source,
            "checked_at": (block or {}).get("checked_at"),
            "evidence": (block or {}).get("evidence") or {},
            "note": _note(stored_state, source),
        }

    from_alert = from_alert_url(getattr(record, "url", None))
    if from_alert:
        return {
            "state": from_alert,
            "established": True,
            "source": SOURCE_ALERT,
            "checked_at": None,
            "evidence": {},
            "note": _note(from_alert, SOURCE_ALERT),
        }

    if stored_state == UNKNOWN:
        return {
            "state": UNKNOWN,
            "established": True,
            "source": (block or {}).get("source") or SOURCE_PORTAL,
            "checked_at": (block or {}).get("checked_at"),
            "evidence": (block or {}).get("evidence") or {},
            "note": (
                "The listing page was read and did not name the kind of advertiser."
            ),
        }

    return {
        "state": UNCHECKED,
        "established": False,
        "source": None,
        "checked_at": None,
        "evidence": {},
        "note": _unchecked_note(record),
    }


def _note(state: str, source: str) -> str:
    if source == SOURCE_MANUAL:
        return "Set by hand."
    if source == SOURCE_ALERT:
        kind = "a private owner" if state == OWNER else "a professional"
        return (
            f"Idealista's alert email delivered this as {kind}'s advert "
            "(its own campaign says so)."
        )
    kind = "a private owner" if state == OWNER else "a professional"
    return f"The listing page names {kind} as the publisher."


def _unchecked_note(record: Any) -> str:
    """Why nothing is known -- naming the site, since that decides the why."""
    from utils.listing_source import IDEALISTA, UNKNOWN as SOURCE_SITE_UNKNOWN
    from utils.listing_source import source_label, source_of

    site = source_of(record) if record is not None else SOURCE_SITE_UNKNOWN
    if site == IDEALISTA:
        return (
            "Not established — this listing did not arrive by alert email, and "
            "Idealista refuses this machine, so its page cannot be read here."
        )
    if site == SOURCE_SITE_UNKNOWN:
        return "Not established — there is no link to read."
    return f"Not established — nobody has read this listing on {source_label(site)}."


def state_expression(model: Any):
    """`read_verdict(...)["state"]` as a SQL expression over `model`.

    It has to agree with `read_verdict` row for row: the dropdown prints a
    count next to every option, and a count that disagrees with the badges
    below it is a third wrong number rather than a disclosure. The branches are
    in the same order as the function's, and the tests run one matrix through
    both.
    """
    stored = model.enrichment[ENRICHMENT_KEY]["state"].as_string()
    stored_source = model.enrichment[ENRICHMENT_KEY]["source"].as_string()
    url = func.lower(func.coalesce(model.url, ""))

    branches: List[Any] = [
        (
            and_(stored_source == SOURCE_MANUAL, stored.in_(ESTABLISHED_STATES)),
            stored,
        ),
        (stored.in_(MEASURED_STATES), stored),
    ]
    for token, state in _ALERT_TOKENS.items():
        pattern = "%" + token.replace("_", _LIKE_ESCAPE + "_") + "%"
        branches.append((url.like(pattern, escape=_LIKE_ESCAPE), literal(state)))
    branches.append((stored == UNKNOWN, literal(UNKNOWN)))

    return case(*branches, else_=literal(UNCHECKED))


def filter_clause(model: Any, state: Optional[str]):
    """The seller filter, as one SQLAlchemy clause (None when unset).

    Shared by the listing surfaces the way they already share
    `source_filter_clause` and `municipality_filter_clause`, so they cannot
    drift into different answers to "show me the ones from owners".
    """
    wanted = (state or "").strip().lower()
    if wanted not in STATES:
        return None
    return state_expression(model) == wanted


REFUSAL_ALREADY_KNOWN = "already_known"
REFUSAL_HAND_SET = "hand_set"
REFUSAL_NO_URL = "no_url"
REFUSAL_SITE_NOT_READABLE = "site_not_readable"
REFUSAL_BACKING_OFF = "backing_off"


def enrich(prop: Any, *, commit: bool = False) -> Dict[str, Any]:
    """Establish who is selling this listing, and record it. Free.

    Returns `{"state": ..., "stored": bool, "refusal": ... or None}` -- the
    caller logs it and never fails a run on it, the way every other advisory
    enricher here behaves.

    Four things it deliberately does not do.

    **It does not re-establish what the row already answers.** A listing that
    arrived by alert email carries the campaign token, so the verdict is
    already derivable and fetching its page would spend somebody's bandwidth to
    learn what is written on the row. Same for a verdict a page reading already
    left, and for one the owner set by hand -- that one is never overwritten at
    all, the rule `services/sea_view_service.py` states as "an owner who looked
    at the listing outranks both models".

    **It reads fotocasa and no other site.** Not an oversight and not a
    to-do: measured 2026-08-15 and again on 2026-08-17, idealista.com answers
    `403` with a DataDome captcha to every request from this machine, browser
    headers included, so an idealista reader could never be run and therefore
    could never be tested. Writing one would ship an untested branch whose only
    observable behaviour is a refusal that this function already reports
    without it.

    **It does not hammer a host that said no.** Fotocasa answers `200` with its
    "SENTIMOS LA INTERRUPCIÓN" page once a client asks too often -- measured
    2026-08-17, after 5 requests spaced 3 s apart, and it kept answering that
    for the next several minutes. `RefusalBreaker` already counts refusals per
    host for the status checker; this uses the same one, so a fotocasa that is
    refusing the status button is not asked by this one either.

    **A refusal writes nothing.** Not even an empty block: the absence of a
    verdict is what `read_verdict` presents as `unchecked`, and a stored
    `unavailable` would be a second spelling of the same nothing.
    """
    from services.fotocasa_source import fetch_listing, is_fotocasa_url
    from services.listing_status_service import ListingStatusService
    from services.enrichment_write import check_writable, locked_write

    url = getattr(prop, "url", None)
    verdict = read_verdict(prop)

    if is_hand_set(prop):
        return {"state": verdict["state"], "stored": False, "refusal": REFUSAL_HAND_SET}
    if verdict["state"] in MEASURED_STATES:
        return {
            "state": verdict["state"],
            "stored": False,
            "refusal": REFUSAL_ALREADY_KNOWN,
        }
    if not url:
        return {"state": UNCHECKED, "stored": False, "refusal": REFUSAL_NO_URL}
    if not is_fotocasa_url(url):
        return {
            "state": UNCHECKED,
            "stored": False,
            "refusal": REFUSAL_SITE_NOT_READABLE,
        }

    breaker = ListingStatusService.breakers.for_url(url)
    if breaker.should_skip():
        return {"state": UNCHECKED, "stored": False, "refusal": REFUSAL_BACKING_OFF}

    # Before the fetch, per `services/enrichment_write.py`'s contract: a caller
    # that cannot honour `commit` should be told so before somebody else's
    # server is asked for a page on its behalf.
    locked = check_writable(prop, commit)

    listing = fetch_listing(url)
    if not listing.ok:
        breaker.record_refusal(listing.refusal or "unreachable")
        return {"state": UNCHECKED, "stored": False, "refusal": listing.refusal}
    breaker.record_success()

    block = portal_verdict(
        portal_type=listing.publisher_type,
        client_type_id=listing.client_type_id,
        client_name=listing.agency,
        site="fotocasa",
    )

    outcome: Dict[str, Any] = {
        "state": block["state"],
        "stored": True,
        "refusal": None,
    }
    with locked_write(prop, locked=locked, commit=commit):
        enrichment = dict(getattr(prop, "enrichment", None) or {})
        previous = enrichment.get(ENRICHMENT_KEY)
        previous = previous if isinstance(previous, dict) else {}
        # Read under the lock, decided under the lock: another process may have
        # written a real answer -- or the owner's own -- between the fetch above
        # and this line, which for a fetch of up to 20 s is a window wide enough
        # to have cost two rows of #339 in a pool backfill.
        if (
            previous.get("source") == SOURCE_MANUAL
            or previous.get("state") in MEASURED_STATES
        ):
            outcome = {
                "state": previous.get("state"),
                "stored": False,
                "refusal": REFUSAL_ALREADY_KNOWN,
            }
        else:
            enrichment[ENRICHMENT_KEY] = block
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")

    return outcome


def options(counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """The states to offer, with their counts.

    A state holding nothing is not offered -- the rule the subscription and
    source dropdowns already follow, so that every option leads somewhere.
    """
    return [
        {"value": state, "label": label(state), "count": counts.get(state, 0)}
        for state in STATES
        if counts.get(state, 0) > 0
    ]
