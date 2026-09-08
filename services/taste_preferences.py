"""Compile owner comments into constrained, source-attributed preference clauses."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from services import taste_descriptors

SCHEMA_VERSION = 1

_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "plot_outline",
        (
            "l-shaped",
            "l-образ",
            "изломан",
            "переше",
            "правильной формы",
            "прямоуголь",
            "форма компактная",
        ),
    ),
    (
        "planning_usability",
        (
            "легален",
            "можно ремонтировать",
            "nucleo rural",
            "núcleo rural",
            "certificado",
            "справка мэрии",
            "pola",
        ),
    ),
    ("utilities", ("канализац", "скважин", "септик")),
    ("beach_access", ("пляж", "пляжей")),
    ("fiber", ("оптик", "гбит", "fiber")),
    ("price_value", ("€/м²", "медиан", "eur/m")),
    ("sea_view", ("моря не видно", "вид на море")),
    (
        "house_condition",
        ("сырост", "сырость", "заброш", "ремонт", "реконструк", "разруш"),
    ),
    ("area_consistency", ("расхожд", "кадастр сходится")),
    (
        "neighbor_privacy",
        ("не изолирован", "чужие дома близко", "мало соседей", "нет других домов"),
    ),
    ("nearby_buildings", ("много построек",)),
    ("agricultural_context", ("сельхоз", "сх постройки", "огороды")),
    ("road_proximity", ("возле дороги", "рядом с дорог")),
    ("house_character", ("крестьянский дом", "каменн")),
    ("room_scale", ("маленькие комнат",)),
    ("plot_area_m2", ("маленький участок", "участок малень")),
    ("land_presence", ("нет земли рядом",)),
    ("visual_appeal", ("визуально не нравится", "не красиво")),
    ("property_kind", ("это участок, а не дом", "участок, а не дом")),
)


def _segments(reason: str) -> list[tuple[str, bool]]:
    text = re.sub(r"\s+", " ", reason).strip()
    if not text:
        return []
    # Semicolon and sentence boundaries are significant; commas stay because
    # dossier clauses commonly carry one fact plus its qualification.
    parts: list[tuple[str, bool]] = []
    for match in re.finditer(r"([^.!?;]+)([.!?;]?)", text):
        part = match.group(1).strip(" -")
        if part:
            # A heading can carry over a list separated with a semicolon, but
            # it must not turn a later sentence's factual observation into a
            # preference.
            parts.append((part, match.group(2) == ";"))
    return parts


def _explicit_global(text: str) -> bool:
    low = text.casefold()
    return any(
        token in low for token in ("никогда", "безусловный", "какими бы ни", "never")
    )


def _polarity(text: str) -> str | None:
    """Read only an explicit local preference from an owner phrase.

    A listing verdict describes that listing as a whole.  It cannot turn every
    fact mentioned beside it into a durable dislike (or every fact on an
    interested listing into a like).  Mixed wording is useful evidence, but
    not an executable rule until the owner says which half applies.
    """
    low = text.casefold()
    tolerated = any(token in low for token in ("терпим", "терпимо", "готов мириться"))
    negated_desire = bool(
        re.search(
            r"\bне\s+(?:\w+\s+){0,2}"
            r"(?:хочу|хоч\w*|люблю|предпочита\w*|нравит\w*|нравят\w*|"
            r"подходит\w*|подходят\w*)",
            low,
        )
    )
    negative = (
        any(
            token in low
            for token in (
                "не нравится",
                "не подходит",
                "минус",
                "слишком",
                "маленьк",
                "сырост",
                "заброш",
                "разруш",
                "реконструк",
            )
        )
        or negated_desire
    )
    positive = (
        any(
            token in low
            for token in ("нравит", "предпочита", "люблю", "хочу", "подходит")
        )
        and "не нравится" not in low
        and "не подходит" not in low
        and not negated_desire
    )
    if tolerated and negative:
        return "tradeoff"
    if positive == negative:
        return None
    return "prefer" if positive else "avoid"


def _section_polarity(text: str, verdict: Any) -> str | None:
    """Recognise an owner-written heading whose meaning carries forward."""
    low = text.casefold().lstrip()
    if re.match(r"(?:нравится|предпочитаю|люблю)\s*:", low):
        return "prefer"
    if re.match(r"терпимые?\s+минусы?\s*:", low):
        return "tradeoff"
    if re.match(r"минусы?\s*:", low) and verdict == "interested":
        return "tradeoff"
    return None


def _intrinsic_avoid(text: str) -> bool:
    """A small set of phrases that state the negative preference themselves."""
    low = text.casefold()
    return any(
        token in low
        for token in (
            "не изолирован",
            "не красив",
            "чужие дома близко",
            "далеко от пляж",
            "возле дороги",
            "рядом с дорог",
            "нет земли рядом",
            "разруш",
            "реконструк",
            "маленькие комнат",
            "участок, а не дом",
            "это участок, а не дом",
            "сх постройки",
        )
    )


def _preference_segments(segment: str) -> list[str]:
    """Split an explicit contrast before mapping aspects to a polarity."""
    low = segment.casefold()
    if any(token in low for token in ("терпим", "терпимо", "готов мириться")) and any(
        token in low for token in ("минус", "сырост", "заброш", "разруш", "реконструк")
    ):
        # "Damp is a minus, but tolerable" is one local tradeoff.  Splitting
        # it would detach the tolerance from the only fact it qualifies.
        return [segment]
    parts = [
        part.strip(" ,—-:")
        for part in re.split(
            r"\s*(?:,?\s+но\s+|;\s*но\s+|\bоднако\b)\s*", segment, flags=re.IGNORECASE
        )
    ]
    return [part for part in parts if part]


def _clause_id(property_id: int, aspect_id: str | None, text: str) -> str:
    raw = f"{property_id}\0{aspect_id or 'unmapped'}\0{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def compile_signal(signal: dict[str, Any]) -> list[dict[str, Any]]:
    reason = str(signal.get("reason") or "").strip()
    if not reason:
        return []
    result: list[dict[str, Any]] = []
    active_polarity: str | None = None
    for segment, carries_heading in _segments(reason):
        low = segment.casefold()
        heading_polarity = _section_polarity(segment, signal.get("verdict"))
        if heading_polarity:
            active_polarity = heading_polarity
        if "за 300" in low or "за300" in low:
            result.append(
                {
                    "id": _clause_id(
                        signal["property_id"], "budget_condition", segment
                    ),
                    "aspect_id": "budget_condition",
                    "text": segment,
                    "source_property_id": signal["property_id"],
                    "source_profile_id": signal.get("profile_id"),
                    "polarity": "avoid",
                    "strength": "unresolved",
                    "scope": "profile",
                    "mapping_state": "unresolved_condition",
                    "reason": "numeric budget predicate is not explicit",
                }
            )
        matched_any = False
        parts = _preference_segments(segment)
        for part in parts:
            part_low = part.casefold()
            matched = [
                aspect
                for aspect, needles in _RULES
                if any(needle in part_low for needle in needles)
            ]
            matched_any = matched_any or bool(matched)
            explicit_polarity = _polarity(part)
            if heading_polarity and len(parts) == 1:
                polarity = heading_polarity
            else:
                polarity = (
                    explicit_polarity
                    or ("avoid" if _intrinsic_avoid(part) else None)
                    or active_polarity
                )
            for aspect_id in matched:
                values = taste_descriptors.text_claim_values(part).get(aspect_id, [])
                hard = polarity == "avoid" and _explicit_global(part)
                result.append(
                    {
                        "id": _clause_id(signal["property_id"], aspect_id, part),
                        "aspect_id": aspect_id,
                        "text": part,
                        "source_property_id": signal["property_id"],
                        "source_profile_id": signal.get("profile_id"),
                        "polarity": polarity or "unresolved",
                        "strength": "hard" if hard else "soft",
                        "scope": "global" if hard else "profile",
                        "values": values,
                        "mapping_state": "executable" if polarity else "unmapped",
                        "reason": None
                        if polarity
                        else "preference polarity is not explicit",
                    }
                )
        if not matched_any and "за 300" not in low and "за300" not in low:
            polarity = (
                _polarity(segment)
                or ("avoid" if _intrinsic_avoid(segment) else None)
                or active_polarity
            )
            result.append(
                {
                    "id": _clause_id(signal["property_id"], None, segment),
                    "aspect_id": None,
                    "text": segment,
                    "source_property_id": signal["property_id"],
                    "source_profile_id": signal.get("profile_id"),
                    "polarity": polarity or "unresolved",
                    "strength": "soft",
                    "scope": "profile",
                    "mapping_state": "unmapped",
                    "reason": "no supported aspect mapping"
                    if polarity
                    else "preference polarity is not explicit",
                }
            )
        if not carries_heading:
            active_polarity = None
    # The same aspect mentioned twice in one long segment is still one clause.
    unique: dict[tuple[str, str | None], dict[str, Any]] = {}
    for clause in result:
        unique[(clause["id"], clause["aspect_id"])] = clause
    return list(unique.values())


def compile_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for signal in signals:
        clauses.extend(compile_signal(signal))
    return clauses


def applies_to(clause: dict[str, Any], profile_id: int | None) -> bool:
    return (
        clause.get("scope") == "global" or clause.get("source_profile_id") == profile_id
    )
