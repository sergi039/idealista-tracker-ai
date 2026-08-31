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

import json
import logging
import pathlib
import re
import unicodedata
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

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
# The third spelling of the same inversion. INE writes "Laracha, A" and
# idealista "A Laracha", both of which the two patterns above already fold;
# yaencontre writes "Laracha (A)", which folded to "laracha a" and therefore
# grouped as a municipality of its own. Production carried "Laracha (A)" (4
# rows) beside "Laracha" (1) before the parser change that made it common, so
# the split is older than that change. The brackets alone are not enough --
# "Gijón (address hidden)" is a real stored title and folding it would claim a
# municipality the row does not state -- so the content must be an article.
_PARENTHESISED_ARTICLE_RE = re.compile(r"\(\s*" + _ARTICLES + r"\s*'?\s*\)\s*$")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def normalize(name: str) -> str:
    """Fold a municipality name onto its join key.

    Lowercase, accents stripped (NFKD, combining marks dropped), whitespace
    and hyphens collapsed to single spaces, and the article removed from any
    of its three positions — "Franco, El", "El Franco", "Franco (El)" and
    "franco" all become "franco". Deterministic and side-agnostic: apply it to
    the INE name when building an index and to the portal name when looking
    one up.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    bare = _PARENTHESISED_ARTICLE_RE.sub("", bare.strip())
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


def match(
    name: str, index: Mapping[str, str], *, apply_aliases: bool = True
) -> Optional[str]:
    """Resolve a portal municipality name to a 5-digit INE code.

    `index` maps normalized INE names to codes (see `build_index`). Returns
    None when the name is unknown or the code falls outside the five watched
    provinces — the caller records `not_matched`. No fuzzy matching beyond
    the verified alias table: a silent wrong join is the failure mode this
    module exists to prevent.

    `apply_aliases=False` is for a name that did **not** come from the portal
    or from a reference dataset. `ALIASES` is verified for one direction only
    — what Idealista calls a council, mapped onto INE's name for it — and
    several of its source strings are real, independent places in their own
    right. "San Esteban" is the alias the portal uses for the capital of
    Muros de Nalón (33039), *and* a genuine parish of Morcín (33038); applied
    to a geocoder's answer it turns a correct result for Morcín into a code
    for a council 40 km away. The row side keeps the table, because that is
    the side it was measured on; a geocoding result is matched without it and
    an unrecognised name stays an honest miss (GEO-001).
    """
    if not name:
        return None
    key = normalize(name)
    if not key:
        return None
    if apply_aliases:
        key = ALIASES.get(key, key)
    code = index.get(key)
    if code is None or code[:2] not in PROVINCE_CODES:
        return None
    return code


# --- the shared name index, loaded once ------------------------------------

# `build_index` needs `{code: name}`, and the only place that mapping lives is
# the committed INE reference file. `QualityOfLifeService` builds its own from
# the same file because it is already holding that data for renta and
# población; this loader exists for callers that want nothing but the join —
# `PropertyLocationService`, which needs a province code to check that a
# geocoding result is about the row's own municipality (#348).
#
# Cached because it is read per property during ingestion and the file does not
# change under a running process. A missing or unreadable file yields an empty
# index, and `match()` against an empty index returns None for everything —
# which the caller must read as "cannot tell", never as a contradiction.
_INE_DATA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "ine_municipal.json"
)

_name_index_cache: Optional[Dict[str, str]] = None


def load_name_index() -> Dict[str, str]:
    """Normalized INE municipality name -> 5-digit code, built once."""
    global _name_index_cache
    if _name_index_cache is None:
        try:
            with open(_INE_DATA_PATH, encoding="utf-8") as handle:
                payload = json.load(handle)
            municipalities = (payload or {}).get("municipalities") or {}
            code_to_name = {
                code: (row or {}).get("name")
                for code, row in municipalities.items()
                if isinstance(row, dict) and (row or {}).get("name")
            }
            _name_index_cache = build_index(code_to_name)
        except Exception:
            # An absent reference file is "cannot tell", not a reason to refuse
            # every geocoding result on the machine.
            logger.warning(
                "INE reference file unusable at %s; municipality checks will "
                "report `cannot tell`",
                _INE_DATA_PATH,
                exc_info=True,
            )
            _name_index_cache = {}
    return _name_index_cache


# Province name -> two-digit INE province code, for the case where Google's
# answer carries no postal code (issue #371).
#
# Why every province and not the five this tracker watches. The check must be
# able to tell "a different province" (a contradiction, refuse) from "not a
# province at all" (cannot tell, accept and record). With only the watched five
# in the table, `Lleida` would be unrecognised and read as cannot-tell — which
# is the exact answer #348 exists to avoid giving. With all 52, a name that is
# still unrecognised really is not a province: measured on production, the one
# such value is `Galicia`, an autonomous community, and cannot-tell is the
# right answer for it.
#
# Keys are normalized by `normalize()`, so accents fold and a leading article
# is dropped: "A Coruña" and "La Coruña" both become "coruna", "Las Palmas"
# becomes "palmas", "La Rioja" becomes "rioja". The alternates listed here are
# the ones normalization cannot bridge on its own — a different word, not a
# different spelling of the same one ("Ourense"/"Orense", "Lleida"/"Lérida",
# "Bizkaia"/"Vizcaya").
_PROVINCE_NAMES: Dict[str, Tuple[str, ...]] = {
    "01": ("Álava", "Araba"),
    "02": ("Albacete",),
    "03": ("Alicante", "Alacant"),
    "04": ("Almería",),
    "05": ("Ávila",),
    "06": ("Badajoz",),
    "07": ("Baleares", "Illes Balears", "Islas Baleares"),
    "08": ("Barcelona",),
    "09": ("Burgos",),
    "10": ("Cáceres",),
    "11": ("Cádiz",),
    "12": ("Castellón", "Castelló"),
    "13": ("Ciudad Real",),
    "14": ("Córdoba",),
    "15": ("A Coruña", "La Coruña"),
    "16": ("Cuenca",),
    "17": ("Girona", "Gerona"),
    "18": ("Granada",),
    "19": ("Guadalajara",),
    "20": ("Gipuzkoa", "Guipúzcoa"),
    "21": ("Huelva",),
    "22": ("Huesca",),
    "23": ("Jaén",),
    "24": ("León",),
    "25": ("Lleida", "Lérida"),
    "26": ("La Rioja",),
    "27": ("Lugo",),
    "28": ("Madrid",),
    "29": ("Málaga",),
    "30": ("Murcia",),
    "31": ("Navarra", "Nafarroa"),
    "32": ("Ourense", "Orense"),
    "33": ("Asturias",),
    "34": ("Palencia",),
    "35": ("Las Palmas",),
    "36": ("Pontevedra",),
    "37": ("Salamanca",),
    "38": ("Santa Cruz de Tenerife",),
    "39": ("Cantabria",),
    "40": ("Segovia",),
    "41": ("Sevilla",),
    "42": ("Soria",),
    "43": ("Tarragona",),
    "44": ("Teruel",),
    "45": ("Toledo",),
    "46": ("Valencia", "València"),
    "47": ("Valladolid",),
    "48": ("Bizkaia", "Vizcaya"),
    "49": ("Zamora",),
    "50": ("Zaragoza",),
    "51": ("Ceuta",),
    "52": ("Melilla",),
}

_PROVINCE_CODE_BY_NAME: Dict[str, str] = {
    normalize(name): code for code, names in _PROVINCE_NAMES.items() for name in names
}


def province_code_for_name(name: str) -> Optional[str]:
    """Two-digit province code for a province name, or None if it is not one.

    None is a real answer and the caller must treat it as "cannot tell": an
    autonomous community, a region, a country or anything else that is not one
    of Spain's 52 provinces lands here, and none of those may contradict a
    municipality.
    """
    key = normalize(str(name or ""))
    return _PROVINCE_CODE_BY_NAME.get(key) if key else None
