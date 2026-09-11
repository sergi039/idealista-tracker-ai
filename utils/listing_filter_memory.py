"""The last filter state of `/properties`, kept across navigation.

Every filter on the page lives in its query string and nowhere else, so every
road back onto the page that carries no query string -- the navbar entry, the
"back to properties" button on a listing, the redirect after a POST such as the
taste retrain -- opened the page on its defaults. Measured on production on
2026-09-11: housing / house / Recommendations, applied through the form, were
gone after one visit to a listing and back. The owner's words: the filters must
be remembered, not reset on every navigation.

The memory is the Flask session cookie -- signed, per browser, thirty days
(`permanent_session_lifetime`), already carrying the language and the pool
weight's pending config -- so a state chosen on the laptop does not surprise
the phone, and nothing is stored on the server. That is also why this is not a
"saved views" feature: there is one memory, it is the last thing the owner did
on the page, and it needs no control of its own.

What is remembered is decided by provenance, not by content, for the reason
`utils/listing_status_scope.py` gives: the page's own form and every link it
draws carry `mode` and `view_type`, and nothing else does. A request carrying
them is the owner *choosing* on the page and is remembered verbatim -- filters,
subscription selection, sort, mode, view -- minus the page number, which is
where the reader was rather than what they asked for. A request carrying
neither (a cross-page link from `/profiles` or `/map`, a hand-typed URL, the
`/municipalities` drill-downs) is a visit and changes nothing. A link that is
one of the page's own but names one row rather than a standing state -- the
reveal link a listing page draws -- says `remember=off` and is shown without
being kept.

A bare `/properties` recalls the memory by redirecting to it, so the address
bar says what the page shows and every link on the page is built the way it
always was. Nothing recalls without a session: the deploy's render check
(`DEPLOY_RENDER_PATH`) curls the bare page cookie-less and still gets the 200
it demands, and a fresh browser gets the same defaults as before.

Clearing is therefore its own request. The Clear button used to lead to the
bare page, which now leads back to the memory; it says `remember=forget`, which
drops the memory and redirects to the bare page, which then renders the
defaults. The narrowing note's "clear filters" link is unchanged: it keeps the
view state, and it is itself remembered, as a cleared state.

Bounded at the boundary: the cookie has 4 KB for everything, so a state that
does not fit is forgotten rather than half-kept -- a memory that returns to a
state the owner never chose is worse than none -- and what comes back out of
the cookie is re-parsed and re-encoded before it becomes a redirect, so the
target is always this page with a well-formed query string, and a memory that
no longer carries the form's markers is dropped rather than followed into a
redirect loop.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping
from urllib.parse import parse_qsl, urlencode

from utils.listing_status_scope import submitted_by_the_filter_form

SESSION_KEY = "properties_filters"

# The one parameter this module reads. `off`: show this request, keep the
# memory as it was. `forget`: drop the memory and land on the bare page.
REMEMBER_PARAM = "remember"
REMEMBER_OFF = "off"
REMEMBER_FORGET = "forget"

# Never stored: the page number is a position, and the parameter above is an
# instruction to this module, not a state of the page.
NOT_REMEMBERED = frozenset({"page", REMEMBER_PARAM})

# The signed session cookie holds everything the app keeps per browser in
# about 4 KB; a state past this is forgotten, never truncated.
MAX_STORED_CHARS = 2048


def redirect_query(args: Mapping[str, Any], session: MutableMapping[str, Any]):
    """What the `/properties` request does about the memory.

    Returns `None` when the request renders as it is (and is remembered when
    it is the page's own), a query string when the request should redirect to
    `/properties?<that>`, and `""` when it should redirect to the bare page.
    """
    if args.get(REMEMBER_PARAM) == REMEMBER_FORGET:
        forget(session)
        return ""
    if not args:
        return recall(session)
    if args.get(REMEMBER_PARAM) == REMEMBER_OFF:
        return None
    if submitted_by_the_filter_form(args):
        remember(session, args)
    return None


def remember(session: MutableMapping[str, Any], args: Mapping[str, Any]) -> None:
    """Store this request's query as the memory, page number dropped.

    Empty values are dropped too: an empty select is what the form submits for
    "no filter", and an empty value is read as absent everywhere in these
    routes. The cookie becomes permanent here, because a memory that ends
    with the browser window is the defect being fixed with a shorter fuse.
    """
    pairs = [
        (key, value)
        for key, value in _pairs(args)
        if key not in NOT_REMEMBERED and value != ""
    ]
    query = urlencode(pairs)
    if not query or len(query) > MAX_STORED_CHARS:
        forget(session)
        return
    session[SESSION_KEY] = query
    session.permanent = True


def recall(session: MutableMapping[str, Any]) -> str | None:
    """The remembered query string, or `None` -- and a memory that cannot be
    followed (not a string, too long, empty once re-encoded, or without the
    form's markers) is dropped on the way."""
    if SESSION_KEY not in session:
        return None
    stored = session[SESSION_KEY]
    if not isinstance(stored, str) or len(stored) > MAX_STORED_CHARS:
        forget(session)
        return None
    pairs = [
        (key, value)
        for key, value in parse_qsl(stored, keep_blank_values=False)
        if key not in NOT_REMEMBERED
    ]
    if not pairs or not submitted_by_the_filter_form(dict(pairs)):
        forget(session)
        return None
    return urlencode(pairs)


def forget(session: MutableMapping[str, Any]) -> None:
    session.pop(SESSION_KEY, None)


def _pairs(args: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`(key, value)` in request order, repeated keys kept (`profile_id`)."""
    lists = getattr(args, "lists", None)
    if callable(lists):
        return [(key, str(value)) for key, values in lists() for value in values]
    return [(key, str(value)) for key, value in args.items()]
