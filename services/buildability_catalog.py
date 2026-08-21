"""What may be said about building rules, and which of it is local.

The single owner of the policy (the shape `PlaceRules` set): every topic the
/construccion page can render is declared here, and only here.  A topic marked
`requires_local_confirmation=True` may show a per-concejo value ONLY when a
record with `state: "present"` exists in that concejo's file under
`reference/legal/concejos/` — no fallback, ever.  The regional default is a
different statement about a different subject (the region's statute, with its
citation) and lives in `regional_statute`, rendered as its own row by the
injection slot and never as the concejo's value.

`forbidden_patterns` are the content lint: regexes over the regional chapter
bodies that catch a topic's concrete value leaking into free prose, where it
would read as checked for whichever concejo is selected.  The chapters must
describe the *mechanism* and point at the slot; the numbers live here.

Design history: seven review rounds (2026-08-21), verdict PASS WITH FIXES.
The absence-never-renders-as-fact rule is CLAUDE.md's #98.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Topic:
    label_key: str
    requires_local_confirmation: bool
    # (statement, citation) — the regional statute row of the two-row slot.
    # Mandatory for local topics: the slot must be able to say what the
    # regional frame is without borrowing the concejo's row to say it.
    regional_statute: Optional[tuple] = None
    # Allowed values for a `present` record. A tuple of strings is an enum;
    # a (lo, hi) tuple of numbers is an inclusive range.
    value_domain: tuple = ()
    stale_after_days: int = 365
    mandatory: bool = False
    chapter_anchor: Optional[str] = None
    forbidden_patterns: tuple = field(default_factory=tuple)


TOPICS = {
    "pgo_status": Topic(
        label_key="concejo_topic_pgo",
        requires_local_confirmation=True,
        regional_statute=(
            "PGO aprobados 15.07.2008–22.11.2022 sin adaptar: régimen ROTU "
            "de aplicación directa desde 23.11.2026",
            "Decreto 63/2022, DT 1",
        ),
        value_domain=("approved", "in_revision", "nnss", "none"),
        stale_after_days=1825,
        mandatory=True,
        chapter_anchor=("01-permisos", "pgo"),
        forbidden_patterns=(r"PGO\s+(?:aprobado|принят)\s+en\s+\d{4}",),
    ),
    "cedula_regime": Topic(
        label_key="concejo_topic_cedula",
        requires_local_confirmation=True,
        regional_statute=(
            "La cédula existe donde el municipio la creó por ordenanza; "
            "en su defecto se expide certificado urbanístico",
            "TROTU art. 24.1–24.2",
        ),
        value_domain=("cedula", "certificado_only"),
        stale_after_days=730,
        mandatory=True,
        chapter_anchor=("02-certificado", "cedula-regime"),
        forbidden_patterns=(r"(?:sólo|solo|только)\s+certificado\b",),
    ),
    "coastal_pola": Topic(
        label_key="concejo_topic_coastal",
        requires_local_confirmation=True,
        regional_statute=(
            "En la franja litoral la vivienda nueva queda excluida salvo "
            "núcleo rural delimitado",
            "TROTU art. 133–135 / POLA-PESC",
        ),
        value_domain=("coastal", "not_coastal"),
        stale_after_days=1825,
        mandatory=True,
        chapter_anchor=("04-sectoriales", "costas"),
        forbidden_patterns=(
            r"(?:es|является)\s+(?:un\s+)?concejo\s+(?:costero|прибрежн\w+)",
        ),
    ),
    "silence_period_months": Topic(
        label_key="concejo_topic_silence",
        requires_local_confirmation=True,
        regional_statute=(
            "3 meses por defecto; la ordenanza municipal puede ampliar "
            "hasta 6. El silencio para obra nueva de vivienda es negativo",
            "ROTU art. 316",
        ),
        value_domain=(1, 6),
        stale_after_days=730,
        mandatory=True,
        chapter_anchor=("05-licencia", "silencio"),
        forbidden_patterns=(r"[36]\s*(?:мес(?:яц\w*)?|mes(?:es)?)\b",),
    ),
    "first_occupation_regime": Topic(
        label_key="concejo_topic_occupation",
        requires_local_confirmation=True,
        regional_statute=(
            "Declaración responsable de primera utilización y ocupación; "
            "la cédula de habitabilidad de primera ocupación está suprimida",
            "ROTU art. 312 / Decreto 73/2018",
        ),
        value_domain=("declaracion_responsable", "licencia"),
        stale_after_days=730,
        mandatory=False,
        chapter_anchor=("06-final", "primera-ocupacion"),
        forbidden_patterns=(
            r"licencia de primera ocupación\s+(?:obligatoria|обязательн\w+)",
        ),
    ),
    "icio_rate_pct": Topic(
        label_key="concejo_topic_icio",
        requires_local_confirmation=True,
        regional_statute=(
            "Tipo municipal sobre el coste de ejecución material, con el "
            "máximo estatal del 4%",
            "RDL 2/2004 art. 102",
        ),
        value_domain=(0.0, 4.0),
        stale_after_days=365,
        mandatory=False,
        chapter_anchor=("07-impuestos", "icio"),
        forbidden_patterns=(r"ICIO[^.]{0,40}\d(?:[.,]\d+)?\s*%",),
    ),
}


def mandatory_topics() -> list:
    """Non-empty by invariant: `all([])` would announce a completeness nobody
    established, so the test suite fails the build if this list is empty."""
    return [key for key, topic in TOPICS.items() if topic.mandatory]


def local_topics() -> list:
    return [key for key, topic in TOPICS.items() if topic.requires_local_confirmation]


def topics_for_chapter(chapter_id: str) -> list:
    return [
        key
        for key, topic in TOPICS.items()
        if topic.chapter_anchor and topic.chapter_anchor[0] == chapter_id
    ]
