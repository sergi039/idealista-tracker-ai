"""A listing a second saved search delivers leaves a record, and nothing else.

Issue #632. A portal listing is one row whichever door it comes through
(`services/fotocasa_import.py`), and that row has one `search_profile_id`.
When another saved search delivers the same listing, ingestion finds the row
and moves on -- correctly, there is nothing to create -- and until this module
existed it moved on without a trace: no column, no log line, nothing a later
reader could count. Measured on production 2026-11-21: 2501 portal rows, and
the number of them a second search has delivered is unknowable from what is
stored.

The owner's decision (2026-11-21) is to RECORD the arrival and stop there.
Production carries one live visible subscription, so showing a listing under
a second one would change nothing on screen today; a table of memberships
(reviewed, plan-gate PASS, parked on the issue) waits for a second live
subscription. What this module does and deliberately does not do:

* it appends one entry to `enrichment["arrivals"]` -- the subscription the
  arrival resolved to, after `SearchProfileService.canonical_profile()`, the
  portal, the door, the email's own id and two UTC times;
* it never touches `search_profile_id`, so the row's home, and with it every
  filter, count, score, criteria verdict and the `routed_to` rule, is exactly
  what it was. Nothing reads the journal except `read()` below and
  `utils/report_arrivals.py`.

What an entry means, exactly: "an email that resolves to this subscription
named a listing that was already stored under another one". After a full
re-read of the mailbox that can be the listing's own first email, if matchers
or routes changed since and it resolves elsewhere today -- still a true
statement about where that alert belongs now, but not a second delivery.
`received_at` beside the row's own `email_date` tells the two apart.

Four rules, each a refusal somebody would otherwise have to rediscover:

* **Only another search.** An arrival that resolves to the row's own home is
  the same saved search repeating itself, or two stubs routed onto one target
  -- neither is a second subscription. An email no profile claimed (an
  ambiguous alert resolves to None) names no search at all.
* **Idempotent on the email.** One entry per (subscription, email id): a held
  UID re-read, a full sync, or a double-pressed import confirm records
  nothing new. Entries are never rewritten and never removed.
* **Bounded.** `MAX_ITEMS` entries a row, then `truncated: true` and no more
  writes -- a digest that repeats a listing daily must not grow a JSON column
  without limit, and a reader must be able to tell a full journal from a
  complete one.
* **One writer, under the row lock.** `record()` is the only writer of the
  key and goes through `services/enrichment_write.py`: the row is locked, the
  journal is re-read from the locked row, a copy of the column is written and
  the transaction ends on every exit (#339). It therefore needs a session
  with nothing pending and says so loudly if it is handed one.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ENRICHMENT_KEY = "arrivals"

# A row's journal stops here. Fifty second-search emails for one listing is
# already far past anything the measurement needs; past it the journal says
# `truncated` instead of growing.
MAX_ITEMS = 50

# The doors, in the words `enrichment["import"]["method"]` already uses.
METHOD_ALERT_EMAIL = "alert_email"
METHOD_PASTE_IMPORT = "portal_payload"

RECORDED = "recorded"
SAME_SEARCH = "same_search"
NO_PROFILE = "no_profile"
ALREADY_RECORDED = "already_recorded"
TRUNCATED = "truncated"


def read(prop: Any) -> Dict[str, Any]:
    """The row's journal as `{"items": [...], "truncated": bool}`. Total.

    Anything that is not the shape `record()` writes reads as an empty
    journal: an absent key and a malformed one both mean "nothing recorded",
    and neither is a reason for a caller to fail.
    """
    block = (getattr(prop, "enrichment", None) or {}).get(ENRICHMENT_KEY)
    if not isinstance(block, dict) or not isinstance(block.get("items"), list):
        return {"items": [], "truncated": False}
    return {
        "items": [item for item in block["items"] if isinstance(item, dict)],
        "truncated": block.get("truncated") is True,
    }


def _already(items: List[Dict[str, Any]], profile_id: int, ref: Optional[str]) -> bool:
    return any(
        item.get("profile_id") == profile_id and item.get("ref") == ref
        for item in items
    )


def _utc_text(value: Optional[datetime]) -> Optional[str]:
    """An aware time as UTC text; a missing one stays missing.

    A naive datetime carries no zone, so none is invented for it: it is
    written as it was read, without an offset, and the absence of the offset
    is the record that nobody knew.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat(timespec="seconds")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def record(
    prop: Any,
    *,
    profile_id: Optional[int],
    source: str,
    method: str,
    ref: Optional[str],
    received_at: Optional[datetime] = None,
) -> str:
    """Record that `profile_id`'s saved search delivered the stored `prop`.

    Returns what happened, one of the module's five words. Owns its
    transaction: it commits on success and rolls back on failure, so the
    caller must hand it a session with nothing pending
    (`enrichment_write.check_writable` refuses otherwise).
    """
    from app import db
    from models import SearchProfile
    from services import enrichment_write
    from services.search_profile_service import SearchProfileService

    if profile_id is None:
        return NO_PROFILE
    canonical = SearchProfileService.canonical_profile(
        db.session.get(SearchProfile, profile_id)
    )
    if canonical is None:
        return NO_PROFILE
    arrival_profile_id = int(canonical.id)

    # Answered from the copy in memory where that copy is enough. Entries are
    # never removed and the flag is never cleared, so an entry or a flag seen
    # here is one that is stored; only "not seen" needs the locked re-read.
    if prop.search_profile_id == arrival_profile_id:
        return SAME_SEARCH
    seen = read(prop)
    if _already(seen["items"], arrival_profile_id, ref):
        return ALREADY_RECORDED
    if seen["truncated"]:
        return TRUNCATED

    entry = {
        "profile_id": arrival_profile_id,
        "source": source,
        "method": method,
        "ref": ref,
        "received_at": _utc_text(received_at),
        "recorded_at": _utc_text(datetime.now(timezone.utc)),
    }

    locked = enrichment_write.check_writable(prop, commit=True)
    with enrichment_write.locked_write(prop, locked=locked, commit=True):
        # Everything below reads the locked row, never the copy above: the
        # home may have moved (a route) and another run may have recorded
        # this very email while this one waited for the lock.
        if prop.search_profile_id == arrival_profile_id:
            return SAME_SEARCH
        journal = read(prop)
        items = list(journal["items"])
        if _already(items, arrival_profile_id, ref):
            return ALREADY_RECORDED
        if journal["truncated"]:
            return TRUNCATED

        full = len(items) >= MAX_ITEMS
        if not full:
            items.append(entry)
        # A copy of the column, never a mutation of the stored dict: the JSON
        # column is not a MutableDict, and an in-place change is not a change
        # SQLAlchemy writes.
        enrichment = dict(prop.enrichment or {})
        enrichment[ENRICHMENT_KEY] = {"items": items, "truncated": full}
        prop.enrichment = enrichment
        outcome = TRUNCATED if full else RECORDED

    if outcome == RECORDED:
        logger.info(
            "Listing %s (home subscription %s) also arrived through "
            "subscription %s by %s %s",
            prop.id,
            prop.search_profile_id,
            arrival_profile_id,
            source,
            ref,
        )
    return outcome
