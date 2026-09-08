"""Source-typed property descriptors shared by references and candidates.

This module is deliberately pure: it reads facts already held by a ``Property``
and never calls a model or the network.  A descriptor is evidence, not taste;
the preference matcher in :mod:`services.taste_recommendation` consumes the
same shape for a starred reference and a new candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any, Iterable

from services import sea_view_service, visual_input as visual_input_service
from services.subscription_criteria import effective_figures

SCHEMA_VERSION = 1
VALID_STATUSES = frozenset({"supported", "claimed", "unknown", "conflicting"})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number < 1e9 else None


def _one_line(value: Any, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()[:limit].strip()
    return text or None


def _evidence(
    *,
    source_kind: str,
    source_id: str,
    status: str,
    value: Any,
    observed_at: str | None = None,
    quote: str | None = None,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid descriptor status: {status}")
    row = {
        "value": value,
        "status": status,
        "source_kind": source_kind,
        "source_id": source_id,
    }
    if observed_at:
        row["observed_at"] = observed_at
    if quote:
        row["evidence"] = quote
    return row


def _text_claims(
    text: str | None, *, source_kind: str, source_id: str
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Conservative lexical claims from bounded source text.

    These are claims even when the advertiser says "confirmed".  Absence is
    never inferred from a photograph or from missing words.
    """
    line = _one_line(text, 1800)
    if not line:
        return []
    low = line.casefold()
    rules: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "plot_outline",
            "notched",
            ("l-shaped", "l shaped", "l-образ", "переше", "шеей", "вырез", "изломан"),
        ),
        (
            "plot_outline",
            "regular",
            (
                "правильной формы",
                "прямоуголь",
                "rectangular",
                "regular plot",
                "форма компактная",
            ),
        ),
        ("house_condition", "ruined", ("разрушенн", "руин", "ruin")),
        (
            "house_condition",
            "major_renovation",
            (
                "под реконструкц",
                "много ремонта",
                "большая реконструк",
                "major renovation",
            ),
        ),
        ("house_condition", "damp", ("сырост", "сырость", "damp")),
        ("room_scale", "small", ("маленькие комнат", "small rooms")),
        (
            "neighbor_privacy",
            "low",
            (
                "чужие дома близко",
                "много построек",
                "не изолирован",
                "dense neighbours",
            ),
        ),
        (
            "neighbor_privacy",
            "high",
            ("мало соседей", "нет других домов", "private setting"),
        ),
        (
            "agricultural_context",
            "present",
            ("сельхоз", "сх постройки", "agricultural", "огороды"),
        ),
        ("road_proximity", "near", ("возле дороги", "рядом с дорог", "near the road")),
        ("beach_access", "far", ("далеко от пляж", "far from beach")),
        (
            "beach_access",
            "walkable",
            ("пляж 488", "пляж 1,0", "пешком", "walkable beach"),
        ),
        (
            "house_character",
            "old_farmhouse",
            ("старый крестьянский дом", "old farmhouse"),
        ),
        ("fiber", "present", ("оптика 1", "fiber on", "fibra disponible")),
        ("fiber", "absent", ("оптики на парцеле нет", "no fiber", "sin fibra")),
        ("land_presence", "absent", ("нет земли рядом", "no adjacent land")),
        ("visual_appeal", "disliked", ("визуально не нравится", "не красиво", "ugly")),
        (
            "planning_usability",
            "claimed_usable",
            ("легален", "можно ремонтировать", "nucleo rural", "núcleo rural"),
        ),
        (
            "planning_usability",
            "needs_verification",
            ("письменная справка", "certificado urbanístico", "открыты канализация"),
        ),
    )
    found: list[tuple[str, dict[str, Any]]] = []
    for aspect_id, value, needles in rules:
        if any(needle in low for needle in needles):
            if source_kind == "owner_research_claim" and aspect_id == "visual_appeal":
                # "I dislike how it looks" is a preference signal, not an
                # observable visual facet. The photo extractor must name the
                # actual material/style/condition evidence before candidates
                # can be compared on it.
                continue
            found.append(
                (
                    aspect_id,
                    _evidence(
                        source_kind=source_kind,
                        source_id=source_id,
                        status="claimed",
                        value=value,
                        quote=line,
                    ),
                )
            )
    return found


def _plot_claims(attributes: dict[str, Any]) -> Iterable[tuple[str, str, Any]]:
    """Explicit known homes for dossier/research plot figures.

    The first string is provenance, the second is the leaf name.  We do not
    recursively hunt arbitrary JSON because a similarly named area in an
    unrelated block is not this parcel.
    """
    homes = (
        ("attributes", attributes),
        ("attributes.dossier", _dict(attributes.get("dossier"))),
        ("attributes.research", _dict(attributes.get("research"))),
        ("attributes.property_research", _dict(attributes.get("property_research"))),
    )
    keys = ("plot_area_cadastre_m2", "plot_area_m2", "parcel_area_m2")
    for home_name, home in homes:
        for key in keys:
            value = _finite_positive(home.get(key))
            if value is not None:
                yield home_name, key, value


def _mark_numeric_conflicts(observations: list[dict[str, Any]]) -> None:
    values = [
        float(item["value"])
        for item in observations
        if _finite_positive(item.get("value"))
    ]
    if len(values) < 2:
        return
    low, high = min(values), max(values)
    if high - low > max(5.0, low * 0.05):
        for item in observations:
            item["status"] = "conflicting"


def _merge_visual(
    aspects: dict[str, list[dict[str, Any]]], visual_input: Any, fingerprint: str
) -> dict[str, str] | None:
    block = _dict(visual_input)
    if (
        block.get("schema_version") != 1
        or block.get("property_fingerprint") != fingerprint
        or not isinstance(block.get("input_fingerprint"), str)
        or re.fullmatch(r"[0-9a-f]{64}", block["input_fingerprint"]) is None
    ):
        return None
    rows = block.get("visual_observations")
    if not isinstance(rows, list):
        return None
    allowed = {
        "visual_appeal",
        "house_character",
        "house_condition",
        "neighbor_privacy",
        "nearby_buildings",
        "agricultural_context",
        "room_scale",
        "plot_outline",
    }
    accepted: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("aspect_id") not in allowed:
            continue
        status = row.get("status")
        evidence = _dict(row.get("evidence"))
        image_sha256 = evidence.get("image_sha256")
        image_index = evidence.get("image_index")
        value = row.get("value")
        confidence = row.get("confidence")
        limitation = row.get("limitation")
        if (
            status not in VALID_STATUSES
            or evidence.get("source_kind") != "photo"
            or not isinstance(evidence.get("source_id"), str)
            or not isinstance(image_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", image_sha256) is None
            or not isinstance(image_index, int)
            or isinstance(image_index, bool)
            or (
                value is not None
                and value
                not in visual_input_service.VISUAL_VALUE_VOCABULARY[row["aspect_id"]]
            )
            or (status == "unknown" and value is not None)
            or (
                row["aspect_id"] == "plot_outline"
                and (status != "unknown" or value is not None)
            )
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
            or not isinstance(limitation, str)
            or not limitation.strip()
        ):
            continue
        observation = _evidence(
            source_kind="photo",
            source_id=evidence["source_id"],
            status=status,
            value=value,
            quote=_one_line(limitation),
        )
        observation["evidence"] = {
            "source_kind": "photo",
            "source_id": evidence["source_id"],
            "image_sha256": image_sha256,
            "image_index": image_index,
        }
        aspects[row["aspect_id"]].append(observation)
        accepted.append(
            {
                "aspect_id": row["aspect_id"],
                "value": value,
                "status": status,
                "evidence": observation["evidence"],
                "confidence": float(confidence),
                "limitation": limitation.strip(),
            }
        )
    descriptor_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "input_fingerprint": block["input_fingerprint"],
                "visual_observations": accepted,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "input_fingerprint": block["input_fingerprint"],
        "descriptor_fingerprint": descriptor_fingerprint,
    }


def descriptor_input(prop: Any) -> dict[str, Any]:
    """Stable basis for descriptor freshness, excluding taste itself."""
    return {
        "id": getattr(prop, "id", None),
        "area": str(getattr(prop, "area", None)),
        "plot_area": str(getattr(prop, "plot_area", None)),
        "area_type": getattr(prop, "area_type", None),
        "category": getattr(prop, "property_category", None),
        "subtype": getattr(prop, "property_subtype", None),
        "title": getattr(prop, "title", None),
        "description": getattr(prop, "description", None),
        "attributes": _dict(getattr(prop, "attributes", None)),
        "environment": _dict(getattr(prop, "environment", None)),
        "enrichment": _dict(getattr(prop, "enrichment", None)),
        "travel": _dict(getattr(prop, "travel", None)),
    }


def input_fingerprint(prop: Any) -> str:
    raw = json.dumps(
        descriptor_input(prop), ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_descriptor(
    prop: Any,
    *,
    owner_reason: str | None = None,
    visual_input: Any = None,
) -> dict[str, Any]:
    """Build the shared source-typed descriptor for one property."""
    aspects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fingerprint = input_fingerprint(prop)
    figures = effective_figures(prop)
    if figures.get("house_m2") is not None:
        aspects["house_area_m2"].append(
            _evidence(
                source_kind="canonical",
                source_id="properties.area",
                status="supported",
                value=figures["house_m2"],
            )
        )
    if figures.get("plot_m2") is not None:
        source_id = (
            "properties.plot_area"
            if _finite_positive(getattr(prop, "plot_area", None))
            else "properties.area"
        )
        aspects["plot_area_m2"].append(
            _evidence(
                source_kind="canonical",
                source_id=source_id,
                status="supported",
                value=figures["plot_m2"],
            )
        )
    aspects["property_kind"].append(
        _evidence(
            source_kind="canonical",
            source_id="property_classification",
            status="supported",
            value="land" if figures.get("bare_land") else "house",
        )
    )

    attrs = _dict(getattr(prop, "attributes", None))
    for home, key, value in _plot_claims(attrs):
        aspects["plot_area_m2"].append(
            _evidence(
                source_kind="research_claim",
                source_id=f"{home}.{key}",
                status="claimed",
                value=value,
            )
        )

    enrichment = _dict(getattr(prop, "enrichment", None))
    cadastre = _dict(enrichment.get("cadastre"))
    geometry = _dict(cadastre.get("geometry"))
    cadastral_area = _finite_positive(geometry.get("area_m2"))
    if cadastral_area is not None:
        aspects["plot_area_m2"].append(
            _evidence(
                source_kind="measurement",
                source_id="cadastre.geometry.area_m2",
                status="supported",
                value=cadastral_area,
                observed_at=_one_line(cadastre.get("measured_at")),
            )
        )
    if aspects.get("plot_area_m2"):
        _mark_numeric_conflicts(aspects["plot_area_m2"])

    sea = sea_view_service.read_verdict(prop)
    sea_state = sea.get("state") if isinstance(sea, dict) else None
    if (
        sea_state in sea_view_service.VALID_STATES
        and sea_state != sea_view_service.UNKNOWN
    ):
        detail = _dict(sea.get("detail"))
        source_kind = (
            "measurement" if detail.get("source") != "manual" else "owner_measurement"
        )
        aspects["sea_view"].append(
            _evidence(
                source_kind=source_kind,
                source_id="sea_view_service",
                status="supported",
                value=sea_state,
                observed_at=_one_line(detail.get("measured_at")),
            )
        )
    else:
        aspects["sea_view"].append(
            _evidence(
                source_kind="canonical",
                source_id="sea_view_service",
                status="unknown",
                value="unknown",
            )
        )

    travel = _dict(getattr(prop, "travel", None))
    beaches = _dict(travel.get("beaches"))
    if (
        beaches.get("status") == "ok"
        and isinstance(beaches.get("items"), list)
        and beaches["items"]
    ):
        first = _dict(beaches["items"][0])
        duration = _finite_positive(first.get("duration_min"))
        if duration is not None:
            aspects["beach_access"].append(
                _evidence(
                    source_kind="measurement",
                    source_id="travel.beaches",
                    status="supported",
                    value="walkable" if duration <= 15 else "far",
                    observed_at=_one_line(beaches.get("measured_at")),
                )
            )

    for aspect_id, observation in _text_claims(
        getattr(prop, "description", None),
        source_kind="listing_claim",
        source_id="properties.description",
    ):
        aspects[aspect_id].append(observation)
    for aspect_id, observation in _text_claims(
        owner_reason,
        source_kind="owner_research_claim",
        source_id=f"property:{getattr(prop, 'id', '?')}:reason",
    ):
        aspects[aspect_id].append(observation)

    visual_fingerprints = _merge_visual(aspects, visual_input, fingerprint)
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "property_id": getattr(prop, "id", None),
        "input_fingerprint": fingerprint,
        "aspects": {key: value for key, value in sorted(aspects.items())},
    }
    if visual_fingerprints is not None:
        descriptor["visual_input_fingerprint"] = visual_fingerprints[
            "input_fingerprint"
        ]
        descriptor["visual_descriptor_fingerprint"] = visual_fingerprints[
            "descriptor_fingerprint"
        ]
    return descriptor


def aspect_values(descriptor: Any, aspect_id: str) -> list[dict[str, Any]]:
    block = _dict(descriptor)
    aspects = _dict(block.get("aspects"))
    rows = aspects.get(aspect_id)
    return rows if isinstance(rows, list) else []
