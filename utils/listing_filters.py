"""The filters a route read, recorded as it reads them.

Every surface over `properties` — the list, the map, the CSV export — narrows
the same table with the same vocabulary, and each of them also has to *hand
that vocabulary on*: to its own pagination and sort links, to the other
surface, to the export, to a recovery link. Until now every one of those was a
hand-written list of parameter names, and the lists went stale one at a time,
each time a filter was added.

That is not a hypothetical failure mode, it is this repository's most frequent
defect, and 2026-08-20 is the day it stopped being deniable:

* `source` and `advertiser` (#391/#392) were missing from `base_args`, so
  `?advertiser=owner` found 70 listings and its own Next link found 470 (#435);
  from the export's `form_submitted` list (#439); and from
  `_map_focus_link`'s `dropped` set, where "Clear the filters and show it"
  re-issued the filter that hid the listing (#445).
* `measured` (#377–#380) was missing from the export, which showed 72 rows and
  exported 471 (#439), and from the map, which plotted 470 (#445).
* `verdict` and `action` (#430) reached `base_args` in the morning and were
  missing from `list_view_args` — written that same afternoon, by the session
  fixing the previous instance — within the hour.

Six filters, five stale copies, one day. Naming the missing parameters is the
fix that has now failed four times, so this module removes the naming.

**A link is built from the record of what the route read, not from a list
somebody maintains.** `FilterArgs` wraps `request.args`, and every filter the
route takes from it is remembered; `link_args()` gives them back. A filter
added to a route therefore rides that route's links the moment it is read, and
one that the route does *not* read cannot appear in them — which matters as
much, because a link carrying a filter its origin never applied sends the
reader somewhere narrower than the page they left (#98's shape, and the reason
`/map`'s List View link deliberately omitted `measured` while `/map` ignored
it).

What this module deliberately does not do is *apply* the filters. Each one has
its own clause and its own module — `utils/listing_source.py`,
`services/advertiser.py`, `utils/municipality_grouping.py`,
`utils/listing_search.py`, `services/owner_review.py` — and a dispatch table
here would be a second home for readings that already have one. The
recording is the part that kept going stale; the applying never did.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

# Parameters that are never filters: they say which subscription, which one
# listing, how to draw the result, or which page of it. They are handled
# explicitly by whoever builds a link, because each has its own rule -- a sort
# header sets its own `sort`, pagination its own `page`, and `profile_id` is
# replaced rather than carried.
#
# `rebuilt_from(..., keep=NON_FILTERS - {"profile_id"})` is how a caller clears
# every filter without naming one. Note which way this list fails, because it is
# the opposite of the lists it replaces: forgetting to add a *filter* here does
# nothing at all (it is not a filter, so it is dropped, which is correct), and
# forgetting a new *view* parameter costs a dropped view preference on one
# recovery link. Neither can produce a wrong row set, which every stale list
# above did.
NON_FILTERS: frozenset[str] = frozenset(
    {
        "profile_id",
        "focus",
        "sort",
        "order",
        "page",
        "per_page",
        "mode",
        "view_type",
        "endpoint",
    }
)


# The exception that sentence above did not survive, and the reason it needs
# naming rather than fixing quietly: **`criteria` filters when it is absent.**
# Every other filter here is off when it is not in the URL, so dropping it
# clears it. `criteria` is a four-state view of a verdict
# (`services/subscription_criteria.py`) whose unset state is *hide the
# measured fails* -- so a link that drops it re-issues exactly the hide it
# meant to lift, which is #445's defect in the one parameter the inverted
# list cannot see.
#
# Measured on production 2026-08-31: `/map?focus=1457` said the listing was
# hidden by the filters and offered "Clear the filters and show it"; the
# cleared link rendered the identical notice with the identical link, a loop
# with no exit, because 1457 is a measured criteria fail.
#
# So clearing is "keep the non-filters, AND state the cleared value for any
# filter whose absence is not its off position". A future filter of the same
# shape belongs here; one of the ordinary shape needs nothing.
CLEARED_NOT_ABSENT: dict[str, str] = {"criteria": "all"}


class FilterArgs:
    """`request.args`, remembering which filters were taken from it.

    Read a filter with `get` (text) or `flag` (an on/off switch), exactly as
    the routes did with `request.args.get`, and the value comes back
    unchanged. `link_args()` then returns what was read, shaped for
    `url_for`: empty values become `None` so they are omitted rather than
    sent as `?category=`, which would claim a filter that is not applied.
    """

    def __init__(self, args: Mapping[str, Any]):
        self._args = args
        self._read: Dict[str, Any] = {}

    def get(self, name: str, default: str = "") -> str:
        value = self._args.get(name, default)
        self._read[name] = value
        return value

    def flag(self, name: str, on: str = "on") -> bool:
        value = self._args.get(name, "") == on
        # Recorded as the string a link has to carry, not as the boolean the
        # route works with: `url_for` needs `favorites=on`, and `False` would
        # travel as the string "False" and read as truthy at the far end.
        self._read[name] = on if value else None
        return value

    def link_args(self) -> Dict[str, Any]:
        """The filters that were read, ready to hand to `url_for`."""
        return {name: (value or None) for name, value in self._read.items()}

    def names_read(self) -> frozenset[str]:
        return frozenset(self._read)


def rebuilt_from(
    args: Mapping[str, Any],
    *,
    drop: Iterable[str] = (),
    keep: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """Rebuild a query string from `args`, for a link that carries state on.

    `drop` names keys to remove. `keep`, when given, names the **only** keys to
    retain -- which is how a caller clears every filter without naming one:
    `_map_focus_link(keep_filters=False)` keeps `focus` and nothing else, so a
    filter added tomorrow is unknown to it and therefore dropped, which is the
    correct answer and the reason that helper's promise ("Clearing them is
    guaranteed to work") failed for `source`, `advertiser`, `verdict` and
    `action` until #445.

    `endpoint` and `_`-prefixed keys never travel: `url_for` reads them as its
    own arguments, so a query string carrying one would raise instead.
    """
    dropped = set(drop)
    kept = None if keep is None else set(keep)
    return {
        key: (values[0] if len(values) == 1 else values)
        for key, values in _lists(args)
        if key not in dropped
        and (kept is None or key in kept)
        and key != "endpoint"
        and not key.startswith("_")
    }


def _lists(args: Mapping[str, Any]):
    """`request.args.lists()` where available, so repeated keys survive."""
    lists = getattr(args, "lists", None)
    if callable(lists):
        return lists()
    return [(key, [value]) for key, value in args.items()]
