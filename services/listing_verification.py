"""What may be presented as a listing's status, and what may not.

`listing_status` defaults to `'active'` on ingestion. Nothing verified that
default -- it is what a row is born with -- and until this module existed every
surface rendered it exactly like a status a check had confirmed. Measured
2026-08-15: across `properties`, 1 land row of 311 had ever been checked, and
`/properties` presented the other 310 as live listings. Property 192
(idealista 109689073) was withdrawn by the advertiser on 08/05/2026 and still
read as active on the list, so a report built off that feed recommended a dead
listing. That is issue #98's defect wearing the status column's clothes: an
absence of measurement rendered as a measurement.

The rule this module owns, in one sentence: **`active` is a claim, and a claim
needs a source.**

* `removed` / `sold` are always shown. No writer sets them by default -- they
  come from idealista's own removal email, from a check that read the listing
  page, or from the owner -- so a terminal status is always something somebody
  established, even on the rows that predate `listing_status_source` and carry
  NULL there.
* `active` is shown only when `listing_status_source` says a *check* read the
  listing page or the *owner* set it by hand. `ingest` (the birth default),
  `email` (which only ever writes a terminal status) and NULL are not sources
  for a live listing.
* Everything else is `unchecked`, which is a fourth presentation state and not
  a fourth database value. Nothing is migrated and no row is rewritten: the
  database keeps what it knows, and this module stops the templates
  overclaiming it. `'unknown'` -- the stored value for recorded uncertainty --
  presents as `unchecked` too, whatever set it.

Both readings of that rule live here on purpose. `read_verdict` answers it for
one row and `verified_expression` answers it for a query, and
`tests/test_listing_verification.py` runs the same matrix through both, because
a page whose coverage count disagrees with its own badges is worse than a page
with no coverage count.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_

# `check` read the listing page; `manual` is the owner looking at idealista
# themselves, which is the only path that works from this machine today. Both
# are somebody's claim about a *live* listing. `email` is deliberately absent:
# idealista mails a removal notice, never a "still up" notice, so a source of
# `email` on an active row could only be an accident.
VERIFIED_ACTIVE_SOURCES = ("check", "manual")

# Statuses nothing ever assigns by default.
TERMINAL_STATUSES = ("removed", "sold")

# How old a check may be before the age is worth putting in front of the
# reader. A verified status does not expire -- the listing was up when it was
# read -- but "checked in March" and "checked yesterday" are different claims
# about today, so past this the surfaces say how old it is.
STALE_AFTER_DAYS = 30

_SOURCE_NOTES = {
    "email": "Recorded from Idealista's own removal email.",
    "check": "Recorded by a status check that read the listing page.",
    "manual": "Set by hand.",
    "ingest": "The default this listing was ingested with — never verified.",
}
_UNRECORDED_NOTE = "Source not recorded."


def _unchecked_note(record=None) -> str:
    """ "Never verified" — against the site the listing is actually on.

    This sentence used to name Idealista unconditionally. For the 56 fotocasa
    rows in this table that was wrong twice over: nobody had checked them, and
    the site it promised to have checked them against does not carry them at
    all. Naming the row's own source costs one function call and removes a
    claim the reader has no way to see through.
    """
    from utils.listing_source import UNKNOWN, source_label, source_of

    source = source_of(record) if record is not None else UNKNOWN
    where = (
        "against the source site" if source == UNKNOWN else f"on {source_label(source)}"
    )
    return (
        f"Never verified {where} — this is the default a new listing "
        "carries, not a status anybody confirmed."
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes come back from the database; treat them as UTC.

    Persisted time is UTC everywhere in this project, so this normalises rather
    than guesses. Without it the subtraction below raises on the rows that
    matter most -- the ones that were actually checked.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def read_verdict(record, now: Optional[datetime] = None) -> dict:
    """The presentation verdict for one `Property` or `Land` row.

    Returns a dict (not a dataclass) so Jinja can read `verdict.state` the way
    it reads the sea-view verdict, which this deliberately mirrors:

    * `state`      -- 'active', 'removed', 'sold' or 'unchecked'
    * `verified`   -- did anybody establish this, or is it the birth default?
    * `source`     -- the raw `listing_status_source`, or None
    * `checked_at` -- when a check last read the listing page (UTC), or None.
                      `manual` and `email` never stamp it: they are claims, not
                      readings, and dating them would credit a check that never
                      ran.
    * `age_days`   -- age of that reading, or None
    * `stale`      -- verified by a check older than STALE_AFTER_DAYS
    * `note`       -- one sentence a tooltip or a banner can carry verbatim
    """
    now = now or _utcnow()
    status = (getattr(record, "listing_status", None) or "active").lower()
    source = getattr(record, "listing_status_source", None) or None
    checked_at = _as_utc(getattr(record, "listing_last_checked", None))

    age_days = None
    if checked_at is not None:
        age_days = max(0, (now - checked_at).days)

    if status in TERMINAL_STATUSES:
        state, verified = status, True
        note = _SOURCE_NOTES.get(source, _UNRECORDED_NOTE)
    elif status == "unknown":
        # Somebody recorded that they did not know. Presenting that as a live
        # listing would invent the confidence they declined to record.
        state, verified = "unchecked", False
        note = "Recorded as unknown — the listing was never confirmed live."
    elif source in VERIFIED_ACTIVE_SOURCES:
        state, verified = "active", True
        note = _SOURCE_NOTES[source]
    else:
        state, verified = "unchecked", False
        note = _unchecked_note(record)

    stale = bool(
        verified
        and state == "active"
        and age_days is not None
        and age_days > STALE_AFTER_DAYS
    )

    return {
        "state": state,
        "verified": verified,
        "source": source,
        "checked_at": checked_at,
        "age_days": age_days,
        "stale": stale,
        "note": note,
    }


def verified_expression(model):
    """`read_verdict(...)["verified"]` as a SQL predicate over `model`.

    Used for the coverage count the list page discloses. It has to agree with
    `read_verdict` row for row -- a header reading "12 of 311 verified" above a
    table showing 9 ticks is a third wrong number, not a disclosure -- so the
    two are pinned against each other by the same matrix in the tests.
    """
    status = func.lower(func.coalesce(model.listing_status, "active"))
    return or_(
        status.in_(TERMINAL_STATUSES),
        and_(
            status.notin_(TERMINAL_STATUSES + ("unknown",)),
            model.listing_status_source.in_(VERIFIED_ACTIVE_SOURCES),
        ),
    )
