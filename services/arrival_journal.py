"""A listing a second saved search delivers leaves a record, and nothing else.

Issue #632. A portal listing is one row whichever door it comes through
(`services/fotocasa_import.py`), and that row has one `search_profile_id`.
When another saved search delivers the same listing, ingestion finds the row
and moves on -- correctly, there is nothing to create -- and until this module
existed it moved on without a trace: no column, no log line, nothing a later
reader could count. Measured on production 2026-11-21: 2501 portal rows, and
the number of them a second search has delivered is unknowable from what is
stored.

The owner's decision (2026-11-21) is to RECORD the arrival first. Showing a
listing under a second subscription is a separate change (a table of
memberships, migration 030); production carries one live visible
subscription, so that would change nothing on screen today. What this module
does and deliberately does not do:

* it appends one entry to `enrichment["arrivals"]` -- the subscription the
  arrival resolved to, after `SearchProfileService.canonical_profile()`, the
  portal, the door, the email's own id and two UTC times;
* it never touches `search_profile_id`, so the row's home, and with it every
  filter, count, score, criteria verdict and the `routed_to` rule, is exactly
  what it was. Nothing reads the journal except `read()` below; the
  read-only count over it is a separate change.

What an entry means, exactly: "an email that resolves to this subscription
named a listing that was already stored under another one". After a full
re-read of the mailbox that can be the listing's own first email, if matchers
or routes changed since and it resolves elsewhere today -- still a true
statement about where that alert belongs now, but not a second delivery.
`received_at` beside the row's own `email_date` tells the two apart.

Five rules, each a refusal somebody would otherwise have to rediscover:

* **Only another search.** An arrival that resolves to the row's own home is
  the same saved search repeating itself, or two stubs routed onto one target
  -- neither is a second subscription. An email no profile claimed (an
  ambiguous alert resolves to None) names no search at all.
* **Idempotent on the delivery, not on the subscription.** One entry per
  (portal, email id): a held UID re-read, a full sync or a double-pressed
  import confirm records nothing new -- and neither does a replay after the
  email's subscription was routed elsewhere, which a key that included the
  canonical subscription counted twice (review of #698). Entries are never
  rewritten and never removed.
* **Bounded.** `MAX_ITEMS` entries a row, then `truncated: true` and no more
  writes -- a digest that repeats a listing daily must not grow a JSON column
  without limit, and a reader must be able to tell a full journal from a
  complete one.
* **One writer, and every decision under the row lock.** `record()` is the
  only writer of the key and goes through `services/enrichment_write.py`
  (#339). The pending-work guard runs FIRST, before any query: a lookup can
  autoflush somebody's uncommitted edit, after which the guard sees a clean
  session and the journal's commit persists that edit. Then the row is
  locked, and the home, the route and the journal are all read from the
  locked row -- the copy in memory may hold a home the listing has since
  left, and deciding "same search" from it loses the arrival.
* **Total, and fail-closed.** `read()` answers for any stored value; a column
  or a block that is not the shape this module writes reads as "no journal,
  not readable", and `record()` refuses to write over it rather than replace
  something it does not understand.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
UNWRITABLE = "unwritable"


def _stored(prop: Any) -> Optional[Tuple[List[Any], bool]]:
    """The stored journal as `(raw items, truncated)`, or None if unreadable.

    None means the column, or the block inside it, is not the shape this
    module writes -- a legacy list, a string, a block without its list. An
    absent column or key is an empty journal, which is a different fact.
    """
    enrichment = getattr(prop, "enrichment", None)
    if enrichment is None:
        return [], False
    if not isinstance(enrichment, dict):
        return None
    block = enrichment.get(ENRICHMENT_KEY)
    if block is None:
        return [], False
    if not isinstance(block, dict) or not isinstance(block.get("items"), list):
        return None
    return block["items"], block.get("truncated") is True


def _is_entry(item: Any) -> bool:
    profile_id = item.get("profile_id") if isinstance(item, dict) else None
    return isinstance(profile_id, int) and not isinstance(profile_id, bool)


def read(prop: Any) -> Dict[str, Any]:
    """The row's journal: `{"items": [...], "truncated": bool, "readable": bool}`.

    Total: it answers for any stored value and never raises. `items` holds
    only well-formed entries (a dict naming an integer subscription), so a
    caller can use them without checking; `readable` is False when the column
    or the block is not this module's shape, so "nothing recorded" and
    "could not be read" stay two answers.
    """
    stored = _stored(prop)
    if stored is None:
        return {"items": [], "truncated": False, "readable": False}
    items, truncated = stored
    return {
        "items": [item for item in items if _is_entry(item)],
        "truncated": truncated,
        "readable": True,
    }


def _delivered(items: List[Any], source: str, ref: Optional[str]) -> bool:
    """Whether this delivery -- this portal's email id -- is already in `items`."""
    return any(
        isinstance(item, dict)
        and item.get("source") == source
        and item.get("ref") == ref
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

    Returns what happened, one of the module's six words. Owns its
    transaction: it commits on success and rolls back on failure, so the
    caller must hand it a session with nothing pending
    (`enrichment_write.check_writable` refuses otherwise).
    """
    from services import enrichment_write

    if profile_id is None:
        return NO_PROFILE

    # Before anything that can query. Nothing above this line touches the
    # database, and `check_writable` itself issues no statement.
    locked = enrichment_write.check_writable(prop, commit=True)
    with enrichment_write.locked_write(prop, locked=locked, commit=True):
        outcome, arrival_profile_id = _decide_and_write(
            prop,
            profile_id=profile_id,
            source=source,
            method=method,
            ref=ref,
            received_at=received_at,
        )

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
    elif outcome == UNWRITABLE:
        logger.error(
            "Listing %s holds an enrichment value the arrival journal cannot "
            "read; the arrival through subscription %s by %s %s was not recorded",
            prop.id,
            arrival_profile_id,
            source,
            ref,
        )
    return outcome


def _decide_and_write(
    prop: Any,
    *,
    profile_id: int,
    source: str,
    method: str,
    ref: Optional[str],
    received_at: Optional[datetime],
) -> Tuple[str, Optional[int]]:
    """Everything `record()` decides, on the row it has just locked.

    Nothing here trusts a copy from before the lock: `prop` was refreshed
    with the lock, and the subscription is re-read from the database so a
    route set since this session loaded it is the route that counts.
    """
    from app import db
    from models import SearchProfile
    from services.search_profile_service import SearchProfileService

    canonical = SearchProfileService.canonical_profile(
        db.session.get(SearchProfile, profile_id, populate_existing=True)
    )
    if canonical is None:
        return NO_PROFILE, None
    arrival_profile_id = int(canonical.id)

    if prop.search_profile_id == arrival_profile_id:
        return SAME_SEARCH, arrival_profile_id
    stored = _stored(prop)
    if stored is None:
        return UNWRITABLE, arrival_profile_id
    items, truncated = stored
    if _delivered(items, source, ref):
        return ALREADY_RECORDED, arrival_profile_id
    if truncated:
        return TRUNCATED, arrival_profile_id

    # Raw items, malformed ones included: whatever is stored is kept as it is.
    items = list(items)
    full = len(items) >= MAX_ITEMS
    if not full:
        items.append(
            {
                "profile_id": arrival_profile_id,
                "source": source,
                "method": method,
                "ref": ref,
                "received_at": _utc_text(received_at),
                "recorded_at": _utc_text(datetime.now(timezone.utc)),
            }
        )
    # A copy of the column, never a mutation of the stored dict: the JSON
    # column is not a MutableDict, and an in-place change is not a change
    # SQLAlchemy writes.
    enrichment = dict(prop.enrichment or {})
    enrichment[ENRICHMENT_KEY] = {"items": items, "truncated": full}
    prop.enrichment = enrichment
    return (TRUNCATED if full else RECORDED), arrival_profile_id
