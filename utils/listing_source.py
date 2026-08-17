"""Which site a listing came from, decided once.

`properties` has no column for it and does not need one. The URL already says:
measured against production on 2026-08-17, 675 of 732 rows are idealista.com,
56 are fotocasa.es and one is an agency's own site. What the table lacked was anywhere that reading
lived, so four surfaces would each have grown their own `ILIKE '%fotocasa%'`
-- and `services/listing_verification.py` grew something worse than that, a
label naming Idealista for every row regardless of where it came from.

Deriving the source rather than storing it is deliberate, and it is the same
decision `utils/municipality_grouping.py` records for municipality names: a
derived column has to be maintained by every writer, and the writer that
forgets hides rows from their own filter. The URL is written once, by whoever
creates the row, and cannot drift out of agreement with itself.

`OTHER` is a real answer, not a leftover. One row here is an agency's own
site, and a row with no URL at all -- possible, since `url` is nullable --
is `UNKNOWN`, which the filter offers and never silently folds into another
source.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy import and_, func, not_, or_

IDEALISTA = "idealista"
FOTOCASA = "fotocasa"
OTHER = "other"
UNKNOWN = "unknown"

# Host fragments that name a source. Matched on the parsed hostname, never on
# the whole URL: a path segment or a query parameter can carry any word, and
# `?ref=idealista` on somebody else's site is not an Idealista listing.
_HOSTS: Tuple[Tuple[str, str], ...] = (
    ("idealista.com", IDEALISTA),
    ("idealista.it", IDEALISTA),
    ("idealista.pt", IDEALISTA),
    ("fotocasa.es", FOTOCASA),
)

# The order the filter offers them in, and the only values it accepts.
SOURCES: Tuple[str, ...] = (IDEALISTA, FOTOCASA, OTHER, UNKNOWN)

_LABELS: Dict[str, str] = {
    IDEALISTA: "Idealista",
    FOTOCASA: "Fotocasa",
    OTHER: "Other site",
    UNKNOWN: "No link",
}


def source_label(source: Optional[str]) -> str:
    """What to print on a badge. Never a bare slug."""
    return _LABELS.get((source or "").strip().lower(), _LABELS[UNKNOWN])


def source_of_url(url: Optional[str]) -> str:
    """The source a stored URL names."""
    raw = (url or "").strip()
    if not raw:
        return UNKNOWN
    if "//" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return OTHER
    if not host:
        return OTHER
    for fragment, source in _HOSTS:
        if host == fragment or host.endswith("." + fragment):
            return source
    return OTHER


def source_of(record: Any) -> str:
    """The source of a listing row."""
    return source_of_url(getattr(record, "url", None))


def _host_clause(model: Any, fragment: str):
    """`url` names this host.

    Anchored on `://` and on the host's own boundary so a path or a query
    parameter mentioning the name cannot match: `//fotocasa.es/` and
    `.fotocasa.es/` are hosts, `?from=fotocasa.es` is not.
    """
    column = func.lower(model.url)
    return or_(
        column.like(f"%//{fragment}/%"),
        column.like(f"%.{fragment}/%"),
    )


def source_filter_clause(model: Any, source: Optional[str]):
    """The source filter, as one SQLAlchemy clause (None when unset).

    Shared by the four listing surfaces the way they already share
    `municipality_filter_clause` and `listing_search_clause`, so they cannot
    drift into four different answers to "show me the fotocasa ones".
    """
    wanted = (source or "").strip().lower()
    if wanted not in SOURCES:
        return None

    has_url = and_(model.url.isnot(None), func.length(func.trim(model.url)) > 0)

    if wanted == UNKNOWN:
        return not_(has_url)

    known = [_host_clause(model, fragment) for fragment, _ in _HOSTS]

    if wanted == OTHER:
        return and_(has_url, not_(or_(*known)))

    fragments = [fragment for fragment, source_name in _HOSTS if source_name == wanted]
    return and_(has_url, or_(*[_host_clause(model, f) for f in fragments]))


def source_options(counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """The sources to offer, with their counts.

    A source holding nothing is not offered -- the same rule the subscription
    dropdown follows for an empty profile. The point is a filter whose every
    option leads somewhere.
    """
    return [
        {"value": source, "label": source_label(source), "count": counts.get(source, 0)}
        for source in SOURCES
        if counts.get(source, 0) > 0
    ]
