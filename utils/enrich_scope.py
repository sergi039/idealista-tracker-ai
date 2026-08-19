"""The owner's auto-enrich scope, in one place (rule of 2026-08-14).

Automatic enrichment passes cover listings from the last N days (default 30)
plus favorites; everything older is enriched manually, per property, from the
detail page. Both Phase-2 backfills select through here so the rule cannot
drift between them.

The scope is profile-agnostic on purpose and stays that way (decision #410): a
hidden subscription keeps ingesting, and showing it again must not reveal
holes where its listings were skipped. What it owed its operator until
UNIVERSE-001 is a **say-so before it spends** -- the shared 30-day-or-favorite
window selected 572 rows on 2026-08-19, 112 of them from retired
subscriptions, and `utils/recalc_property_travel.py` covers all 769 located
rows including 307 retired, at about $0.36 a listing. A count alone cannot
tell an operator that two fifths of what they are about to pay for belongs to
saved searches that stopped in February. `log_scope` is that disclosure, and
it lives here rather than in each CLI so the nine of them cannot describe the
same queue nine ways.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, List, Optional, Sequence

from sqlalchemy import or_

from models import Property
from services.population import (
    BASIS_NOT_DERIVED,
    Population,
    listings_by_profile,
    subscription_mix,
)

DEFAULT_AUTO_ENRICH_DAYS = 30


def scoped_properties(
    days: int = DEFAULT_AUTO_ENRICH_DAYS,
    include_all: bool = False,
    needs: Optional[Callable[[Property], bool]] = None,
) -> List[Property]:
    """Properties with coordinates inside the auto-enrich scope.

    `needs` filters to rows the specific backfill still has work for —
    rows it already answered leave the scope, which is what makes reruns
    resumable. `include_all` drops the window (a wider run needs its own
    ticket per CLAUDE.md when the pass costs money).
    """
    query = Property.query.filter(
        Property.location_lat.isnot(None),
        Property.location_lon.isnot(None),
    )
    if not include_all:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        query = query.filter(
            or_(Property.created_at >= cutoff, Property.is_favorite.is_(True))
        )
    rows = query.order_by(Property.id.asc()).all()
    if needs is None:
        return rows
    return [prop for prop in rows if needs(prop)]


def window_note(days: int, include_all: bool) -> str:
    """The phrase describing which rows `scoped_properties` actually selected.

    One home, because the two branches were written out per CLI and one of
    them was written out wrong: `utils/backfill_pool.py` and
    `utils/backfill_quality_of_life.py` announced "last N days or a favorite"
    on an `--all` run, which drops that window entirely. A disclosure
    describing a population the run did not use is the defect UNIVERSE-001
    exists to remove, reproduced inside its own first consumers -- and it is
    what a per-caller copy of a two-branch rule always eventually does.
    """
    if include_all:
        return "every located row (--all: the recent-or-favorite window is off)"
    return f"auto-enrich window: last {days} days or a favorite, located rows only"


def scope_population(
    rows: Sequence[Any], *, label: str, notes: Iterable[str] = ()
) -> Population:
    """Name the work queue `rows` is, before anything acts on it.

    Built from the rows the caller actually selected, never from a query of
    its own -- the same rule the municipality drill-down follows (#417), and
    for the same reason: two selections of "the same" set are how a
    disclosure comes to describe a run that did not happen.
    """
    return Population(
        label=label,
        total=len(rows),
        returned=len(rows),
        basis=BASIS_NOT_DERIVED,
        subscriptions=subscription_mix(listings_by_profile(rows)),
        notes=tuple(notes),
    )


def log_scope(
    logger: logging.Logger,
    rows: Sequence[Any],
    *,
    label: str,
    notes: Iterable[str] = (),
) -> Population:
    """Log what a run is about to cover, and hand the description back.

    One call per entry point, above the work and above any `--dry-run` exit,
    so a report and a real run describe the same queue.
    """
    population = scope_population(rows, label=label, notes=notes)
    for line in population.as_lines():
        logger.info("%s", line)
    return population
