"""What the listing search box accepts, in one place.

The search box used to read `title`, `description` and `municipality` and
nothing else, so the most natural way to look a listing up -- paste the link
you got in the alert email, or the one you are looking at on idealista.com --
returned "0 properties found" for a listing the table was holding all along.
Measured 2026-08-17 against the live database: pasting
``https://www.idealista.com/en/inmueble/91523456/`` found nothing, while
property 351 ("Land plot in Salamir, s/n Nn, Cudillero") carries exactly that
listing id.

That zero is the shape of #98 in a filter: an absence of a *match* rendered
as an absence of the *row*. The count says "0 properties found" with the same
face it uses for a search that really has no answer, and nothing on the page
suggests the query was understood differently from how it was typed.

Two ways in, because the table holds two kinds of URL:

* **The Idealista listing id.** `properties.url` stores the link as the email
  wrote it, with a `?utm_...` tail attached, and the language segment is
  whatever that email used -- so a plain substring match on a hand-copied
  ``/es/`` link misses a stored ``/en/`` one. `idealista_property_id` is the
  stable identity (it is what ingestion dedups on), so a query naming a
  listing id matches on the column, and on the URL as well for the rows whose
  id was never extracted.
* **The URL itself.** 57 of the 730 rows on that same date are not Idealista
  at all -- fotocasa.es listings and one agency's own site, entered by hand --
  and they have no listing id to name. For them the query is matched against
  `url` directly, after the tracking parameters are dropped, since the stored
  link carries a tail the pasted one does not.

The URL match fires only for a query that really looks like a link. Matching
every text search against `url` too would quietly widen ordinary searches: on
this table "terreno" would start matching every fotocasa URL by its path
segment, and a two-letter query like "en" would match every Idealista link
ever stored.

The four listing surfaces (/properties, /map, /properties/export.csv and the
JSON /api/properties) share this the way they already share
`municipality_filter_clause`, so they cannot drift into four different
answers to "find me this listing".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import or_

from utils.idealista_extractors import extract_idealista_property_id

# `idealista_property_id` is a bigint, so a 25-digit query names no listing.
# Refusing it here makes the answer independent of how the driver renders the
# comparison, which is not a formality: measured against this deployment's
# PostgreSQL on 2026-08-17, `= 9999999999999999999999999` (the untyped literal
# psycopg2 sends) returns no rows, while the same value bound to a `bigint`
# parameter -- what a prepared statement or an explicit cast produces -- fails
# outright with `ERROR: bigint out of range`. A typo in the search box must
# not be able to become a 500 the day a driver or a backend changes its mind.
_BIGINT_MAX = 9223372036854775807

# Backslash rather than the SQL default (no escape character at all), so a `_`
# in a pasted URL -- they are common in slugs -- matches itself instead of any
# character.
_LIKE_ESCAPE = "\\"

# A scheme, a `www.`, or a bare `host.tld/path`. The last form is what a
# copied address bar gives when the browser hides the scheme.
_URL_SHAPED = re.compile(
    r"^(?:[a-z][a-z0-9+.\-]*://|www\.)|^[a-z0-9\-]+(?:\.[a-z0-9\-]+)+/",
    re.IGNORECASE,
)

_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_LEADING_WWW = re.compile(r"^www\.", re.IGNORECASE)

# Parameters that identify the *delivery* of a link rather than the listing.
# Idealista's alert emails append ten of them, all `utm_*`; the rest are the
# usual ad-network click ids. Anything else is kept, because some sites carry
# the listing id in the query string itself
# ("detalle-inmuebles.php?id=2546", a real row here).
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "yclid",
}


def _escape_like(value: str) -> str:
    """Escape the LIKE wildcards so a pasted URL matches literally."""
    for char in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(char, _LIKE_ESCAPE + char)
    return value


def extract_listing_id(text: Optional[str]) -> Optional[int]:
    """The Idealista listing id a search box entry names, or None.

    Accepts a listing URL in any of its spellings (the `/inmueble/<id>/`
    segment is what identifies it) and a bare id typed on its own. A number
    too large to be a bigint is not one: see `_BIGINT_MAX`.
    """
    query = (text or "").strip()
    if not query:
        return None

    listing_id = extract_idealista_property_id(query)
    if listing_id is None and query.isdecimal():
        listing_id = int(query)

    if listing_id is None or listing_id <= 0 or listing_id > _BIGINT_MAX:
        return None
    return listing_id


def _is_url_shaped(text: str) -> bool:
    return bool(_URL_SHAPED.match(text))


def url_fragment(text: Optional[str]) -> Optional[str]:
    """The part of a pasted URL that a stored URL can be matched on.

    The scheme, a leading `www.` and a trailing slash are dropped because the
    two spellings of one link disagree about them, and the tracking
    parameters are dropped because the stored link has them and the pasted
    one does not. What is left is a substring of the stored URL whenever they
    name the same listing.

    None when the query is not shaped like a URL at all -- an ordinary text
    search must not start matching URLs.
    """
    query = (text or "").strip()
    if not query or not _is_url_shaped(query):
        return None

    query = query.split("#", 1)[0]
    path, _, raw_query = query.partition("?")

    path = _SCHEME.sub("", path)
    path = _LEADING_WWW.sub("", path)
    path = path.rstrip("/")

    kept = [
        (name, value)
        for name, value in parse_qsl(raw_query, keep_blank_values=True)
        if name.lower() not in _TRACKING_PARAMS
        and not name.lower().startswith(_TRACKING_PREFIXES)
    ]
    if kept:
        path = f"{path}?{urlencode(kept)}"

    return path or None


@dataclass(frozen=True)
class SearchInterpretation:
    """How one search box entry was read.

    The page needs this because "0 properties found" is the same sentence for
    a query that genuinely has no answer and for one that was understood
    differently from how it was typed. It is derived here, next to the clause
    and from the same two functions, so what the page says it searched for
    cannot drift from what was actually searched.
    """

    query: str
    listing_id: Optional[int]
    url_fragment: Optional[str]

    @property
    def is_listing_reference(self) -> bool:
        """True when the query named a listing rather than described one."""
        return self.listing_id is not None or self.url_fragment is not None


def interpret_search(search_query: Optional[str]) -> Optional[SearchInterpretation]:
    """Read a search box entry, or None when there is nothing to read."""
    query = (search_query or "").strip()
    if not query:
        return None
    return SearchInterpretation(
        query=query,
        listing_id=extract_listing_id(query),
        url_fragment=url_fragment(query),
    )


def listing_id_clause(model: Any, listing_id: int):
    """The rows carrying one Idealista listing id, by either of its two homes.

    `idealista_property_id` is the extracted identity and it is NULL wherever
    nothing extracted it. Measured against production on 2026-08-24: of the
    786 rows whose URL is an idealista listing, **48 carry no id in the
    column** -- every one of them written by `utils/import_research_sheet.py`,
    which stores the link and never the id. A lookup reading only the column
    is therefore blind to a question those rows can answer, which is #98 in a
    dedup: an absence of a *match* read as an absence of the *row*.

    The trailing slash is a boundary: without it `/inmueble/9152345` would
    also match the different listing `/inmueble/91523456/`. All 786 of those
    stored URLs carry it (measured the same day), and none carries the id in
    any other shape.

    This is the whole reading, so a caller that has a listing id asks here
    rather than half-remembering it -- which is exactly what
    `import_research_sheet._existing` did until 2026-08-25.
    """
    return or_(
        model.idealista_property_id == listing_id,
        model.url.ilike(f"%/inmueble/{listing_id}/%"),
    )


def listing_search_clause(model: Any, search_query: Optional[str]):
    """The listing search box, as one SQLAlchemy clause (None when empty).

    Always the three text columns the box has always read; additionally the
    listing id and the URL when the query names one. They are OR-ed, so a
    query that happens to look like both stays a superset of the text search
    it used to be -- this widens what can be found and narrows nothing.
    """
    read = interpret_search(search_query)
    if read is None:
        return None

    pattern = f"%{read.query}%"
    clauses: List[Any] = [
        model.title.ilike(pattern),
        model.description.ilike(pattern),
        model.municipality.ilike(pattern),
    ]

    if read.listing_id is not None:
        clauses.append(listing_id_clause(model, read.listing_id))

    if read.url_fragment:
        clauses.append(
            model.url.ilike(f"%{_escape_like(read.url_fragment)}%", escape=_LIKE_ESCAPE)
        )

    return or_(*clauses)
