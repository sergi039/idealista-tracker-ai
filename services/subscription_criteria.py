"""A listing against its subscription's criteria: pass / fail / unknown.

The owner's requirements — a house of at least 150 m² on a plot of at least
700 m² — cannot be encoded on any portal (measured when the alerts were
created: no portal filters by plot), so the filter lives here, on data the
app holds. Three states, never two (#98): `fail` is a MEASURED shortfall,
`pass` needs every required figure measured and sufficient, and `unknown` is
everything else — a plot nobody has stated is not a plot that is too small.

The criteria are data, not code: `search_profiles.criteria`, e.g.
{"min_house_m2": 150, "min_plot_m2": 700}. A profile without criteria has no
verdict at all — its rows are never hidden and no filter control is drawn
for it. A malformed criteria block reads as no criteria, because hiding
listings on the strength of a typo would be the defect this module exists
to avoid, relocated.

Which stored figure answers which requirement follows `area_type`: `area` is
the BUILT surface unless the row says `plot` (bare land), and `plot_area`
(migration 025) is the parcel where the source portal stated one — for a
bare-land row without it, `area` IS the plot. Both readings exist twice, in
Python and in SQL, branch for branch (the `advertiser.py` contract), and
`tests/test_subscription_criteria.py` runs one matrix through both.

The default view hides only measured fails, and NEVER a row the owner has
favorited or reviewed — hiding a listing somebody already judged would
contradict their own judgement with a filter (the plan-gate reviewer's
finding, round 1).

**The `criteria` parameter is read here too**, by every surface that answers
over `properties`. It used to be read in `routes/main_routes.py`, where the
list, the map and the CSV could reach it and `routes/api_routes.py` could
not — so `GET /api/properties?criteria=fail` accepted the parameter and
ignored it, and answered `total: 443` for a subscription whose measured
fails are 59 of that number (measured against production 2026-08-31). That
is #445's regression in the one filter whose absence is not its off
position: a surface that keeps a filter while another drops it disagrees
about which listings exist. `profile_context()`, `apply_filter()` and
`row_verdict()` are that one reading, in SQL for the query and in Python for
a row that has already been loaded.
"""

import logging
import math
from typing import Any, Dict, Optional

from sqlalchemy import and_, func, or_

logger = logging.getLogger(__name__)

CRITERIA_KEYS = ("min_house_m2", "min_plot_m2")

# A surface no parcel has. It is a NaN filter first and a sanity bound
# second: PostgreSQL orders `NUMERIC 'NaN'` ABOVE every number, so
# `plot_area > 0 AND plot_area >= 700` is TRUE for a NaN and SQL called it
# `pass` while Python (where `nan > 0` is False) called it `unknown` — the
# gate review's reproduction. `< MAX_CREDIBLE_M2` excludes NaN on
# PostgreSQL and keeps every real value, in a form SQLite reads too, and
# both languages apply it so a value this absurd reads as unmeasured on
# both. 1e9 m2 is a thousand square kilometres.
MAX_CREDIBLE_M2 = 1e9


def read_criteria(profile: Any) -> Optional[Dict[str, float]]:
    """The profile's validated criteria, or None (no verdicts, no filter)."""
    block = getattr(profile, "criteria", None)
    if not isinstance(block, dict):
        return None
    unknown_keys = set(block) - set(CRITERIA_KEYS)
    if unknown_keys:
        # A typo ("min_plto_m2") silently ignored would half-apply the
        # block and hide listings on the strength of the half that parsed
        # (the implementation review's reproduction). Unknown keys reject
        # the whole block; there is no schema version to be forward-
        # compatible with.
        logger.warning(
            "Profile %s criteria %r carries unknown keys %s; reading as no criteria",
            getattr(profile, "id", "?"),
            block,
            sorted(unknown_keys),
        )
        return None
    cleaned: Dict[str, float] = {}
    for key in CRITERIA_KEYS:
        value = block.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            logger.warning(
                "Profile %s criteria %r is malformed; reading as no criteria",
                getattr(profile, "id", "?"),
                block,
            )
            return None
        try:
            number = float(value)
        except (OverflowError, ValueError):
            # 10**400 is a perfectly good Python int and float() of it
            # raises — a hand-edited block must not 500 the listing page.
            number = math.inf
        if not math.isfinite(number) or number <= 0:
            logger.warning(
                "Profile %s criteria %r has a non-finite or non-positive "
                "bound; reading as no criteria",
                getattr(profile, "id", "?"),
                block,
            )
            return None
        cleaned[key] = number
    return cleaned or None


def _effective_figures(prop: Any) -> Dict[str, Optional[float]]:
    """Which stored number answers which requirement, honestly.

    `area` is built surface unless the row says `plot`; `plot_area` wins for
    the parcel where stated, and for bare land the `area` IS the parcel.
    A non-positive stored value reads as unmeasured, never as a tiny plot —
    fotocasa's 0-as-blank convention one layer up.
    """

    def _positive(value: Any) -> Optional[float]:
        """A credible surface, or None. NaN, inf and absurd values are not
        measurements — see MAX_CREDIBLE_M2 for why SQL needs the ceiling."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number if 0 < number < MAX_CREDIBLE_M2 else None

    area = _positive(prop.area)
    plot = _positive(getattr(prop, "plot_area", None))
    # `.strip(" ")`, not `.strip()`: the SQL twin normalizes with trim(),
    # which strips SPACES only (btrim — the SEPE lesson, one module over),
    # and a tab-polluted "PLOT\t" must read the same in both languages —
    # here as not-plot, exactly as lower(trim(...)) reads it.
    area_type = (getattr(prop, "area_type", None) or "").strip(" ").lower()
    if area_type == "plot":
        return {"house_m2": None, "plot_m2": plot if plot is not None else area}
    return {"house_m2": area, "plot_m2": plot}


def read_verdict(prop: Any, criteria: Optional[Dict[str, float]]) -> Dict[str, Any]:
    """pass / fail / unknown for one row, with the figures it was judged on."""
    if not criteria:
        return {"state": "no_criteria"}
    figures = _effective_figures(prop)
    checks: Dict[str, Optional[bool]] = {}
    if "min_house_m2" in criteria:
        value = figures["house_m2"]
        checks["house"] = None if value is None else value >= criteria["min_house_m2"]
    if "min_plot_m2" in criteria:
        value = figures["plot_m2"]
        checks["plot"] = None if value is None else value >= criteria["min_plot_m2"]
    if any(result is False for result in checks.values()):
        state = "fail"
    elif checks and all(result is True for result in checks.values()):
        state = "pass"
    else:
        state = "unknown"
    return {"state": state, "checks": checks, "figures": figures}


def _credible(column):
    """The SQL twin of `_positive()`: present, positive and credible.

    The upper bound is what excludes `NUMERIC 'NaN'` on PostgreSQL, where
    NaN compares GREATER than every number — without it a NaN satisfied
    both `> 0` and `>= bound` and SQL alone answered `pass`. Definite for
    every row (no NULL third value), so negating it stays sound.
    """
    return and_(
        column.isnot(None),
        column > 0,
        column < MAX_CREDIBLE_M2,
    )


def db_not_credible(column):
    """`~_credible`, written positively so it is definite for every row:
    NULL, non-positive, or past the credible ceiling (which is where a
    PostgreSQL NaN lands)."""
    return or_(
        column.is_(None),
        column <= 0,
        column >= MAX_CREDIBLE_M2,
    )


def _definite_shapes(model):
    """`is_plot` / `not_plot` with NO NULL third value.

    Every clause built on these is definitely TRUE or FALSE per row, never
    NULL — which is what makes their negations sound: `filter(~expr)` drops
    a NULL outright under three-valued logic, so one NULL-able comparison
    would silently eat rows from the `unknown` filter.
    """
    # lower(trim(...)) — the Python reader normalizes with .strip().lower(),
    # and a hand-written " PLOT " must not read as bare land in one language
    # and built in the other (the gate review's case reproduction). The NULL
    # guard comes first, so every clause stays definite.
    normalized = func.lower(func.trim(model.area_type))
    is_plot = and_(model.area_type.isnot(None), normalized == "plot")
    not_plot = or_(model.area_type.is_(None), normalized != "plot")
    return is_plot, not_plot


def failing_expression(model, criteria: Dict[str, float]):
    """SQL twin of `read_verdict()[state] == 'fail'`, branch for branch.

    Only typed columns are compared (`area`, `plot_area`, `area_type`), so
    nothing here can raise on hand-edited data — the reason `plot_area` is a
    column and not a JSON key. A measured shortfall on EITHER bound fails,
    exactly as the Python side ORs its False checks. Every clause is
    definite (see `_definite_shapes`).
    """
    is_plot, not_plot = _definite_shapes(model)
    clauses = []
    if "min_house_m2" in criteria:
        clauses.append(
            and_(
                not_plot,
                _credible(model.area),
                model.area < criteria["min_house_m2"],
            )
        )
    if "min_plot_m2" in criteria:
        bound = criteria["min_plot_m2"]
        plot_known_and_short = and_(
            _credible(model.plot_area),
            model.plot_area < bound,
        )
        # `plot_area <= 0` counts as absent, exactly as the Python
        # reader's `_positive()` does — zero is fotocasa's blank, and a
        # bare-land row carrying it falls back to `area`, both languages
        # (the review's 650/plot/0 reproduction).
        plot_absent = db_not_credible(model.plot_area)
        bare_land_short = and_(
            is_plot,
            plot_absent,
            _credible(model.area),
            model.area < bound,
        )
        clauses.append(or_(plot_known_and_short, bare_land_short))
    if not clauses:
        # No bounds means nothing can measurably fail.
        return and_(model.id.is_(None), model.id.isnot(None))
    return or_(*clauses)


def passing_expression(model, criteria: Dict[str, float]):
    """SQL twin of `read_verdict()[state] == 'pass'`: every bound measured
    AND sufficient. `unknown` is then `~fail AND ~pass`, which is only sound
    because both expressions are definite per row."""
    is_plot, not_plot = _definite_shapes(model)
    clauses = []
    if "min_house_m2" in criteria:
        clauses.append(
            and_(
                not_plot,
                _credible(model.area),
                model.area >= criteria["min_house_m2"],
            )
        )
    if "min_plot_m2" in criteria:
        bound = criteria["min_plot_m2"]
        clauses.append(
            or_(
                and_(_credible(model.plot_area), model.plot_area >= bound),
                and_(
                    is_plot,
                    db_not_credible(model.plot_area),
                    _credible(model.area),
                    model.area >= bound,
                ),
            )
        )
    if not clauses:
        return and_(model.id.is_(None), model.id.isnot(None))
    return and_(*clauses)


def owner_has_judged(prop: Any) -> bool:
    """The three exemptions of `hidden_by_default_expression`, in Python.

    Branch for branch with the SQL twin below, and deliberately not with
    `owner_review.read_decision`: the SQL asks `owner_verdict IS NULL`, so an
    empty string stored there exempts the row in SQL, and a Python reader
    that folded it back into `undecided` would call a visible row hidden.
    """
    from services.owner_review import ACTION_NONE, read_action

    if getattr(prop, "is_favorite", None) is True:
        return True
    if getattr(prop, "owner_verdict", None) is not None:
        return True
    # Date-free, exactly as `open_action_expression` is: `pending` and
    # `overdue` are both "still to do", and only the date tells them apart.
    return read_action(prop)["state"] != ACTION_NONE


def hidden_by_default(prop: Any, criteria: Optional[Dict[str, float]]) -> bool:
    """Python twin of `hidden_by_default_expression` for ONE row.

    The listing page hides a row and says only how many it hid; the row's own
    page is where the reader arrives asking why, so it needs this reading in
    Python. It is the twin rather than a second rule for the same reason the
    verdict is (the `advertiser.py` contract): a page saying "this listing is
    hidden" while the list still draws it is a third wrong number.
    """
    if not criteria:
        return False
    return read_verdict(prop, criteria)["state"] == "fail" and not owner_has_judged(
        prop
    )


def hidden_by_default_expression(model, criteria: Dict[str, float]):
    """What the default view hides: measured fails the owner has NOT judged.

    A favorited or reviewed listing is never hidden — the filter must not
    overrule the owner's own recorded judgement. **An outstanding action
    counts as such a judgement** (#502 review, the HIGH finding): a row
    carrying `next_action` was hidden here while the overdue count, built
    without any criteria clause, went on advertising it — so the bare page
    read "1 overdue" and its own link landed on "0 properties found" and
    re-rendered the same link, a loop with no way out of it.

    Three exemptions, one idea: the owner has touched this row, so the
    subscription's blanket criteria no longer decide whether they see it.
    The action predicate is deliberately date-free — `pending` and `overdue`
    are both "still to do", and only the date tells them apart — so nothing
    here needs `review_today` threaded in, and the hide cannot drift from the
    counts by recomputing a date per row.
    """
    from services.owner_review import open_action_expression

    return and_(
        failing_expression(model, criteria),
        model.is_favorite.isnot(True),
        model.owner_verdict.is_(None),
        ~open_action_expression(model),
    )


# The vocabulary of the `criteria` parameter. `default` is what an ABSENT
# parameter means, and it is the one filter in this application that narrows
# when nobody asked for it — `utils/listing_filters.CLEARED_NOT_ABSENT`
# records what that costs a "clear the filters" link.
FILTER_MODES = ("default", "all", "pass", "fail", "unknown")


def read_filter_mode(raw_value):
    """`(mode, recognised)` for a raw `criteria` parameter.

    An unrecognised spelling falls back to `default`, which HIDES the
    measured fails — so a caller who typed `criteria=failing` gets a narrower
    answer than the one they asked for. `recognised` is what lets a surface
    say so instead of leaving that to be discovered, the shape
    `routes/api_routes.py` already uses for an unreadable `profile_id`.

    One reading, so the surface that DESCRIBES the mode and the surface that
    APPLIES it cannot disagree about which one ran.
    """
    mode = (raw_value or "").strip().lower()
    if mode in ("", "default"):
        return "default", True
    if mode in FILTER_MODES:
        return mode, True
    return "default", False


def profile_context(model):
    """The subscriptions carrying criteria, with their SQL clauses.

    None when no profile has criteria — the filter control is then not drawn
    and no query is touched, so the feature is dormant until the owner sets
    criteria on a subscription. The clauses are per-profile ORs: a row fails
    only against ITS OWN subscription's bounds, and rows of criteria-less
    subscriptions are never touched by any of them.
    """
    from services.search_profile_service import SearchProfileService

    pairs = []
    for profile in SearchProfileService.list_profiles(active_only=False):
        criteria = read_criteria(profile)
        if criteria:
            pairs.append((profile.id, criteria))
    if not pairs:
        return None

    def _across(builder):
        # `search_profile_id.isnot(None)` first: on an UNASSIGNED row the
        # bare `== pid` is NULL, the OR of NULLs is NULL, and `~NULL` drops
        # the row from the default view — the review's reproduction. The
        # definite guard makes the whole clause FALSE there, so unassigned
        # rows are never touched by anybody's criteria.
        return or_(
            *[
                and_(
                    model.search_profile_id.isnot(None),
                    model.search_profile_id == pid,
                    builder(model, crit),
                )
                for pid, crit in pairs
            ]
        )

    # Membership in SOME criteria-carrying subscription, for the `unknown`
    # mode: unknown is ~fail AND ~pass, and without this clause a row whose
    # subscription has NO criteria answered both negations TRUE and leaked
    # into a verdict it never had (its reading is `no_criteria`) — the gate
    # review's finding.
    member = or_(
        *[
            and_(
                model.search_profile_id.isnot(None),
                model.search_profile_id == pid,
            )
            for pid, _ in pairs
        ]
    )
    return {
        "pairs": pairs,
        # The same pairs keyed for the Python reader, so `row_verdict` judges
        # a loaded row against the bounds the SQL clause above applied to it
        # rather than against a second lookup of its own.
        "by_profile": dict(pairs),
        "member": member,
        "hidden_default": _across(hidden_by_default_expression),
        "fail": _across(failing_expression),
        "pass": _across(passing_expression),
    }


def apply_filter(query, ctx, raw_value, count_hidden=False):
    """One reading of the `criteria` parameter for every listing surface —
    the list, the map, the CSV and `GET /api/properties`.

    Default ('' or 'default') hides measured fails the owner has not judged
    (never a favorited or reviewed row); `all` shows everything; `pass`,
    `fail`, `unknown` select one verdict. Returns (query, hidden_count) —
    the count only when asked, because it costs a COUNT(*) and only a surface
    that draws the disclosure needs it.

    A `ctx` of None leaves the query alone, INCLUDING under `fail`/`pass`/
    `unknown`: no subscription carries criteria, so no row has a verdict and
    there is nothing to select. A surface that offers the parameter where the
    control is not drawn owes its reader that sentence — `/properties` never
    draws the control in that state, `GET /api/properties` cannot help being
    asked, and says so in its scope block.
    """
    if ctx is None:
        return query, None
    mode, _ = read_filter_mode(raw_value)
    if mode == "all":
        return query, None
    if mode == "fail":
        return query.filter(ctx["fail"]), None
    if mode == "pass":
        return query.filter(ctx["pass"]), None
    if mode == "unknown":
        return query.filter(ctx["member"], ~ctx["fail"], ~ctx["pass"]), None
    hidden = query.filter(ctx["hidden_default"]).count() if count_hidden else None
    return query.filter(~ctx["hidden_default"]), hidden


def row_verdict(prop, ctx):
    """`read_verdict` for one row, against ITS OWN subscription's bounds.

    The Python twin of what `apply_filter` selects in SQL, for a surface that
    has the row in hand and has to SAY which verdict it carries — the CSV
    export and the JSON payload. `no_criteria` where the row's subscription
    sets none and where it has no subscription at all, which is exactly the
    set the SQL clauses leave alone.
    """
    criteria = None
    if ctx is not None:
        profile_id = getattr(prop, "search_profile_id", None)
        if profile_id is not None:
            criteria = ctx["by_profile"].get(int(profile_id))
    return read_verdict(prop, criteria)
