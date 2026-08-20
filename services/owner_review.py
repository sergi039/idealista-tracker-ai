"""What the owner decided about a listing, and what is still outstanding on it.

Everything else this application stores about a property was measured: a drive
time, a sea-view verdict, a price. This module holds the two things nobody can
measure -- the conclusion a person reached, and the thing they are still
waiting for. Issue #430 arrived from a day (2026-08-20, property 774) in which
one listing collected a cadastral document, a promise with a date, three verbal
answers and a rejection, and every one of them had to be written into
`enrichment` by hand through `docker exec`, where it rendered nowhere.

**Two readings, deliberately independent.**

* The **decision** -- `interested`, `waiting`, `rejected` -- is what the owner
  concluded. `undecided` is the fourth state and it is *not* a database value:
  it is what NULL reads as, exactly as `unchecked` is in
  `services/listing_verification.py` and `services/advertiser.py`. An absence
  of a decision is not a rejection, and the filter offers it as its own option
  rather than folding it into one (#98).
* The **action** -- `none`, `pending`, `overdue` -- is what is still
  outstanding. It is legal under any decision: "interested; call the architect
  on Friday" is an ordinary state, and hanging the reminder off `waiting` would
  lose it. Nothing writes `overdue`; it is derived from the due date, so the
  badge and the column cannot drift.

**One date per request.** `overdue` compares a due date against today, and
today is a calendar date the owner reads off a calendar in Spain -- so it is
`Europe/Madrid` (`Config.SCHEDULER_TIMEZONE`), not UTC like every other
timestamp here, and not the container's local date. A view computes it **once**
and passes that one value into the filter, the counts, the badge and both
serializers: three independent `now()` calls in one request is how a badge
comes to disagree with the count printed above it, and at 23:59 Madrid in
winter the UTC date is already tomorrow.

**Both readings, in both languages.** `read_decision` / `read_action` for a row
and `decision_expression` / `action_expression` for a query, branch for branch,
the contract `services/advertiser.py` states: the badge, the filter and the
count beside its option are one answer rather than three, and
`tests/test_owner_review.py` runs one matrix through both.

The two row readers are **pure**: no session, no query. The list calls them
once per row, so a reader that reached the database would be an N+1 on a page
that already renders four other verdicts per row.

`set_review` is the one writer, and it owns its transaction. It takes the row
`FOR UPDATE` before it reads the old state, because two presses on four gunicorn
threads can otherwise read the same old decision and append two contradictory
transitions, each atomic and both wrong -- the shape of #339, one column over.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import String, and_, case, cast, func, literal, or_

logger = logging.getLogger(__name__)

# --- the decision -----------------------------------------------------------

INTERESTED = "interested"
WAITING = "waiting"
REJECTED = "rejected"
UNDECIDED = "undecided"

# What may be written. `undecided` is not among them: nothing writes it, it is
# what an empty column reads as.
DECIDED_STATES: Tuple[str, ...] = (INTERESTED, WAITING, REJECTED)

# The order the filter offers them in, and the only values it accepts.
DECISION_STATES: Tuple[str, ...] = (INTERESTED, WAITING, REJECTED, UNDECIDED)

# --- the action -------------------------------------------------------------

ACTION_NONE = "none"
ACTION_PENDING = "pending"
ACTION_OVERDUE = "overdue"

ACTION_STATES: Tuple[str, ...] = (ACTION_OVERDUE, ACTION_PENDING, ACTION_NONE)

# --- the activity log -------------------------------------------------------

KIND_NOTE = "note"
KIND_CONTACT = "contact"
KIND_VERDICT = "verdict"

CHANNELS: Tuple[str, ...] = (
    "whatsapp",
    "email",
    "portal",
    "phone",
    "visit",
    "other",
)

_DECISION_LABEL_KEYS: Dict[str, str] = {
    INTERESTED: "owner_verdict_interested",
    WAITING: "owner_verdict_waiting",
    REJECTED: "owner_verdict_rejected",
    UNDECIDED: "owner_verdict_undecided",
}

_ACTION_LABEL_KEYS: Dict[str, str] = {
    ACTION_OVERDUE: "next_action_overdue",
    ACTION_PENDING: "next_action_pending",
    ACTION_NONE: "next_action_none",
}


def decision_label_key(state: Optional[str]) -> str:
    """The i18n key for a decision. Never a bare slug on screen."""
    return _DECISION_LABEL_KEYS.get(
        (state or "").strip().lower(), _DECISION_LABEL_KEYS[UNDECIDED]
    )


def action_label_key(state: Optional[str]) -> str:
    return _ACTION_LABEL_KEYS.get(
        (state or "").strip().lower(), _ACTION_LABEL_KEYS[ACTION_NONE]
    )


def today() -> date:
    """The date the owner would read off a calendar.

    Madrid, not UTC. The rest of this application timestamps in UTC and should
    go on doing so; a *due date* is different in kind -- it is a calendar date,
    and at 23:59 on the 20th in Madrid a due date of the 20th is not yet
    overdue even though UTC has already turned over in summer.
    """
    from config import Config

    return datetime.now(ZoneInfo(Config.SCHEDULER_TIMEZONE)).date()


def _blank(value: Optional[str]) -> bool:
    """Nothing, or nothing but whitespace.

    The database says the same thing with `~ '[^[:space:]]'` (migration 021).
    `str.strip()` and that regex agree on tabs and newlines; both are stricter
    than `BTRIM`, which strips spaces only.
    """
    return not (value or "").strip()


def read_decision(record: Any) -> Dict[str, Any]:
    """One row's decision. Pure: no session, no query.

    `decided` is the honest half of the answer -- False means nobody has
    decided, which is not the same fact as a rejection and must never render
    like one.
    """
    stored = getattr(record, "owner_verdict", None)
    state = (stored or "").strip().lower()
    if state not in DECIDED_STATES:
        return {
            "state": UNDECIDED,
            "decided": False,
            "reason": None,
            "decided_at": None,
        }
    reason = getattr(record, "owner_verdict_reason", None)
    return {
        "state": state,
        "decided": True,
        "reason": None if _blank(reason) else reason,
        "decided_at": getattr(record, "owner_verdict_at", None),
    }


def read_action(record: Any, on_date: Optional[date] = None) -> Dict[str, Any]:
    """One row's outstanding action. Pure: no session, no query.

    `on_date` is the request's one date. It defaults for a single-row caller;
    a collection must pass its own, or the row and the query that selected it
    can disagree across midnight.
    """
    action = getattr(record, "next_action", None)
    if _blank(action):
        return {
            "state": ACTION_NONE,
            "action": None,
            "due_on": None,
            "overdue": False,
            "days_over": None,
        }

    due_on = getattr(record, "next_action_due_on", None)
    if isinstance(due_on, datetime):
        due_on = due_on.date()

    # No date is `pending`, never `overdue`: an action with no deadline is the
    # ordinary "still deciding what to ask" state, and `None < date` raises.
    if due_on is None:
        return {
            "state": ACTION_PENDING,
            "action": action,
            "due_on": None,
            "overdue": False,
            "days_over": None,
        }

    reference = on_date or today()
    overdue = due_on < reference
    return {
        "state": ACTION_OVERDUE if overdue else ACTION_PENDING,
        "action": action,
        "due_on": due_on,
        "overdue": overdue,
        "days_over": (reference - due_on).days if overdue else None,
    }


def decision_expression(model: Any):
    """`read_decision(...)["state"]` as SQL, branch for branch.

    The dropdown prints a count next to every option, and a count that
    disagrees with the badges below it is a third wrong number rather than a
    disclosure.
    """
    stored = func.lower(func.trim(func.coalesce(model.owner_verdict, "")))
    return case((stored.in_(DECIDED_STATES), stored), else_=literal(UNDECIDED))


def action_expression(model: Any, on_date: Optional[date] = None):
    """`read_action(...)["state"]` as SQL, against one bound date.

    The date is bound as a literal parameter rather than computed by the
    database, because `CURRENT_DATE` is the *server's* today and the Python
    reader's is Madrid's -- and a page whose badge and filter disagree once a
    day is worse than one that is wrong all the time, because nobody catches
    it.
    """
    reference = on_date or today()
    has_action = func.coalesce(cast(model.next_action, String), "").op("~")(
        "[^[:space:]]"
    )
    due = model.next_action_due_on
    return case(
        (~has_action, literal(ACTION_NONE)),
        (and_(due.isnot(None), due < literal(reference)), literal(ACTION_OVERDUE)),
        else_=literal(ACTION_PENDING),
    )


def _sqlite_has_action(model: Any):
    """`next_action` holds a non-whitespace character, without a POSIX regex.

    SQLite (which the test suite runs on) has no `~` operator, so the
    expression above cannot be the only reading. This is the same predicate
    written in operators both engines have: not NULL, and not equal to any of
    the whitespace-only strings `TRIM` can produce. `TRIM` in SQLite strips
    spaces only, like PostgreSQL's -- hence the explicit tab/newline cases.
    """
    text = func.coalesce(model.next_action, "")
    return and_(
        model.next_action.isnot(None),
        ~text.in_(("", " ", "\t", "\n", "\r", "\r\n")),
        func.trim(text) != "",
    )


def action_expression_portable(model: Any, on_date: Optional[date] = None):
    """`action_expression` for whichever engine is under us.

    PostgreSQL gets the regex, which is the same test the CHECK constraint
    applies. SQLite gets the operator form above. They are separated here, in
    one place, rather than at every call site -- and `tests/test_owner_review.py`
    runs its matrix through this function, so what the suite proves is what the
    surfaces call.
    """
    from app import db

    reference = on_date or today()
    dialect = ""
    try:
        dialect = db.session.get_bind().dialect.name
    except Exception:  # pragma: no cover - no bind outside an app context
        dialect = ""

    has_action = (
        func.coalesce(cast(model.next_action, String), "").op("~")("[^[:space:]]")
        if dialect == "postgresql"
        else _sqlite_has_action(model)
    )
    due = model.next_action_due_on
    return case(
        (~has_action, literal(ACTION_NONE)),
        (and_(due.isnot(None), due < literal(reference)), literal(ACTION_OVERDUE)),
        else_=literal(ACTION_PENDING),
    )


def decision_filter_clause(model: Any, state: Optional[str]):
    """The decision filter, as one clause (None when unset).

    Shared by every listing surface the way they already share
    `municipality_filter_clause` and `advertiser.filter_clause`, so they cannot
    drift into different answers to "show me the ones I rejected".
    """
    wanted = (state or "").strip().lower()
    if wanted not in DECISION_STATES:
        return None
    if wanted == UNDECIDED:
        # Expressed directly rather than through the CASE so the index on
        # owner_verdict is usable: the common filter is "the ones I have not
        # looked at yet", over a mostly-empty column.
        return or_(
            model.owner_verdict.is_(None),
            func.lower(func.trim(model.owner_verdict)).notin_(DECIDED_STATES),
        )
    return func.lower(func.trim(func.coalesce(model.owner_verdict, ""))) == wanted


def action_filter_clause(
    model: Any, state: Optional[str], on_date: Optional[date] = None
):
    """The action filter, as one clause (None when unset)."""
    wanted = (state or "").strip().lower()
    if wanted not in ACTION_STATES:
        return None
    return action_expression_portable(model, on_date) == wanted


def decision_options(counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """The decisions to offer, with their counts.

    `undecided` is offered whenever it holds anything, unlike a state a
    dropdown may quietly drop: "nobody decided yet" is the disclosure that
    stops the other three from reading as a complete tally.
    """
    return [
        {
            "value": state,
            "label_key": decision_label_key(state),
            "count": counts.get(state, 0),
        }
        for state in DECISION_STATES
        if counts.get(state, 0) > 0
    ]


def action_options(counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """The action states to offer. `none` is not offered.

    Every row with nothing outstanding is `none`, so an option counting most of
    the table selects nothing anyone is looking for -- the reason
    `advertiser.py` badges one state out of four.
    """
    return [
        {
            "value": state,
            "label_key": action_label_key(state),
            "count": counts.get(state, 0),
        }
        for state in (ACTION_OVERDUE, ACTION_PENDING)
        if counts.get(state, 0) > 0
    ]


def review_snapshot(record: Any) -> Dict[str, Any]:
    """The whole review state of a row, as it would be recorded in an event.

    Whole, not a from/to pair: a changed reason or a moved due date under an
    unchanged decision is a real change, and a pair loses it.
    """
    due_on = getattr(record, "next_action_due_on", None)
    if isinstance(due_on, datetime):
        due_on = due_on.date()
    decided_at = getattr(record, "owner_verdict_at", None)
    action = getattr(record, "next_action", None)
    reason = getattr(record, "owner_verdict_reason", None)
    return {
        "decision": (getattr(record, "owner_verdict", None) or None),
        "reason": None if _blank(reason) else reason,
        "action": None if _blank(action) else action,
        "due_on": due_on.isoformat() if due_on else None,
        "decided_at": decided_at.isoformat() if decided_at else None,
    }


def _same_review(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """Two snapshots describing the same decision.

    `decided_at` is excluded: every write moves it, so comparing it would make
    "nothing changed" impossible to detect and put a second identical entry in
    the log for a second press of Save.
    """
    keys = ("decision", "reason", "action", "due_on")
    return all(left.get(key) == right.get(key) for key in keys)


class ReviewError(ValueError):
    """A rejected write. The route turns it into a flash, never a 500."""


def set_review(
    prop: Any,
    *,
    decision: Optional[str],
    reason: Optional[str] = None,
    action: Optional[str] = None,
    due_on: Optional[date] = None,
) -> Dict[str, Any]:
    """Record the decision and the outstanding action. The one writer.

    It owns its transaction. There is deliberately no `commit=False` mode: this
    function takes `FOR UPDATE` on the row, and a lock whose release the callee
    cannot see is worse than the race it was taken against -- the rule
    `services/enrichment_write.py` already states for `enrichment`. A caller
    that needs a wider transaction uses `_apply_review_locked` and holds the
    lock itself.

    `decision=None` clears the decision. Clearing is not `rejected`: a row
    nobody has judged and a row somebody rejected are different facts, and the
    only way back to the first is for this to write NULL.
    """
    from app import db

    wanted = (decision or "").strip().lower() or None
    if wanted is not None and wanted not in DECIDED_STATES:
        raise ReviewError(f"unknown decision: {decision!r}")
    if due_on is not None and _blank(action):
        # The database says the same thing; saying it here names the field.
        raise ReviewError("a due date needs an action to be due")

    if prop not in db.session:
        raise ReviewError(
            "the review write was handed a property this session does not hold"
        )
    if db.session.new or db.session.dirty or db.session.deleted:
        # This function ends the transaction on every exit, so anything else in
        # flight would be committed or discarded wholesale -- the contract
        # `services/enrichment_write.check_writable` states in the same words.
        raise ReviewError("a review write needs a session with nothing pending")

    try:
        db.session.refresh(prop, with_for_update=True)
        result = _apply_review_locked(
            prop, decision=wanted, reason=reason, action=action, due_on=due_on
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


def _apply_review_locked(
    prop: Any,
    *,
    decision: Optional[str],
    reason: Optional[str],
    action: Optional[str],
    due_on: Optional[date],
) -> Dict[str, Any]:
    """Write the columns and append the event. The row must already be locked.

    Module-private and it makes no promise of its own: it neither commits nor
    rolls back, and it is correct only inside a transaction whose caller holds
    this row.
    """
    from app import db
    from models import PropertyActivity

    previous = review_snapshot(prop)
    previously_decided_at = prop.owner_verdict_at

    prop.owner_verdict = decision
    prop.owner_verdict_reason = None if _blank(reason) else reason.strip()
    prop.next_action = None if _blank(action) else action.strip()
    prop.next_action_due_on = due_on if prop.next_action else None
    prop.owner_verdict_at = datetime.now(timezone.utc).replace(tzinfo=None)

    current = review_snapshot(prop)
    if _same_review(current, previous):
        # Nothing changed. An event saying so would be noise in the one place
        # the owner reads to find out what happened -- and the timestamp is put
        # back, because "when this was decided" did not change either. Pressing
        # Save twice must leave the row exactly as one press left it.
        prop.owner_verdict_at = previously_decided_at
        return {"changed": False, "snapshot": review_snapshot(prop)}

    snapshot = dict(current)
    snapshot["previous"] = previous
    stamped = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.add(
        PropertyActivity(
            property_id=prop.id,
            kind=KIND_VERDICT,
            happened_at=stamped,
            body=None,
            snapshot=snapshot,
            created_at=stamped,
            updated_at=stamped,
        )
    )
    return {"changed": True, "snapshot": current}


def history_out_of_sync(prop: Any) -> bool:
    """Do the columns disagree with the newest verdict event?

    One query, and only the property page asks it. It is a **disclosure, not a
    guarantee**: direct SQL is a supported workflow here (`curate_on_mini.sh`,
    `docker exec ... psql` -- property 774's own data arrived that way), and a
    column written outside the app leaves no event behind. What this can see is
    that the newest event no longer describes the current state; it cannot see
    an older transition that was never recorded, and it does not claim to.
    """
    from models import PropertyActivity

    newest = (
        PropertyActivity.query.filter(
            PropertyActivity.property_id == prop.id,
            PropertyActivity.kind == KIND_VERDICT,
            PropertyActivity.deleted_at.is_(None),
        )
        .order_by(PropertyActivity.happened_at.desc(), PropertyActivity.id.desc())
        .first()
    )
    current = review_snapshot(prop)
    if newest is None:
        # No event and no review state is the ordinary untouched row.
        return any(current[key] is not None for key in ("decision", "action", "reason"))

    recorded = dict(newest.snapshot or {})
    recorded.pop("previous", None)
    # `decided_at` moves on every write and says nothing about agreement, so
    # the comparison is the same one the writer uses to decide "nothing
    # changed" -- two readings of "is this the state that was recorded".
    return not _same_review(recorded, current)


# --- the timeline -----------------------------------------------------------


def timeline(prop: Any, *, include_deleted: bool = False) -> List[Any]:
    """One property's entries, newest first. The reverse-chronological feed.

    Ordered by `happened_at` and not by `created_at`: an answer given on the
    phone yesterday is typed today, and a feed ordered by when somebody sat
    down to record things tells the story in the wrong order. `id` breaks ties
    so two entries stamped the same minute keep a stable order.

    Soft-deleted entries are out by default. They are *kept* rather than
    removed because a sentence the owner typed is the one thing in this
    application nothing can recompute.
    """
    from models import PropertyActivity

    query = PropertyActivity.query.filter(PropertyActivity.property_id == prop.id)
    if not include_deleted:
        query = query.filter(PropertyActivity.deleted_at.is_(None))
    return query.order_by(
        PropertyActivity.happened_at.desc(), PropertyActivity.id.desc()
    ).all()


def add_note(prop: Any, *, body: str, happened_at: Optional[datetime] = None) -> Any:
    """Record a note. The text is the entry; a blank one is refused."""
    from app import db
    from models import PropertyActivity

    if _blank(body):
        raise ReviewError("a note is its text")

    # `created_at` and `updated_at` are set from ONE value rather than left to
    # two separate column defaults: `was_edited` compares them exactly, and two
    # `utcnow()` calls on the same insert land microseconds apart. A tolerance
    # instead of this would have to be wide enough to cover that and narrow
    # enough to notice a correction typed three seconds later, and there is no
    # such number.
    stamped = datetime.now(timezone.utc).replace(tzinfo=None)
    entry = PropertyActivity(
        property_id=prop.id,
        kind=KIND_NOTE,
        happened_at=happened_at or stamped,
        body=body.strip(),
        created_at=stamped,
        updated_at=stamped,
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def add_contact(
    prop: Any,
    *,
    channel: str,
    counterpart: Optional[str] = None,
    asked: Optional[str] = None,
    body: Optional[str] = None,
    happened_at: Optional[datetime] = None,
) -> Any:
    """Record one exchange.

    A channel is required, and something has to have been exchanged or
    somebody named -- a visit with nothing written down is a real entry as
    long as it says who was met, and an entirely empty row is not. The
    database says the same thing (migration 021); this says it in a sentence
    that names the field.
    """
    from app import db
    from models import PropertyActivity

    wanted = (channel or "").strip().lower()
    if wanted not in CHANNELS:
        raise ReviewError(f"unknown channel: {channel!r}")
    if _blank(counterpart) and _blank(asked) and _blank(body):
        raise ReviewError("a contact entry needs who was spoken to, or what was said")

    stamped = datetime.now(timezone.utc).replace(tzinfo=None)
    entry = PropertyActivity(
        property_id=prop.id,
        kind=KIND_CONTACT,
        happened_at=happened_at or stamped,
        channel=wanted,
        counterpart=None if _blank(counterpart) else counterpart.strip(),
        asked=None if _blank(asked) else asked.strip(),
        body=None if _blank(body) else body.strip(),
        created_at=stamped,
        updated_at=stamped,
    )
    db.session.add(entry)
    db.session.commit()
    return entry


# The kinds a person may edit or delete. `verdict` is not among them: those
# entries are the history of the decision, written by `set_review` in the same
# transaction as the columns, and letting the note controls reach them would
# let the log be edited into disagreement with the state it describes.
EDITABLE_KINDS: Tuple[str, ...] = (KIND_NOTE, KIND_CONTACT)


def edit_entry(entry: Any, **fields: Any) -> Any:
    """Change what an entry says. Notes and contacts only."""
    from app import db

    if entry.kind not in EDITABLE_KINDS:
        raise ReviewError("a verdict entry is the record of a decision, not a note")

    if "body" in fields:
        body = fields["body"]
        if entry.kind == KIND_NOTE and _blank(body):
            raise ReviewError("a note is its text")
        entry.body = None if _blank(body) else body.strip()
    if "asked" in fields and entry.kind == KIND_CONTACT:
        entry.asked = None if _blank(fields["asked"]) else fields["asked"].strip()
    if "counterpart" in fields and entry.kind == KIND_CONTACT:
        entry.counterpart = (
            None if _blank(fields["counterpart"]) else fields["counterpart"].strip()
        )
    if "channel" in fields and entry.kind == KIND_CONTACT:
        wanted = (fields["channel"] or "").strip().lower()
        if wanted not in CHANNELS:
            raise ReviewError(f"unknown channel: {fields['channel']!r}")
        entry.channel = wanted
    if "happened_at" in fields and fields["happened_at"]:
        entry.happened_at = fields["happened_at"]

    if entry.kind == KIND_CONTACT and (
        _blank(entry.counterpart) and _blank(entry.asked) and _blank(entry.body)
    ):
        raise ReviewError("a contact entry needs who was spoken to, or what was said")

    entry.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    return entry


def soft_delete_entry(entry: Any) -> Any:
    """Take an entry off the feed without destroying it.

    Everything else this application holds can be recomputed -- a drive time,
    a score, a parcel outline. A sentence somebody typed cannot, so a mis-tap
    must not be the end of it, and `deleted_at` is one nullable column against
    that.
    """
    from app import db

    if entry.kind not in EDITABLE_KINDS:
        raise ReviewError("a verdict entry is the record of a decision, not a note")

    entry.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    return entry


def was_edited(entry: Any) -> bool:
    """Whether to show the "(edited)" marker, the Slack/GitHub convention.

    An exact comparison, which is only honest because both writers above stamp
    the two columns from one value. A tolerance was tried first and is the
    wrong shape: it has to be wide enough to swallow the microseconds between
    two column defaults and narrow enough to notice a typo corrected three
    seconds later, and no number is both.
    """
    created = getattr(entry, "created_at", None)
    updated = getattr(entry, "updated_at", None)
    if not created or not updated:
        return False
    return updated != created
