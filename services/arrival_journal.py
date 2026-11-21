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
  (portal, email id): a held UID, a full sync or a replay after the email's
  subscription was routed elsewhere adds nothing. The id is REQUIRED: "no
  id" as a key made two id-less deliveries one (`no_delivery_id`).
* **Bounded.** `MAX_ITEMS` entries a row, then `truncated: true` and no more
  writes, so a full journal cannot be read as a complete one.
* **One writer, every decision under the row lock.** `record()` goes through
  `services/enrichment_write.py` (#339). Its pending-work guard is its FIRST
  statement, ahead of every read of an ORM attribute, a log line included:
  such a read can be a query, a query autoflushes somebody's uncommitted
  edit, and the journal's commit then persists it. The home, the route and
  the journal are read from the locked row; the copy in memory may hold a
  home the listing has left. Two sessions on one row are proved on
  PostgreSQL (`tests/test_postgres_migrations.py`).
* **Total, fail-closed, one reading of the shape.** `read()` answers for any
  stored value. The block is readable only as an object with a boolean under
  `truncated` and, under `items`, a list in which EVERY item is an entry
  (`_is_entry`). Anything else under the key -- an explicit JSON `null`, one
  malformed item -- makes the whole block "not readable" and `record()` will
  not write over it (`unwritable`). The duplicate check reads through the
  same function, so an item `read()` would not return can never count as
  "already recorded". Only an ABSENT key is an empty journal; a row whose
  whole `enrichment` is NULL is a new row and is written to.

Not journaled yet (`docs/rules/subscriptions.md`): a link pasted on the
import page for a listing stored under another subscription.
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


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_entry(item: Any) -> bool:
    """The one definition of an entry: who, which portal, which delivery."""
    if not isinstance(item, dict):
        return False
    profile_id = item.get("profile_id")
    return (
        isinstance(profile_id, int)
        and not isinstance(profile_id, bool)
        and _is_id(item.get("source"))
        and _is_id(item.get("ref"))
    )


def _stored(prop: Any) -> Optional[Dict[str, Any]]:
    """The stored block, an empty one where none is stored, or None.

    None is "not readable": the column is not an object, or the key holds
    anything but the exact shape `record()` writes. An explicit JSON null is
    such a value, and so is a block with one malformed item. Only an absent
    key (or a row with no enrichment at all) is an empty journal.
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
        or not isinstance(block.get("truncated"), bool)
        or not isinstance(block.get("items"), list)
        or not all(_is_entry(item) for item in block["items"])
    ):
        return None
    return block


def read(prop: Any) -> Dict[str, Any]:
    """The row's journal: `{"items": [...], "truncated": bool, "readable": bool}`.

    Total: it answers for any stored value and never raises. Every item is an
    entry, so a caller can use them without checking; `readable` is False
    when the column or the block is not this module's shape, so "nothing
    recorded" and "could not be read" stay two answers.
    """
    stored = _stored(prop)
    if stored is None:
        return {"items": [], "truncated": False, "readable": False}
    return {
        "items": list(stored["items"]),
        "truncated": stored["truncated"],
        "readable": True,
    }


def _delivered(items: List[Dict[str, Any]], source: str, ref: str) -> bool:
    """Whether this delivery -- this portal's email id -- is among `items`.

    `items` come from `_stored`, so each one is an entry `read()` returns.
    """
    return any(item["source"] == source and item["ref"] == ref for item in items)


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
    caller must hand it a session with nothing pending.

    `ref` identifies the delivery and has no default meaning: without a
    non-empty one nothing is written.
    """
    from services import enrichment_write

    # The first statement, on purpose. It issues no query; everything after
    # it may, and until the row is locked nothing below reads an attribute
    # of `prop` -- not even for a log line.
    locked = enrichment_write.check_writable(prop, commit=True)

    if profile_id is None:
        return NO_PROFILE
    if not _is_id(ref):
        logger.error(
            "An arrival through subscription %s by %s carries no delivery id "
            "and was not recorded",
            profile_id,
            source,
        )
        return NO_DELIVERY_ID

    with enrichment_write.locked_write(prop, locked=locked, commit=True):
        outcome, arrival_profile_id, prop_id, home_id = _decide_and_write(
            prop,
            profile_id=profile_id,
            source=source,
            method=method,
            ref=ref,
            received_at=received_at,
        )

    # Locals only: after the commit `prop` is expired, and reading it again
    # would be one more query for the sake of a log line.
    if outcome == RECORDED:
        logger.info(
            "Listing %s (home subscription %s) also arrived through "
            "subscription %s by %s %s",
            prop_id,
            home_id,
            arrival_profile_id,
            source,
            ref,
        )
    elif outcome == UNWRITABLE:
        logger.error(
            "Listing %s holds an enrichment value the arrival journal cannot "
            "read; the arrival through subscription %s by %s %s was not recorded",
            prop_id,
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
) -> Tuple[str, Optional[int], Any, Any]:
    """Everything `record()` decides, on the row it has just locked.

    Returns the outcome, the arrival's subscription, and the row's id and
    home as read under the lock. The subscription is re-read too, so a route
    set since this session loaded it is the route that counts.
    """
    from app import db
    from models import SearchProfile
    from services.search_profile_service import SearchProfileService

    prop_id, home_id = prop.id, prop.search_profile_id
    canonical = SearchProfileService.canonical_profile(
        db.session.get(SearchProfile, profile_id, populate_existing=True)
    )
    if canonical is None:
        return NO_PROFILE, None, prop_id, home_id
    arrival = int(canonical.id)

    if home_id == arrival:
        return SAME_SEARCH, arrival, prop_id, home_id
    stored = _stored(prop)
    if stored is None:
        return UNWRITABLE, arrival, prop_id, home_id
    if _delivered(stored["items"], source, ref):
        return ALREADY_RECORDED, arrival, prop_id, home_id
    if stored["truncated"]:
        return TRUNCATED, arrival, prop_id, home_id

    items = list(stored["items"])
    full = len(items) >= MAX_ITEMS
    if not full:
        items.append(
            {
                "profile_id": arrival,
                "source": source,
                "method": method,
                "ref": ref,
                "received_at": _utc_text(received_at),
                "recorded_at": _utc_text(datetime.now(timezone.utc)),
            }
        )
    # A copy of the column, never a mutation of the stored dict: the JSON
    # column is not a MutableDict, and an in-place change is not a change
    # SQLAlchemy writes. Any other key of the block is kept as it is.
    enrichment = dict(prop.enrichment or {})
    enrichment[ENRICHMENT_KEY] = {**stored, "items": items, "truncated": full}
    prop.enrichment = enrichment
    return (TRUNCATED if full else RECORDED), arrival, prop_id, home_id
