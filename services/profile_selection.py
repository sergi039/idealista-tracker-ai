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
    inactive profiles too -- that is the only way to see one. The same state
    carries ``profile_id=unassigned``, which selects listings with no
    ``search_profile_id`` at all; it sits *beside* the profiles rather than
    among them, and ``all`` never implies it. Ingestion can legitimately
    persist such a listing (issue #110: an email carrying several different
    search links, or a recognised email whose profile lookup lost a
    concurrent write), and without its own option it would be reachable from
    nowhere -- unlike an inactive profile, it has no id to name.

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

# Accepted in `profile_id` to mean "listings with no subscription at all"
# (`search_profile_id IS NULL`). Not a profile, and deliberately not part of
# `all` -- it is a peer of the profile list, not a member of it.
PROFILE_UNASSIGNED_SENTINEL = "unassigned"

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

# Ids past this many are dropped, because the parsed list goes straight into
# a SQL `IN (...)` and a hand-written URL is not obliged to be reasonable.
# Dropping them is never silent: `ProfileSelection.truncated` travels to the
# page, which says so. The owner has nine profiles, so the form cannot reach
# this.
MAX_SELECTED_PROFILE_IDS = 50

# Shown wherever a multi-profile view has to withhold profile-specific travel
# data. Lives here so the two templates and the tests cannot drift apart.
#
# Worded as an offer rather than an apology (2026-08-09): showing every
# subscription at once is now the default view, so this line is on screen most
# of the time and must not read like something went wrong.
TRAVEL_NOTICE = (
    "Pick a single subscription to see its own travel targets and recalculate them"
)

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

    #: `profile_id=unassigned` was asked for -- listings with no profile.
    include_unassigned: bool = False

    #: More ids arrived than `MAX_SELECTED_PROFILE_IDS`; the overflow was
    #: dropped and the page has to say so.
    truncated: bool = False

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

    #: Widen the filter with `search_profile_id IS NULL`, and tick the
    #: "No subscription" box.
    include_unassigned: bool = False

    #: The id list overflowed `MAX_SELECTED_PROFILE_IDS`.
    truncated: bool = False

    @property
    def matches_nothing(self) -> bool:
        """An explicit selection that cannot return a row.

        Distinct from "no filter" (`filter_ids is None`) and from the
        unassigned choice, which selects real rows without naming a profile.
        """
        return self.filter_ids == () and not self.include_unassigned

    @property
    def withholds_profile_travel(self) -> bool:
        """The view covers more than one profile, so profile-specific travel
        data is withheld and the page owes the reader an explanation.

        Deliberately derived from the selection alone. An earlier version let
        the templates gate the explanation on "there is at least one active
        profile" as well, which held right up until *every* profile was
        inactive: a bookmark naming several of them then hid the travel data
        and the reason for it at the same time. Selecting inactive profiles
        by id is supported on purpose, so the explanation has to follow.
        """
        if self.single_id is not None:
            # Exactly one profile: its travel data is shown, nothing withheld.
            return False
        if self.matches_nothing:
            # Nothing on screen to explain.
            return False
        if self.filter_ids is None:
            # No filter and no profile resolved -- an install with nothing to
            # select. Advising the reader to pick one would be nonsense.
            return False
        return True

    @property
    def form_fallback_value(self) -> str:
        """Value for the form's hidden `profile_id`, used when nothing is ticked.

        Normally the `all` sentinel. On an explicitly empty selection it has
        to be the impossible marker instead: no box is ticked there either, so
        an `all` fallback would turn "show nothing" into "show every active
        profile" the moment the user changed an unrelated filter and pressed
        Apply.
        """
        if self.matches_nothing:
            return IMPOSSIBLE_PROFILE_ID
        return PROFILE_ALL_SENTINEL

    @property
    def travel_notice(self) -> str:
        return TRAVEL_NOTICE

    @property
    def label(self) -> str:
        """Text on the dropdown toggle: `All subscriptions` or `N selected`.

        Keyed off the state, not off the tick count: a selection of only
        impossible ids ticks nothing but shows nothing either, and labelling
        that "All subscriptions" would describe the opposite of what is on
        screen.
        """
        ticked = len(self.checked_ids) + (1 if self.include_unassigned else 0)
        if self.state is ProfileSelectionState.SELECTED:
            return f"{ticked} selected"
        if not ticked:
            return "All subscriptions"
        return f"{ticked} selected"


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
    saw_unassigned = False
    ids: list[int] = []

    for raw in raw_values:
        token = str(raw if raw is not None else "").strip()
        if token == "" or token.lower() == PROFILE_ALL_SENTINEL:
            saw_all = True
            continue
        if token.lower() == PROFILE_UNASSIGNED_SENTINEL:
            saw_unassigned = True
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

    if saw_number or saw_unassigned:
        # Ticked boxes beat the form's `all` fallback; see the module
        # docstring for why the fallback is posted at all.
        unique = _dedupe(ids)
        return ProfileSelection(
            ProfileSelectionState.SELECTED,
            unique[:MAX_SELECTED_PROFILE_IDS],
            include_unassigned=saw_unassigned,
            truncated=len(unique) > MAX_SELECTED_PROFILE_IDS,
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
        # "All subscriptions" is the whole point of the state, so no box is ticked
        # even though the filter names every active profile.
        checked: Tuple[int, ...] = ()
    elif selection.is_selected:
        filter_ids = selection.ids
        link_values = tuple(selection.ids)
        checked = selection.ids
        if selection.include_unassigned:
            link_values = link_values + (PROFILE_UNASSIGNED_SENTINEL,)
        elif not selection.ids:
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

    # One profile plus the unassigned rows is still not a single-profile view,
    # so the profile-specific travel UI stays hidden there too.
    single_id = (
        filter_ids[0]
        if filter_ids is not None
        and len(filter_ids) == 1
        and not selection.include_unassigned
        else None
    )

    return ResolvedProfileSelection(
        state=selection.state,
        filter_ids=filter_ids,
        link_values=link_values,
        checked_ids=checked,
        single_id=single_id,
        include_unassigned=selection.include_unassigned,
        truncated=selection.truncated,
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
    from sqlalchemy import or_

    if resolved.filter_ids is None and not resolved.include_unassigned:
        return query

    clauses = []
    ids = resolved.filter_ids or ()
    if len(ids) == 1:
        clauses.append(column == ids[0])
    elif ids:
        clauses.append(column.in_(ids))
    if resolved.include_unassigned:
        clauses.append(column.is_(None))

    if not clauses:
        # An explicit selection that named nothing reachable. `in_(())` is the
        # always-false expression that says so, rather than no filter at all.
        return query.filter(column.in_(()))
    if len(clauses) == 1:
        return query.filter(clauses[0])
    return query.filter(or_(*clauses))
