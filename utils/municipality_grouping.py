"""Fold the spellings of one municipality onto one key.

`properties.municipality` is free text lifted out of Idealista alert emails,
and the same municipality arrives under several spellings. Measured against
the live database on 2026-08-16: "Gijón" (57 rows) beside "Gijon" (16),
"Castrillon" (28) beside "Castrillón" (18), "Muros De Nalón" / "Muros de
Nalon" / "Muros de Nalón" -- 247 rows across 8 municipalities, every one of
them split.

Both surfaces that grouped by that raw string therefore reported a partial
result as a complete one, which is #98's shape applied to a filter and to a
group-by. The /properties dropdown offered "Gijón" and "Gijon" as two
municipalities, so picking one showed 57 of 73 listings with nothing on the
page saying the other 16 existed; /municipalities rendered one place as two
rows, with two medians and two coverage counts.

The key is `utils.municipality_codes.normalize()` -- the function the INE
join already folds *both* sides of its lookup with (NFKD, combining marks
dropped, casefold, articles moved out of the way). This module adds only what
grouping needs on top of it: which values may be grouped at all, and which
spelling of a group a human should be shown.

Nothing is canonicalised on write, and no key is stored. `Property.
municipality` keeps the exact string the email carried: it is the input the
#298 truncation repair reads, and the only record of what Idealista actually
said. A derived column would have to be maintained by every writer -- the
IMAP pipeline, the legacy `Land` mirror, the repair tools -- and a writer
that forgot would leave rows invisible to their own filter, silently, which
is the defect this module exists to remove rather than relocate. The key is
derived where it is needed instead; the table is small enough that the
grouping runs off a `DISTINCT` in a few milliseconds.

A truncated artifact ("Ovi...", issue #298) has no group. `normalize("Ovi...")`
is "ovi", which is nobody's key, and folding it into "oviedo" by prefix is
exactly the wrong-pick hazard `resolve_truncated_municipality` refuses -- the
stem could as easily be Oviñana. It stays out of the dropdown, out of
/municipalities, and out of every group's row set; the repair tool is what
turns it into a real name.
"""

import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from utils.idealista_extractors import is_truncated_municipality
from utils.municipality_codes import normalize

# Words Spanish and Galician place names keep lowercase inside the name
# ("Soto del Barco", "Corvera de Asturias", "Sada e Barrañán"). Used only to
# rank spellings for display: matching never sees them, because `normalize()`
# has already dropped everything that is not alphanumeric.
_DISPLAY_CONNECTIVES = frozenset(
    {"de", "del", "la", "las", "los", "el", "y", "e", "da", "do", "das", "dos"}
)


@dataclass(frozen=True)
class MunicipalityGroup:
    """One real municipality, and every stored spelling of it.

    `label` is what the owner reads, `spellings` is what a query matches on
    (the raw stored values, so an `IN` is exact), and `count` is the sum over
    all of them -- the number the dropdown was hiding.
    """

    key: str
    label: str
    count: int
    spellings: Tuple[str, ...]


def group_key(name: Optional[str]) -> Optional[str]:
    """The key every spelling of one municipality shares, or None.

    None means "this value names no municipality to group by": empty, a
    truncated email artifact, or a string that normalizes to nothing at all
    ("---"). Callers skip such rows rather than inventing a bucket for them,
    and the caller that counts them says so on the page.
    """
    text = str(name or "").strip()
    if not text or is_truncated_municipality(text):
        return None
    return normalize(text) or None


def _accents(name: str) -> int:
    """Combining marks in the name: "Gijón" has one, "Gijon" none."""
    return sum(
        1 for ch in unicodedata.normalize("NFKD", name) if unicodedata.combining(ch)
    )


def _lowercase_connectives(name: str) -> int:
    """Connectives written the way Spanish writes them, inside the name.

    Counted from the second word on, so a leading article keeps its capital
    ("El Franco" is not the shape this penalises).
    """
    return sum(1 for word in name.split()[1:] if word in _DISPLAY_CONNECTIVES)


def _shouted(name: str) -> bool:
    """Whether the spelling is all-caps ("MUROS DE NALON")."""
    letters = [ch for ch in name if ch.isalpha()]
    return bool(letters) and not any(ch.islower() for ch in letters)


def display_rank(name: str, count: int = 0) -> tuple:
    """Ranking key: the smallest is the spelling to show.

    In order -- an all-caps spelling loses to any other, then the one that
    kept its accents wins ("Castrillón" over the commoner "Castrillon":
    frequency is not authority about how a name is spelled), then the one
    that lowercases its connectives ("Muros de Nalón" over "Muros De
    Nalón"), and only then the commoner form. The name itself is the last
    tiebreak, so the choice never depends on dictionary or row order.
    """
    return (
        _shouted(name),
        -_accents(name),
        -_lowercase_connectives(name),
        -count,
        name,
    )


def preferred_display(counts: Mapping[str, int]) -> str:
    """The spelling of one municipality to put in front of the owner.

    Takes {stored spelling: row count}. Never invents a form: the answer is
    always one of the strings that is actually in the table, because a
    generated one ("MUROS DE NALON", or an accent restored by guesswork)
    would claim more than the data supports.
    """
    return min(counts, key=lambda name: display_rank(name, counts[name]))


def group_municipalities(
    rows: Iterable[Tuple[Optional[str], int]],
) -> List[MunicipalityGroup]:
    """Group `(stored municipality, row count)` pairs into real municipalities.

    Ordered by the canonical key, so "Avilés" sorts next to "Aviles" would
    have and the list does not jump around when the preferred spelling
    changes. Values with no group (see `group_key`) are dropped.
    """
    buckets: Dict[str, Dict[str, int]] = {}
    for name, count in rows:
        key = group_key(name)
        if key is None:
            continue
        bucket = buckets.setdefault(key, {})
        bucket[name] = bucket.get(name, 0) + int(count)

    groups = [
        MunicipalityGroup(
            key=key,
            label=preferred_display(counts).strip(),
            count=sum(counts.values()),
            spellings=tuple(sorted(counts)),
        )
        for key, counts in buckets.items()
    ]
    groups.sort(key=lambda group: (group.key, group.label))
    return groups


def municipality_filter_clause(value: str):
    """A filter matching one municipality however the row spells it.

    The four listing surfaces (/properties, /map, /properties/export.csv and
    the JSON /api/properties) share this so they cannot drift into four
    slightly different answers to "show me Gijón".

    A value naming a municipality the table holds becomes an exact `IN` over
    its stored spellings. A truncated row is never one of them -- `group_key`
    gives it no key, so selecting "Oviedo" does not quietly swallow "Ovi...",
    which stays explicitly non-canonical and the repair tool's to fix.

    Anything else keeps the substring match this filter has always had: a
    hand-typed prefix ("?municipality=Gij"), and the truncated artifacts
    themselves, which have to stay reachable by the literal value the
    dropdown puts back when one is applied.
    """
    from app import db
    from models import Property

    key = group_key(value)
    if key is not None:
        spellings = [
            stored
            for (stored,) in db.session.query(Property.municipality).distinct()
            if stored and group_key(stored) == key
        ]
        if spellings:
            return Property.municipality.in_(spellings)
    return Property.municipality.ilike(f"%{value}%")
