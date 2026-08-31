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
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_

logger = logging.getLogger(__name__)

CRITERIA_KEYS = ("min_house_m2", "min_plot_m2")


def read_criteria(profile: Any) -> Optional[Dict[str, float]]:
    """The profile's validated criteria, or None (no verdicts, no filter)."""
    block = getattr(profile, "criteria", None)
    if not isinstance(block, dict):
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
        if value <= 0:
            logger.warning(
                "Profile %s criteria %r has a non-positive bound; reading as "
                "no criteria",
                getattr(profile, "id", "?"),
                block,
            )
            return None
        cleaned[key] = float(value)
    return cleaned or None


def _effective_figures(prop: Any) -> Dict[str, Optional[float]]:
    """Which stored number answers which requirement, honestly.

    `area` is built surface unless the row says `plot`; `plot_area` wins for
    the parcel where stated, and for bare land the `area` IS the parcel.
    A non-positive stored value reads as unmeasured, never as a tiny plot —
    fotocasa's 0-as-blank convention one layer up.
    """

    def _positive(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    area = _positive(prop.area)
    plot = _positive(getattr(prop, "plot_area", None))
    area_type = (getattr(prop, "area_type", None) or "").strip().lower()
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


def _definite_shapes(model):
    """`is_plot` / `not_plot` with NO NULL third value.

    Every clause built on these is definitely TRUE or FALSE per row, never
    NULL — which is what makes their negations sound: `filter(~expr)` drops
    a NULL outright under three-valued logic, so one NULL-able comparison
    would silently eat rows from the `unknown` filter.
    """
    is_plot = and_(model.area_type.isnot(None), model.area_type == "plot")
    not_plot = or_(model.area_type.is_(None), model.area_type != "plot")
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
                model.area.isnot(None),
                model.area > 0,
                model.area < criteria["min_house_m2"],
            )
        )
    if "min_plot_m2" in criteria:
        bound = criteria["min_plot_m2"]
        plot_known_and_short = and_(
            model.plot_area.isnot(None),
            model.plot_area > 0,
            model.plot_area < bound,
        )
        bare_land_short = and_(
            is_plot,
            model.plot_area.is_(None),
            model.area.isnot(None),
            model.area > 0,
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
                model.area.isnot(None),
                model.area > 0,
                model.area >= criteria["min_house_m2"],
            )
        )
    if "min_plot_m2" in criteria:
        bound = criteria["min_plot_m2"]
        clauses.append(
            or_(
                and_(
                    model.plot_area.isnot(None),
                    model.plot_area > 0,
                    model.plot_area >= bound,
                ),
                and_(
                    is_plot,
                    model.plot_area.is_(None),
                    model.area.isnot(None),
                    model.area > 0,
                    model.area >= bound,
                ),
            )
        )
    if not clauses:
        return and_(model.id.is_(None), model.id.isnot(None))
    return and_(*clauses)


def hidden_by_default_expression(model, criteria: Dict[str, float]):
    """What the default view hides: measured fails the owner has NOT judged.

    A favorited or reviewed listing is never hidden — the filter must not
    overrule the owner's own recorded judgement.
    """
    return and_(
        failing_expression(model, criteria),
        model.is_favorite.isnot(True),
        model.owner_verdict.is_(None),
    )
