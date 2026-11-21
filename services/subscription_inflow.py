"""How many listings each subscription brought in, by period, and how many of
those the owner cared about.

`/profiles` printed one number per subscription -- everything it ever stored --
which cannot answer the question the owner asks of a saved search (2026-11-14):
is this feed still producing, and is what it produces any good? A search that
delivered 300 listings a year ago and nothing since reads the same as one that
delivered 300 this month, and neither says whether a single one was worth
opening.

So the page counts, per subscription and for the listings with no subscription:

* **added** in each of the last `PERIOD_DAYS` calendar days and in all time,
  by `created_at` -- the date the list prints under "Added" and the
  `added_from` filter cuts on, through that filter's own clause
  (`listing_attribute_filters.added_on_or_after`), so a counted period and the
  page its link opens are one reading;
* **of interest** among them: a favorite, or a listing the owner marked
  Interested (`owner_review.INTERESTED`). Both are the owner's own acts and
  nothing else is read as interest -- not a score, not a recommendation, and
  not `waiting`, which says a question is open rather than that the answer is
  yes.

A period is counted in the calendar the filter uses (UTC days, see that
module), and `today` is taken once per request and handed in, so every cell and
every link on the page describe the same windows.

Every stored row is counted, withdrawn and rejected ones included: this is a
measurement of what a feed delivered, not of what the list currently shows. A
row with no `created_at` is in "all time" and in no period -- an unknown date
is not recent (#98).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case, func, or_

from services import owner_review
from services.listing_attribute_filters import added_on_or_after

# The windows the page offers, in calendar days including today: a week, a
# month and a quarter of a feed's recent output beside its whole history.
PERIOD_DAYS = (7, 30, 90)


def utc_today() -> date:
    """Today in the calendar `created_at` is stored and filtered in."""
    return datetime.now(timezone.utc).date()


def periods(today: date) -> List[Dict[str, Any]]:
    """Each window as `{"days": n, "start": first day}`, shortest first.

    `start` is the value the `added_from` link carries: the last `n` calendar
    days including `today`.
    """
    return [
        {"days": days, "start": today - timedelta(days=days - 1)}
        for days in PERIOD_DAYS
    ]


def interest_clause(model: Any):
    """A favorite, or marked Interested. Definite, never NULL."""
    return or_(
        model.is_favorite.is_(True),
        owner_review.decision_expression(model) == owner_review.INTERESTED,
    )


def _count(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def read(session, model: Any, today: date) -> Dict[Optional[int], Dict[Any, Any]]:
    """Counts per subscription id (`None` = no subscription), in one query.

    `{profile_id: {"total": {"added": n, "interest": k},
                   7: {"added": n, "interest": k}, 30: ..., 90: ...}}`.
    A subscription holding no listing has no entry.
    """
    interest = interest_clause(model)
    windows = periods(today)
    columns = [
        model.search_profile_id,
        func.count(model.id),
        _count(interest),
    ]
    for window in windows:
        added = added_on_or_after(model, window["start"])
        columns.append(_count(added))
        columns.append(_count(and_(added, interest)))

    result: Dict[Optional[int], Dict[Any, Any]] = {}
    for row in session.query(*columns).group_by(model.search_profile_id):
        entry: Dict[Any, Any] = {
            "total": {"added": int(row[1]), "interest": int(row[2])}
        }
        for position, window in enumerate(windows):
            entry[window["days"]] = {
                "added": int(row[3 + 2 * position]),
                "interest": int(row[4 + 2 * position]),
            }
        result[row[0]] = entry
    return result
