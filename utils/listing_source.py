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

# The exact hosts these sites serve listings on.
#
# An explicit list, not a suffix rule, and that is the whole point. The SQL
# clause below and `source_of_url` must be *the same* reading -- a badge saying
# "Fotocasa" over a row the fotocasa filter excludes is the defect this module
# was written to remove, not one to reintroduce in its own implementation. A
# LIKE pattern cannot express "one more subdomain label, but not a slash":
# `%.fotocasa.es/%` also matches `https://evil.example/x.fotocasa.es/y`. So
# neither side does suffix matching. A host that turns up and is not on this
# list reads as OTHER on *both* sides, which is an answer they agree on.
#
# Measured against production on 2026-08-17: every one of the 732 stored URLs
# is on `www.idealista.com` or `www.fotocasa.es`, bar one agency site. The
# bare-domain spellings are here because a hand-typed link may omit the `www.`.
_HOSTS: Dict[str, str] = {
    "idealista.com": IDEALISTA,
    "www.idealista.com": IDEALISTA,
    "idealista.it": IDEALISTA,
    "www.idealista.it": IDEALISTA,
    "idealista.pt": IDEALISTA,
    "www.idealista.pt": IDEALISTA,
    "fotocasa.es": FOTOCASA,
    "www.fotocasa.es": FOTOCASA,
    "m.fotocasa.es": FOTOCASA,
}

# The schemes a stored URL can start with. The host sits immediately after one
# of these, at position zero -- which is what lets the clause below anchor
# instead of searching, and is why a host named inside a query string cannot
# match any more.
_SCHEMES: Tuple[str, ...] = ("http", "https")

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
    return _HOSTS.get(host, OTHER)


def source_of(record: Any) -> str:
    """The source of a listing row."""
    return source_of_url(getattr(record, "url", None))


def _host_clause(model: Any, host: str):
    """`url` is served by this exact host.

    Anchored at the start of the value, never searched inside it. The previous
    version matched `%//{host}/%` anywhere in the column, which a URL carrying
    another site's link in a query parameter satisfies:
    `https://example.com/x?to=http://idealista.com/y` contains `//idealista.com/`
    and was pulled into the Idealista filter, while `source_of_url` -- reading
    the same row for the badge beside it -- correctly answered `other`.

    The four terms are the four things that may follow a host: a path, a query,
    a fragment, or the end of the string. `source_of_url` accepts all four
    because `urlsplit` does, so the clause has to as well, or a path-less URL
    would be badged one way and filtered the other.
    """
    column = func.lower(model.url)
    terms = []
    for scheme in _SCHEMES:
        prefix = f"{scheme}://{host}"
        terms.extend(
            (
                column.like(f"{prefix}/%"),
                column.like(f"{prefix}?%"),
                column.like(f"{prefix}#%"),
                column.like(prefix),
            )
        )
    return or_(*terms)


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

    known = [_host_clause(model, host) for host in _HOSTS]

    if wanted == OTHER:
        return and_(has_url, not_(or_(*known)))

    hosts = [host for host, source_name in _HOSTS.items() if source_name == wanted]
    return and_(has_url, or_(*[_host_clause(model, host) for host in hosts]))


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
