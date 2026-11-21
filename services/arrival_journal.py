"""A listing a second saved search delivers leaves a record, and nothing else.

Issue #632. A portal listing is one row whichever door it comes through
(`services/fotocasa_import.py`), with one `search_profile_id`. When another
saved search delivered it again, ingestion found the row and moved on without
a trace: on production 2026-11-21, 2501 portal rows and no way to tell how
many of them a second search had delivered.

This module appends one entry to `enrichment["arrivals"]` -- the subscription
after `SearchProfileService.canonical_profile()`, the portal, the door, the
email's id, the email's time (in UTC where the mail layer knew its zone, as
read and without an offset where it did not) and the UTC time of recording
-- and never touches `search_profile_id`, so
every filter, count, score, criteria verdict and the `routed_to` rule are
what they were. Showing a listing under a second subscription is a separate
change (migration 030); nothing reads the journal yet except `read()`.

An entry means: "an email that resolves to this subscription named a listing
already stored under another one". After a full re-read of the mailbox that
can be the listing's own first email, if matchers or routes changed since:
true about where that alert belongs now, not a second delivery. `received_at`
beside the row's `email_date` tells the two apart.

Five rules, each a refusal somebody would otherwise have to rediscover:

* **Only another search.** An arrival that resolves to the row's own home --
  two stubs routed onto one target included -- is not a second subscription,
  and an email no profile claimed names none.
* **Idempotent on the delivery, not on the subscription.** One entry per
  (portal, email id): a held UID or a full sync adds nothing, and neither
  does a replay after the email's subscription was routed elsewhere, which a
  key that included the subscription counted twice (review of #698). The id
  is REQUIRED: "no id" as a key made two id-less deliveries one, and the
  second search's arrival was dropped (`no_delivery_id`). Entries are never
  rewritten or removed.
* **Bounded.** `MAX_ITEMS` entries a row, then `truncated: true` and no more
  writes, so a full journal cannot be read as a complete one.
* **One writer, every decision under the row lock.** `record()` goes through
  `services/enrichment_write.py` (#339). The pending-work guard runs FIRST,
  before any query: a lookup can autoflush somebody's uncommitted edit, the
  guard then sees a clean session and the journal's commit persists it. The
  home, the route and the journal are read from the locked row -- the copy in
  memory may hold a home the listing has left, and "same search" decided from
  it loses the arrival.
* **Total, and fail-closed.** `read()` answers for any stored value. The
  block is readable only in the exact shape written here -- an object with a
  list under `items` and a boolean under `truncated`; anything else under the
  key, an explicit JSON `null` included, is "not readable" and `record()`
  will not write over it (`unwritable`). Only an ABSENT key is an empty
  journal. A row whose whole `enrichment` is NULL is a new row, not a stored
  value, and is written to.

Not journaled yet (`docs/rules/subscriptions.md`): a link pasted on the
import page for a listing stored under another subscription. That door
writes a batch and needs the entry in the batch's own transaction.
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

# The door, in the word `enrichment["import"]["method"]` already uses.
METHOD_ALERT_EMAIL = "alert_email"

RECORDED = "recorded"
SAME_SEARCH = "same_search"
NO_PROFILE = "no_profile"
ALREADY_RECORDED = "already_recorded"
TRUNCATED = "truncated"
UNWRITABLE = "unwritable"
NO_DELIVERY_ID = "no_delivery_id"


def _stored(prop: Any) -> Optional[Dict[str, Any]]:
    """The stored block, an empty one where none is stored, or None.

    None means "not readable": the column is not an object, or the key is
    present and holds anything but the exact shape `record()` writes -- an
    object with a list under `items` and a boolean under `truncated`. An
    explicit JSON null under the key is such a value: somebody stored it, and
    it is not this module's to replace. Only an absent key (or a row with no
    enrichment at all) is an empty journal.
    """
    enrichment = getattr(prop, "enrichment", None)
    if enrichment is None:
        return {"items": [], "truncated": False}
    if not isinstance(enrichment, dict):
        return None
    if ENRICHMENT_KEY not in enrichment:
        return {"items": [], "truncated": False}
    block = enrichment[ENRICHMENT_KEY]
    if (
        not isinstance(block, dict)
        or not isinstance(block.get("items"), list)
        or not isinstance(block.get("truncated"), bool)
    ):
        return None
    return block


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
    return {
        "items": [item for item in stored["items"] if _is_entry(item)],
        "truncated": stored["truncated"],
        "readable": True,
    }


def _delivered(items: List[Any], source: str, ref: str) -> bool:
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

    Returns what happened, one of the module's seven words. Owns its
    transaction: it commits on success and rolls back on failure, so the
    caller must hand it a session with nothing pending
    (`enrichment_write.check_writable` refuses otherwise).

    `ref` identifies the delivery and has no default meaning: without a
    non-empty one nothing is written.
    """
    from services import enrichment_write

    if profile_id is None:
        return NO_PROFILE
    if not isinstance(ref, str) or not ref.strip():
        logger.error(
            "An arrival of listing %s through subscription %s by %s carries no "
            "delivery id and was not recorded",
            getattr(prop, "id", None),
            profile_id,
            source,
        )
        return NO_DELIVERY_ID

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
    ref: str,
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
    if _delivered(stored["items"], source, ref):
        return ALREADY_RECORDED, arrival_profile_id
    if stored["truncated"]:
        return TRUNCATED, arrival_profile_id

    # Raw items, malformed ones included, and any other key of the block:
    # whatever is stored is kept as it is.
    items = list(stored["items"])
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
    enrichment[ENRICHMENT_KEY] = {**stored, "items": items, "truncated": full}
    prop.enrichment = enrichment
    return (TRUNCATED if full else RECORDED), arrival_profile_id
