"""Deterministic, explainable recommendations over prepared descriptors."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import case, false

from services import taste_descriptors, taste_preferences

_VISUAL_RESEMBLANCE_ASPECTS = frozenset(
    {
        "visual_appeal",
        "house_character",
        "house_condition",
        "neighbor_privacy",
        "nearby_buildings",
        "agricultural_context",
        "room_scale",
    }
)
_NUMERIC_ASPECTS = frozenset({"house_area_m2", "plot_area_m2"})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(descriptor: Any, aspect_id: str) -> list[dict[str, Any]]:
    return taste_descriptors.aspect_values(descriptor, aspect_id)


def _normalised_value(value: Any, aspect_id: str) -> str | None:
    if value is None or aspect_id in _NUMERIC_ASPECTS:
        return None
    normalised = str(value)
    if aspect_id == "sea_view" and normalised in {"yes", "likely"}:
        return "present"
    return normalised


def _usable_values(rows: Iterable[dict[str, Any]], aspect_id: str) -> set[str]:
    """Comparable categorical values for a clause or reference facet.

    Numeric distance belongs to ``favorite_similarity``. Exact equality of
    two independently reported square-metre values is not meaningful and a
    phrase such as "small plot" supplies no safe threshold. Sea-view ``yes``
    and ``likely`` both mean present for preference matching while their raw
    evidence/status remains unchanged for display.
    """
    return {
        normalised
        for row in rows
        if row.get("status") in {"supported", "claimed"}
        and (normalised := _normalised_value(row.get("value"), aspect_id)) is not None
    }


def _rows_for_values(
    rows: Iterable[dict[str, Any]], aspect_id: str, values: set[str]
) -> list[dict[str, Any]]:
    """Rows whose own value supports the decision being explained."""
    return [
        row
        for row in rows
        if row.get("status") in {"supported", "claimed"}
        and _normalised_value(row.get("value"), aspect_id) in values
    ]


def _clause_values(
    clause: dict[str, Any], source_rows: list[dict[str, Any]], aspect_id: str
) -> set[str]:
    """Read the value stated by a clause, with legacy snapshot fallback.

    New profiles bind each clause to its own lexical values.  Falling back to
    every value on the source property is safe only for older snapshots that
    predate the ``values`` field; an explicitly empty list stays unknown.
    """
    if "values" not in clause:
        return _usable_values(source_rows, aspect_id)
    raw_values = clause.get("values")
    if not isinstance(raw_values, list):
        return set()
    return {
        normalised
        for value in raw_values
        if (normalised := _normalised_value(value, aspect_id)) is not None
    }


def _strongest_status(rows: list[dict[str, Any]]) -> str:
    statuses = {row.get("status") for row in rows}
    if "conflicting" in statuses:
        return "conflicting"
    if "supported" in statuses:
        return "supported"
    if "claimed" in statuses:
        return "claimed"
    return "unknown"


def _facet(
    aspect_id: str,
    rows: list[dict[str, Any]],
    *,
    source_property_id: int | None = None,
) -> dict[str, Any]:
    status = _strongest_status(rows)
    row = next(
        (item for item in rows if item.get("status") == status), rows[0] if rows else {}
    )
    return {
        "aspect_id": aspect_id,
        "label_key": f"recommendation_aspect_{aspect_id}",
        "status": status,
        "source_kind": row.get("source_kind"),
        "source_property_id": source_property_id,
        "evidence": row.get("evidence") or row.get("source_id"),
    }


def _applicable_clauses(
    clauses: list[dict[str, Any]], profile_id: int | None
) -> list[dict[str, Any]]:
    return [
        clause for clause in clauses if taste_preferences.applies_to(clause, profile_id)
    ]


def _nearest_reference(
    candidate: dict[str, Any],
    reference_ids: list[int],
    descriptors: dict[str, Any],
) -> dict[str, Any] | None:
    best: tuple[int, int, list[str]] | None = None
    candidate_aspects = _dict(candidate.get("aspects"))
    for reference_id in reference_ids:
        reference = _dict(descriptors.get(str(reference_id)))
        matched: list[str] = []
        for aspect_id in sorted(
            set(candidate_aspects) & set(_dict(reference.get("aspects")))
        ):
            left = _usable_values(_rows(candidate, aspect_id), aspect_id)
            right = _usable_values(_rows(reference, aspect_id), aspect_id)
            if left and right and left & right:
                matched.append(aspect_id)
        rank = (len(matched), -reference_id, matched)
        if best is None or rank[:2] > best[:2]:
            best = rank
            best_id = reference_id
    if best is None or not best[2]:
        return None
    return {"id": best_id, "matched_aspect_ids": best[2]}


def _photo_values(descriptor: Any, aspect_id: str) -> set[str]:
    return {
        str(row.get("value"))
        for row in _rows(descriptor, aspect_id)
        if row.get("source_kind") == "photo"
        and row.get("status") in {"supported", "claimed"}
        and row.get("value") is not None
    }


def _visual_overlap(candidate: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    return [
        aspect_id
        for aspect_id in sorted(_VISUAL_RESEMBLANCE_ASPECTS)
        if _photo_values(candidate, aspect_id) & _photo_values(reference, aspect_id)
    ]


def _numeric_similarity(
    similarity_reading: dict[str, Any] | None, reference_ids: list[int]
) -> tuple[int | None, float | None, list[str]]:
    if not isinstance(similarity_reading, dict):
        return None, None, []
    reference_id = similarity_reading.get("reference_id")
    score = similarity_reading.get("score")
    if (
        isinstance(reference_id, bool)
        or not isinstance(reference_id, int)
        or reference_id not in reference_ids
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
    ):
        return None, None, []
    compared = similarity_reading.get("compared")
    return (
        reference_id,
        float(score),
        list(compared) if isinstance(compared, list) else [],
    )


def _composite_reference(
    candidate: dict[str, Any],
    reference_ids: list[int],
    descriptors: dict[str, Any],
    similarity_reading: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, float, int]:
    """Select a positive reference from independent numeric and photo channels.

    Numeric Similar is measured against one reference.  Photo overlap is a
    separate resemblance signal, never an owner preference or coverage claim.
    A matched visual facet outweighs the 0..1 numeric tie-break so identical
    land similarity can still distinguish house texture/material.
    """
    numeric_reference_id, numeric_score, numeric_compared = _numeric_similarity(
        similarity_reading, reference_ids
    )
    best: tuple[float, int, float, int, list[str]] | None = None
    for reference_id in reference_ids:
        reference = _dict(descriptors.get(str(reference_id)))
        visual_matches = _visual_overlap(candidate, reference)
        numeric_component = (
            (numeric_score or 0.0) / 100.0
            if reference_id == numeric_reference_id
            else 0.0
        )
        composite = len(visual_matches) + numeric_component
        rank = (
            composite,
            len(visual_matches),
            numeric_component,
            -reference_id,
            visual_matches,
        )
        if best is None or rank[:4] > best[:4]:
            best = rank
            best_id = reference_id
    if best is None:
        return None, 0.0, 0
    _composite, visual_count, _numeric_component, _negative_id, visual_matches = best
    overall_numeric_component = (numeric_score or 0.0) / 100.0
    if not visual_matches and best_id != numeric_reference_id:
        return (
            _nearest_reference(candidate, reference_ids, descriptors),
            overall_numeric_component,
            0,
        )
    matched = visual_matches or numeric_compared
    return (
        {
            "id": best_id,
            "matched_aspect_ids": matched,
            "visual_matched_aspect_ids": visual_matches,
            "visual_match_count": visual_count,
            "numeric_reference_id": numeric_reference_id,
            "numeric_score": numeric_score,
            "numeric_compared_aspect_ids": numeric_compared,
        },
        overall_numeric_component,
        visual_count,
    )


@dataclass(frozen=True)
class RecommendationContext:
    profile: dict[str, Any]
    readings: dict[int, dict[str, Any]]


def _reading(
    prop: Any,
    *,
    descriptor: dict[str, Any],
    clauses: list[dict[str, Any]],
    descriptors: dict[str, Any],
    state_reference_ids: list[int],
    reference_ids: list[int],
    profile_state: str,
    similarity_reading: dict[str, Any] | None = None,
) -> dict[str, Any]:
    property_id = prop.id
    if (
        property_id in state_reference_ids
        and getattr(prop, "owner_verdict", None) != "rejected"
    ):
        state = "reference"
    elif getattr(prop, "owner_verdict", None) == "rejected":
        state = "rejected"
    else:
        state = "candidate" if profile_state == "current" else "stale"

    matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    needs: list[dict[str, Any]] = []
    unapplied: list[dict[str, Any]] = []
    relevant = 0
    supported = 0
    for clause in _applicable_clauses(
        clauses, getattr(prop, "search_profile_id", None)
    ):
        if clause.get("mapping_state") != "executable" or not clause.get("aspect_id"):
            unapplied.append(
                {
                    "id": clause.get("id"),
                    "aspect_id": clause.get("aspect_id"),
                    "text": clause.get("text"),
                    "reason": clause.get("reason") or "rule is not executable",
                    "source_property_id": clause.get("source_property_id"),
                }
            )
            continue
        if clause.get("polarity") == "tradeoff":
            continue
        aspect_id = clause["aspect_id"]
        relevant += 1
        candidate_rows = _rows(descriptor, aspect_id)
        source = _dict(descriptors.get(str(clause.get("source_property_id"))))
        source_rows = _rows(source, aspect_id)
        if _strongest_status(candidate_rows) == "conflicting":
            conflicts.append(
                _facet(
                    aspect_id,
                    candidate_rows,
                    source_property_id=clause.get("source_property_id"),
                )
            )
            continue
        candidate_values = _usable_values(candidate_rows, aspect_id)
        source_values = _clause_values(clause, source_rows, aspect_id)
        if not candidate_values or not source_values:
            needs.append(
                {
                    **_facet(
                        aspect_id,
                        candidate_rows,
                        source_property_id=clause.get("source_property_id"),
                    ),
                    "status": _strongest_status(candidate_rows),
                }
            )
            continue
        overlap = bool(candidate_values & source_values)
        overlap_rows = _rows_for_values(candidate_rows, aspect_id, source_values)
        decision_rows = overlap_rows if overlap else candidate_rows
        decision_status = _strongest_status(decision_rows)
        if decision_status == "supported":
            supported += 1
        polarity = clause.get("polarity")
        if polarity == "prefer" and overlap:
            matches.append(
                _facet(
                    aspect_id,
                    decision_rows,
                    source_property_id=clause.get("source_property_id"),
                )
            )
        elif (polarity == "avoid" and overlap) or (
            polarity == "prefer" and not overlap
        ):
            facet = _facet(
                aspect_id,
                decision_rows,
                source_property_id=clause.get("source_property_id"),
            )
            facet["hard"] = bool(
                clause.get("strength") == "hard" and decision_status == "supported"
            )
            conflicts.append(facet)

    if conflicts:
        group = "conflict"
    elif relevant and supported == relevant and matches:
        group = "confident"
    else:
        group = "potential"
    exploration = (
        group == "potential"
        and int(hashlib.sha256(str(property_id).encode()).hexdigest()[:4], 16) % 5 == 0
    )
    nearest, numeric_component, visual_match_count = _composite_reference(
        descriptor, reference_ids, descriptors, similarity_reading
    )
    if state == "candidate":
        group_base = {"confident": 3000.0, "potential": 2000.0, "conflict": 1000.0}[
            group
        ]
    elif state == "stale":
        group_base = 500.0
    elif state == "reference":
        group_base = 100.0
    else:
        group_base = 0.0
    rank_value = (
        group_base
        + min(len(matches), 20) * 10
        - min(len(conflicts), 20) * 10
        + numeric_component
        + visual_match_count
    )
    hard_conflicts = [facet for facet in conflicts if facet.get("hard")]
    return {
        "state": state,
        "group": group if state in {"candidate", "stale"} else None,
        "eligibility": (
            "excluded"
            if state == "candidate" and hard_conflicts
            else "eligible"
            if state == "candidate"
            else None
        ),
        "hard_exclusions": hard_conflicts,
        "nearest_positive_reference": nearest,
        "matches": matches,
        "conflicts": conflicts,
        "needs_verification": needs,
        "unapplied_clauses": unapplied,
        "coverage": {"supported": supported, "relevant": relevant},
        "exploration": exploration,
        "rank_value": rank_value,
    }


def build_context(
    props: Iterable[Any],
    profile_data: dict[str, Any] | None,
    profile_summary: dict[str, Any],
    *,
    similarity_ctx: Any = None,
) -> RecommendationContext:
    """Build page-ready readings with no bridge/network calls."""
    rows = list(props)
    if not profile_data:
        return RecommendationContext(
            profile=profile_summary,
            readings={
                prop.id: {
                    "state": "no_profile",
                    "group": None,
                    "eligibility": None,
                    "hard_exclusions": [],
                    "nearest_positive_reference": None,
                    "matches": [],
                    "conflicts": [],
                    "needs_verification": [],
                    "unapplied_clauses": [],
                    "coverage": {"supported": 0, "relevant": 0},
                    "exploration": False,
                    "rank_value": 0.0,
                }
                for prop in rows
            },
        )
    source = _dict(profile_data.get("source"))
    clauses = source.get("clauses") if isinstance(source.get("clauses"), list) else []
    descriptors = _dict(source.get("descriptors"))
    state_reference_ids = [
        value
        for value in source.get("positive_reference_ids", [])
        if isinstance(value, int)
    ]
    current_reference_ids = profile_summary.get("current_positive_reference_ids")
    if isinstance(current_reference_ids, list):
        current_reference_set = {
            value for value in current_reference_ids if isinstance(value, int)
        }
        # A favorite removed or rejected after this immutable snapshot must
        # stop anchoring immediately. A newly starred row still waits for the
        # refresh that captures its descriptor, so only the intersection is
        # safe to compare against.
        state_reference_ids = [
            value for value in state_reference_ids if value in current_reference_set
        ]
    references_by_profile: dict[int | None, list[int]] = {}
    for signal in source.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        reference_id = signal.get("property_id")
        if not isinstance(reference_id, int) or reference_id not in state_reference_ids:
            continue
        profile_id = signal.get("profile_id")
        if profile_id is not None and not isinstance(profile_id, int):
            continue
        references_by_profile.setdefault(profile_id, []).append(reference_id)
    readings = {}
    for prop in rows:
        taste = _dict(getattr(prop, "taste", None))
        visual = taste.get("visual_descriptor")
        descriptor = taste_descriptors.build_descriptor(
            prop,
            owner_reason=getattr(prop, "owner_verdict_reason", None),
            visual_input=visual,
        )
        readings[prop.id] = _reading(
            prop,
            descriptor=descriptor,
            clauses=clauses,
            descriptors=descriptors,
            state_reference_ids=state_reference_ids,
            reference_ids=references_by_profile.get(
                getattr(prop, "search_profile_id", None), []
            ),
            profile_state=profile_summary.get("state", "none"),
            similarity_reading=(
                similarity_ctx.read(prop.id) if similarity_ctx is not None else None
            ),
        )
    return RecommendationContext(profile=profile_summary, readings=readings)


def sort_expression(model: Any, ctx: RecommendationContext):
    """SQL ORDER BY expression for the exact context shown on the page."""
    ranks = {
        property_id: reading.get("rank_value", 0.0)
        for property_id, reading in ctx.readings.items()
    }
    if not ranks:
        # Keep a column-bearing CASE even when this request has no candidates.
        # PostgreSQL reads a bare numeric ORDER BY constant as an ordinal and
        # rejects a non-integer one; the false branch is never selected but
        # leaves the expression valid on both production and SQLite.
        return case((false(), model.id), else_=-1.0)
    return case(ranks, value=model.id, else_=-1.0)
