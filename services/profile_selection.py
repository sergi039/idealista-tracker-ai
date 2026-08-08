"""Which subscriptions the properties views are showing (issue #104).

The owner wants to see every subscription at once *or* tick several of them
in one dropdown, without the filter bar growing a second control. That turns
`profile_id` from a single value into a repeated query parameter, and the
three answers it can encode are modelled here explicitly:

``auto``
    The parameter is absent (an old bookmark, a bare ``/properties``). The
    caller applies its own fallback -- ``/properties`` picks the richest
    active profile, ``/map`` the one with the most mappable rows -- and the
    resolved id is then pinned into every link so the choice stops drifting.

``all``
    ``profile_id=all`` or ``profile_id=`` -- every **active** profile. Not
    "no filter": the dropdown lists active profiles only, so a retired
    subscription's listings would be rows the user has no way to ask for and
    no way to get rid of.

``selected(ids)``
    One or more explicit ids: ``profile_id=6&profile_id=8``. Ids reach
    inactive profiles too -- that is the only way to see one.

Two decisions worth knowing before changing anything here:

* **"all" is never inferred from an emptiness.** The filter form always posts
  an explicit ``profile_id=all`` alongside whatever is ticked, because a form
  with nothing ticked submits no ``profile_id`` at all, and *that* is
  indistinguishable from an old link. Explicit ids therefore win over the
  ``all`` token when both arrive, which is also what makes the dropdown work
  with JavaScript switched off.
* **A value that cannot name a real profile still counts as a selection.**
  ``profile_id=999999`` (no such row), ``0``, ``-1`` and integers past the
  32-bit column are answered with an empty page rather than a fallback,
  because falling back would quietly answer a different question and look
  like a working filter. Only genuinely unparseable text (``profile_id=abc``)
  is ignored, matching what ``request.args.get(..., type=int)`` used to do.

`SearchProfileService.parse_profile_selection` is the older single-value
parser and stays where it is as a compatibility surface; it is collapsed into
this module by the integrator once issue #102 has landed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional, Sequence, Tuple, Union

# Accepted in `profile_id` to mean "every active profile". An empty string
# means the same thing and is what a bare `profile_id=` submits.
PROFILE_ALL_SENTINEL = "all"

QUERY_PARAM = "profile_id"

# `SearchProfile.id` is a 32-bit `db.Integer`. Anything outside the range can
# never match a row, and handing it to PostgreSQL raises instead of returning
# nothing, so it is dropped from the id set (while still counting as an
# explicit selection -- see the module docstring).
MIN_PROFILE_ID = 1
MAX_PROFILE_ID = 2**31 - 1

# Serialised back into links when a selection named only impossible ids, so
# the empty result stays put instead of sliding back to `auto` on the next
# click. `0` is below MIN_PROFILE_ID, so it re-parses to the same state.
IMPOSSIBLE_PROFILE_ID = "0"

# A token that looks like an integer *is* an id claim even when Python
# refuses to convert it: `int()` raises on decimal strings past its 4300-digit
# limit, and treating that as unparseable text would fall back to `auto` --
# the one outcome the module promises never to produce for a numeric input.
_NUMERIC_TOKEN = re.compile(r"[+-]?\d+")

# Ids past this many are dropped. Nothing legitimate selects more profiles
# than exist, and the parsed list goes straight into a SQL `IN (...)`.
MAX_SELECTED_PROFILE_IDS = 50

# Shown wherever a multi-profile view has to withhold profile-specific travel
# data. Lives here so the two templates and the tests cannot drift apart.
TRAVEL_NOTICE = "Select one profile to view or recalculate profile-specific travel data"

LinkValue = Union[int, str]


class ProfileSelectionState(str, Enum):
    AUTO = "auto"
    ALL = "all"
    SELECTED = "selected"


@dataclass(frozen=True)
class ProfileSelection:
    """What the request asked for, before any database lookup."""

    state: ProfileSelectionState
    ids: Tuple[int, ...] = ()

    @property
    def is_auto(self) -> bool:
        return self.state is ProfileSelectionState.AUTO

    @property
    def is_all(self) -> bool:
        return self.state is ProfileSelectionState.ALL

    @property
    def is_selected(self) -> bool:
        return self.state is ProfileSelectionState.SELECTED


@dataclass(frozen=True)
class ResolvedProfileSelection:
    """What the request means once the active profiles are known."""

    state: ProfileSelectionState

    #: Ids to filter `Property.search_profile_id` on. `None` means "apply no
    #: filter at all", which is not the same as `()` -- an empty tuple is an
    #: explicit selection that matches nothing.
    filter_ids: Optional[Tuple[int, ...]]

    #: What every `url_for` on the page must pass back as `profile_id`. A
    #: tuple is serialised by Werkzeug as a repeated parameter.
    link_values: Tuple[LinkValue, ...]

    #: Which dropdown checkboxes are ticked.
    checked_ids: Tuple[int, ...]

    #: The one profile in play, or `None` when the view spans several (or
    #: none). Profile-specific travel targets and the recalculate actions are
    #: only meaningful when this is set: custom target ids belong to a single
    #: profile, so a union of two profiles' targets would label a column with
    #: a destination the row was never measured against.
    single_id: Optional[int]

    @property
    def spans_several_profiles(self) -> bool:
        return self.single_id is None and self.filter_ids != ()

    @property
    def travel_notice(self) -> str:
        return TRAVEL_NOTICE

    @property
    def label(self) -> str:
        """Text on the dropdown toggle: `All profiles` or `N selected`.

        Keyed off the state, not off the tick count: a selection of only
        impossible ids ticks nothing but shows nothing either, and labelling
        that "All profiles" would describe the opposite of what is on screen.
        """
        if self.state is ProfileSelectionState.SELECTED:
            return f"{len(self.checked_ids)} selected"
        if not self.checked_ids:
            return "All profiles"
        return f"{len(self.checked_ids)} selected"


def _dedupe(values: Iterable[int]) -> Tuple[int, ...]:
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _raw_values(args: Any) -> Optional[list]:
    """Every `profile_id` value in the request, or None when absent.

    Accepts anything request-args shaped: a Werkzeug `MultiDict` (repeated
    parameters via `getlist`) or a plain dict, which the older single-value
    call sites pass in.
    """
    if QUERY_PARAM not in args:
        return None

    getlist = getattr(args, "getlist", None)
    if callable(getlist):
        return list(getlist(QUERY_PARAM))

    value = args.get(QUERY_PARAM)
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def parse_profile_selection(args: Any) -> ProfileSelection:
    """Read `profile_id` out of a request's args into an explicit state."""
    raw_values = _raw_values(args)
    if raw_values is None:
        return ProfileSelection(ProfileSelectionState.AUTO)

    saw_all = False
    saw_number = False
    ids: list[int] = []

    for raw in raw_values:
        token = str(raw if raw is not None else "").strip()
        if token == "" or token.lower() == PROFILE_ALL_SENTINEL:
            saw_all = True
            continue
        if not _NUMERIC_TOKEN.fullmatch(token):
            # Unparseable text behaves like the old `type=int` coercion:
            # ignored, so `?profile_id=abc` keeps falling back to auto.
            continue
        saw_number = True
        try:
            number = int(token)
        except ValueError:
            # Numeric but longer than `int()` will convert: an id claim that
            # cannot possibly match, handled like `0` rather than like text.
            continue
        if MIN_PROFILE_ID <= number <= MAX_PROFILE_ID:
            ids.append(number)

    if saw_number:
        # Ticked boxes beat the form's `all` fallback; see the module
        # docstring for why the fallback is posted at all.
        return ProfileSelection(
            ProfileSelectionState.SELECTED, _dedupe(ids)[:MAX_SELECTED_PROFILE_IDS]
        )
    if saw_all:
        return ProfileSelection(ProfileSelectionState.ALL)
    return ProfileSelection(ProfileSelectionState.AUTO)


def resolve_profile_selection(
    selection: ProfileSelection,
    active_profile_ids: Sequence[int],
    auto_profile_id: Optional[int] = None,
) -> ResolvedProfileSelection:
    """Turn a parsed selection into a filter, links and dropdown state.

    `active_profile_ids` are the profiles the dropdown offers, in the order it
    offers them. `auto_profile_id` is the caller's own fallback for the `auto`
    state -- each view resolves it differently, and passing `None` (nothing to
    fall back to) leaves the query unfiltered, which is what a fresh install
    with no profiles at all needs.
    """
    active = _dedupe(int(profile_id) for profile_id in active_profile_ids)

    if selection.is_all:
        filter_ids: Optional[Tuple[int, ...]] = active
        link_values: Tuple[LinkValue, ...] = (PROFILE_ALL_SENTINEL,)
        # "All profiles" is the whole point of the state, so no box is ticked
        # even though the filter names every active profile.
        checked: Tuple[int, ...] = ()
    elif selection.is_selected:
        filter_ids = selection.ids
        link_values = tuple(selection.ids)
        checked = selection.ids
        if not selection.ids:
            # A selection that named only impossible ids: keep it explicit so
            # the links do not slide back to `auto` on the next click.
            link_values = (IMPOSSIBLE_PROFILE_ID,)
    elif auto_profile_id is None:
        filter_ids = None
        link_values = ()
        checked = ()
    else:
        filter_ids = (int(auto_profile_id),)
        link_values = (int(auto_profile_id),)
        checked = (int(auto_profile_id),)

    single_id = (
        filter_ids[0] if filter_ids is not None and len(filter_ids) == 1 else None
    )

    return ResolvedProfileSelection(
        state=selection.state,
        filter_ids=filter_ids,
        link_values=link_values,
        checked_ids=checked,
        single_id=single_id,
    )


def empty_profile_selection() -> ResolvedProfileSelection:
    """A neutral resolution for the error-fallback renders.

    The templates read the selection unconditionally, so a page that failed
    before it could resolve anything still needs a well-formed value rather
    than `None` guards scattered through the markup.
    """
    return resolve_profile_selection(ProfileSelection(ProfileSelectionState.AUTO), ())


def apply_profile_filter(query, column, resolved: ResolvedProfileSelection):
    """Narrow `query` to the resolved selection, if it narrows anything.

    Kept next to the state model so `/properties`, `/properties/export.csv`
    and `/map` cannot drift into three slightly different filters.
    """
    if resolved.filter_ids is None:
        return query
    if len(resolved.filter_ids) == 1:
        return query.filter(column == resolved.filter_ids[0])
    return query.filter(column.in_(resolved.filter_ids))
