"""The owner's auto-enrich scope, in one place (rule of 2026-08-14).

Automatic enrichment passes cover listings from the last N days (default 30)
plus favorites; everything older is enriched manually, per property, from the
detail page. Both Phase-2 backfills select through here so the rule cannot
drift between them.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from sqlalchemy import or_

from models import Property

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
