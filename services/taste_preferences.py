"""Compile owner comments into constrained, source-attributed preference clauses."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from services import taste_descriptors

# Bump when compiled clauses change meaning. Published profiles record this
# value and become dirty until the one-call recommendation refresh rebuilds
# their deterministic clause snapshot.
SCHEMA_VERSION = 2

_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "plot_outline",
        (
            "l-shaped",
            "l-образ",
            "изломан",
            "перешеек",
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


_DESIRE = re.compile(
    r"\b(?:хочу|хоч\w*|люблю|предпочита\w*|нравит\w*|нравят\w*|"
    r"подходит\w*|подходят\w*)"
)
_NEGATION_CONNECTORS = frozenset(
    {
        "то",
        "что",
        "так",
        "уж",
        "и",
        "очень",
        "особо",
        "совсем",
        "вовсе",
        "совершенно",
    }
)


def _desire_negation(text: str) -> str | None:
    """Classify a negation that appears before a desire in one clause.

    A bare modifier/connector chain expresses a negated desire even when it
    crosses commas or hyphens (``не так уж и нравится``).  When a noun or
    another substantive word sits between ``не`` and the desire, assigning a
    preference would guess at its grammatical target; leave that unresolved.
    """
    low = text.casefold()
    for desire in _DESIRE.finditer(low):
        prefix = low[: desire.start()]
        negation = list(re.finditer(r"\bне\b", prefix))
        if not negation:
            continue
        between = prefix[negation[-1].end() :]
        words = re.findall(r"\w+", between)
        if all(word in _NEGATION_CONNECTORS for word in words):
            return "avoid"
        return "unresolved"
    for desire in _DESIRE.finditer(low):
        # ``нравится не каменный дом, а деревянный`` negates the desired
        # object, not the desire verb.  We cannot safely infer which of the
        # two objects is preferred, so it must not become a preference for
        # the first value named.
        suffix = low[desire.end() :]
        if re.match(r"[\s,:—-]*\bне\b", suffix):
            return "unresolved"
    return None


def _polarity(text: str) -> str | None:
    """Read only an explicit local preference from an owner phrase.

    A listing verdict describes that listing as a whole.  It cannot turn every
    fact mentioned beside it into a durable dislike (or every fact on an
    interested listing into a like).  Mixed wording is useful evidence, but
    not an executable rule until the owner says which half applies.
    """
    low = text.casefold()
    tolerated = any(token in low for token in ("терпим", "терпимо", "готов мириться"))
    negated_desire = _desire_negation(text)
    if negated_desire == "unresolved":
        return "unresolved"
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
        or negated_desire == "avoid"
    )
    positive = (
        any(
            token in low
            for token in ("нравит", "предпочита", "люблю", "хочу", "подходит")
        )
        and "не нравится" not in low
        and "не подходит" not in low
        and negated_desire is None
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


def _heading_yields_to_local_signal(
    part: str, explicit_polarity: str | None, heading_polarity: str
) -> bool:
    """Whether a section heading must not supply this part's polarity.

    Headings label a list; they cannot rewrite an owner statement that carries
    its own sign.  Generic factual negatives remain eligible for a genuine
    ``Минусы:`` tradeoff heading, while negated desires and intrinsic drawbacks
    stay local.
    """
    if explicit_polarity in {"unresolved", "tradeoff"}:
        return True
    # Under a positive heading, a locally negative phrase is still local.  A
    # negative heading may intentionally frame ordinary drawbacks as tradeoffs,
    # so that case remains below unless it is one of the stronger signals.
    if heading_polarity == "prefer" and explicit_polarity == "avoid":
        return True
    negation = _desire_negation(part)
    return (
        negation in {"avoid", "unresolved"}
        or _intrinsic_avoid(part)
        or "не нравится" in part.casefold()
        or "не подходит" in part.casefold()
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
    parts: list[str] = []
    for contrast_part in re.split(
        r"\s*(?:,?\s+но\s+|;\s*но\s+|\bоднако\b)\s*",
        segment,
        flags=re.IGNORECASE,
    ):
        # Commas can join distinct owner clauses.  Keeping them local stops a
        # heading from assigning one polarity to every aspect in the sentence;
        # a comma in a decimal number is left intact.
        parts.extend(re.split(r",\s+(?!(?:что|а\s+не)\b)", contrast_part))
    parts = [part.strip(" ,—-:") for part in parts]
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
            explicit_unresolved = explicit_polarity == "unresolved"
            if explicit_unresolved:
                polarity = None
            elif heading_polarity and not _heading_yields_to_local_signal(
                part, explicit_polarity, heading_polarity
            ):
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
                # Multiple values of one aspect need an explicit relationship
                # before they can be executed. Treating "stone farmhouse" as
                # either stone OR farmhouse made a partial photo observation
                # satisfy the whole owner phrase; silently treating it as AND
                # would be another grammatical guess. Keep the clause visible
                # until that relationship is represented in the clause schema.
                executable = bool(polarity) and len(values) == 1
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
                        "mapping_state": "executable" if executable else "unmapped",
                        "reason": None
                        if executable
                        else (
                            "desire negation is ambiguous"
                            if explicit_unresolved
                            else "multiple canonical values need an explicit relationship"
                            if len(values) > 1
                            else "no comparable canonical value"
                            if polarity
                            else "preference polarity is not explicit"
                        ),
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
