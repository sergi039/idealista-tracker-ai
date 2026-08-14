"""Join Idealista municipality names to INE municipality codes.

Idealista's emails carry municipality names as the portal spells them
("Luarca - Valdés", "Castrillon", "Corvera De Asturias"); INE's dictionary
spells the same places its own way ("Valdés", "Castrillón", "Corvera de
Asturias"), and puts leading articles behind a comma ("Franco, El" for El
Franco, "Coruña, A" for A Coruña). `normalize()` folds both spellings onto one
key: casefold, strip accents, collapse whitespace and hyphens, and drop the
article whichever side of the name it sits on. Both sides of a join go through
the same function, so the key never needs to be pretty — only stable.

Where the portal uses a genuinely different name, no normalization recovers
it. `ALIASES` carries the known cases, verified against the live database on
2026-08-13 ("Villalba" is INE's "Vilalba", "Infiesto" is the capital of
Piloña, "San Esteban" the capital of Muros de Nalón, and so on). `match()`
applies the alias table after normalizing and otherwise refuses to guess:
an unknown name returns None so the caller can record `not_matched` — a wrong
code attached silently is worse than an honest miss.

Everything is restricted to the five provinces this tracker watches —
15 A Coruña, 27 Lugo, 32 Ourense, 33 Asturias, 36 Pontevedra. Country-wide
the names collide ("Mieres" exists in Girona too), so a code from any other
province is treated as no match.
"""

import re
import unicodedata
from typing import Dict, Mapping, Optional

# The provinces the tracker watches. Codes are the first two digits of the
# 5-digit INE municipality code.
PROVINCE_CODES = frozenset({"15", "27", "32", "33", "36"})

# Leading/trailing articles INE moves behind a comma: Spanish (El Franco ->
# "Franco, El") and Galician (A Coruña -> "Coruña, A"). "l" covers the elided
# Catalan "L'" so a stray name never crashes the regex path; no municipality
# in the five provinces uses it.
_ARTICLES = r"(?:el|la|los|las|a|o|as|os|l)"
_TRAILING_ARTICLE_RE = re.compile(r",\s*" + _ARTICLES + r"\s*'?\s*$")
_LEADING_ARTICLE_RE = re.compile(r"^" + _ARTICLES + r"[\s']+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def normalize(name: str) -> str:
    """Fold a municipality name onto its join key.

    Lowercase, accents stripped (NFKD, combining marks dropped), whitespace
    and hyphens collapsed to single spaces, and the article removed from
    either position — "Franco, El", "El Franco" and "franco" all become
    "franco". Deterministic and side-agnostic: apply it to the INE name when
    building an index and to the Idealista name when looking one up.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    bare = _TRAILING_ARTICLE_RE.sub("", bare.strip())
    bare = _NON_ALNUM_RE.sub(" ", bare).strip()
    bare = _LEADING_ARTICLE_RE.sub("", bare)
    return re.sub(r"\s+", " ", bare).strip()


# Portal name -> INE name, for the cases normalization cannot bridge.
# Verified against the live database on 2026-08-13: these exact portal
# spellings appear on stored listings, and each right-hand side is the INE
# dictionary name of the same place. Keep this table small and verified —
# it is the only fuzziness `match()` allows itself.
_RAW_ALIASES = {
    "villalba": "vilalba",
    "mieres del camino": "mieres",
    "luarca - valdés": "valdés",
    "infiesto": "piloña",
    "san esteban": "muros de nalón",
}

ALIASES: Dict[str, str] = {normalize(k): normalize(v) for k, v in _RAW_ALIASES.items()}


def build_index(code_to_name: Mapping[str, str]) -> Dict[str, str]:
    """Invert a {code: INE name} mapping into {normalized name: code}.

    Codes outside the five watched provinces are dropped. Two in-scope codes
    normalizing to the same key would make every later match of that key a
    coin flip, so that raises instead of picking one silently.
    """
    index: Dict[str, str] = {}
    for code, name in code_to_name.items():
        if code[:2] not in PROVINCE_CODES:
            continue
        key = normalize(name)
        if not key:
            raise ValueError(
                f"Municipality {code} normalizes to an empty key: {name!r}"
            )
        if key in index and index[key] != code:
            raise ValueError(
                f"Normalized name collision: {key!r} maps to both {index[key]} and {code}"
            )
        index[key] = code
    return index


def match(name: str, index: Mapping[str, str]) -> Optional[str]:
    """Resolve a portal municipality name to a 5-digit INE code.

    `index` maps normalized INE names to codes (see `build_index`). Returns
    None when the name is unknown or the code falls outside the five watched
    provinces — the caller records `not_matched`. No fuzzy matching beyond
    the verified alias table: a silent wrong join is the failure mode this
    module exists to prevent.
    """
    if not name:
        return None
    key = normalize(name)
    if not key:
        return None
    key = ALIASES.get(key, key)
    code = index.get(key)
    if code is None or code[:2] not in PROVINCE_CODES:
        return None
    return code
