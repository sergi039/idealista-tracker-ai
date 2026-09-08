"""Which cadastral parcel lies under a coordinate, and what it is numbered (#559).

`services/address_agreement.py` asks whether Google answered the address that
was asked. It can only read the two strings the record already holds, and it
says so: a ROOFTOP that *does* name the number asked keeps `precise`, and
`services/coordinate_quality.py` grants `precise` zero slack -- sea distance,
drive times and the hazard band are all scored as if the coordinate were the
parcel. Nothing in the table can say what that zero is worth, because 0 of the
kept rows carries a second coordinate to check against. The one row anybody
ever checked by hand (360) was 2868 m out.

**This module is the second coordinate, and it is free.** Catastro's
`Consulta_RCCOOR_Distancia` answers, for one point, which parcels lie at or
near it -- each with its cadastral street and house number and its distance in
metres. So the question "is Google's ROOFTOP on the parcel whose number the
query asked for?" costs exactly one keyless request per row.

**The house number is read from the structured field, not from the sentence.**
The answer carries both: `ldt` is a free-text line built for a human --
`"CL HIGUERAS 25 ROJALES (ALICANTE)"`, but also
`"AL ORBON 14 Poligono 64 Parcela 20014 D017014 00TP62C ORBON. CASTRILLON
(ASTURIAS)"` and `"CL CALZADA,LA 6(A) LLANES (ASTURIAS)"` -- and `dt.lourb.dir`
carries `pnp`, the *numero de policia*, as its own field. A street named
"8 de Marzo" and a parcel numbered 8 are indistinguishable in the sentence and
distinct in the field, so the field is what is read; `ldt` is kept beside it as
the evidence a person reads, never parsed for the number.

**`pnp` of `-1` means the parcel has no street number**, which is how rustic
parcels come back (`"Poligono 91 Parcela 107 LES MATES. SIERO"`). That is an
absence, not a number, and it is reported as one -- #98's rule in a field that
would otherwise contribute a plausible-looking integer.

**The neighbour list has a radius, and a lower bound is labelled as one.**
Measured on production coordinates 2026-09-08, the endpoint returned between 1
and 11 parcels and never one further than about 25 m. So when the asked number
is among them the displacement is *measured*; when it is not, all that has been
established is that the asked parcel is further away than the furthest parcel
returned. Those two are different findings and this module never collapses them
into one, because a slack band derived from lower bounds read as measurements
would be a band that fits the sample and not the world.

**Nothing here trusts an HTTP status**, for the same reason
`services/cadastre_service.py` does not: every Catastro error arrives as
`200 OK` with the failure in the body. And nothing here retries: `max_attempts=1`
on the shared `CATASTRO_GATE`, because Catastro publishes no rate limit and does
document an approximately ten-day IP ban for abuse.

The states are `cadastre_service`'s own, imported rather than restated, so a
caller that already reads one reader's verdicts reads this one's.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from services.cadastre_service import (
    CATASTRO_GATE,
    MALFORMED,
    NOT_FOUND,
    OK,
    REFUSED,
    REQUEST_TIMEOUT_S,
    UNAVAILABLE,
    CadastreError,
)
from utils.http import HTTP_USER_AGENT, request_with_retries

logger = logging.getLogger(__name__)

RCCOOR_DISTANCIA_URL = (
    "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
    "COVCCoordenadas.svc/json/Consulta_RCCOOR_Distancia"
)

# The CRS the listings' coordinates are in. Catastro reprojects on request and
# echoes the SRS back in every answer, which is what `_read_point` checks.
WGS84 = "EPSG:4326"

# Catastro's own "this parcel has no street number". It is not a number and it
# is never returned as one.
NO_STREET_NUMBER = "-1"

# --- the verdicts -----------------------------------------------------------

# The coordinate is inside the parcel that carries the number the query asked
# for. The remaining error is the size of that parcel, which is the smallest
# thing this method can say.
ON_ASKED_PARCEL = "on_asked_parcel"

# The coordinate is inside some other parcel, and the asked number is among the
# neighbours the answer returned: `distance_m` is a measurement.
DISPLACED = "displaced"

# The asked number is not among the neighbours, so it is further away than the
# furthest one returned. `at_least_m` is a lower bound and nothing more.
BEYOND_NEIGHBOURS = "beyond_neighbours"

# The parcel under the coordinate carries the number that was asked, but its
# cadastral street shares no word with the street that was asked. A cannot-tell,
# and it exists because the first run of this measurement over production
# reported row 25 as `on_asked_parcel`: "calle Tarancon, 6" was answered on
# "Av. de Salamanca, 6", and a comparison on the digits alone called the
# neighbouring street's number 6 a match. That is #535's second disclosed blind
# spot arriving inside the tool built to bound the first, and counting it as an
# agreement would bias the whole sample towards "the ROOFTOP landed right".
NUMBER_MATCHED_OTHER_STREET = "number_matched_other_street"

# The query named no house number, so there is nothing to look for. The same
# cannot-tell `address_agreement.NUMBER_NOT_ASKED` reports, kept separate from
# every finding rather than folded into the negative one.
NO_NUMBER_ASKED = "no_number_asked"

# Catastro answered and there is no parcel at or near the point -- open sea,
# a road, or a coordinate outside Spain.
NO_PARCEL = "no_parcel"


def _get(url: str, params: Dict[str, str]) -> requests.Response:
    """One request. One attempt. The retry is a person asking again."""
    return request_with_retries(
        requests.get,
        url,
        params=params,
        headers={"User-Agent": HTTP_USER_AGENT},
        timeout=REQUEST_TIMEOUT_S,
        max_attempts=1,
        gate=CATASTRO_GATE,
        logger=logger,
    )


def _fetch_json(url: str, params: Dict[str, str]) -> Dict[str, Any]:
    """A Catastro JSON answer, or a `CadastreError` naming what went wrong.

    The HTTP status is checked and then not believed: every Catastro refusal
    arrives as `200 OK` with the failure in the body, so a 200 is a necessary
    condition for an answer and never a sufficient one.
    """
    try:
        response = _get(url, params)
    except requests.RequestException as exc:
        raise CadastreError(UNAVAILABLE, str(exc)) from exc
    if response.status_code != 200:
        raise CadastreError(REFUSED, f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise CadastreError(MALFORMED, f"not JSON: {exc}") from exc


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def street_number(entry: Any) -> Optional[str]:
    """The *numero de policia* of one returned parcel, or `None`.

    Read from `dt.lourb.dir.pnp` and from nowhere else. `-1` is Catastro's way
    of saying the parcel has no street number -- every rustic parcel comes back
    that way -- and an absence is returned as an absence.
    """
    if not isinstance(entry, dict):
        return None
    address = entry.get("dt")
    if not isinstance(address, dict):
        return None
    urban = address.get("lourb")
    if not isinstance(urban, dict):
        return None
    direction = urban.get("dir")
    if not isinstance(direction, dict):
        return None
    raw = direction.get("pnp")
    token = str(raw).strip() if raw is not None else ""
    if not token or token == NO_STREET_NUMBER:
        return None
    return token


def _location_codes(entry: Any) -> Tuple[Optional[str], Optional[str]]:
    """Catastro's province and municipality codes for one returned parcel.

    From `dt.loine`, and never from the `ldt` sentence. An earlier draft read
    the municipality out of the trailing `"MUNICIPIO (PROVINCIA)"` and got it
    wrong twice on the first four real addresses tried: the prefix swallowed the
    street ("CL HIGUERAS 25 ROJALES"), and a parish in its own parentheses
    ("GOZON (LUANCO) (ASTURIAS)") defeated the pattern entirely. The codes are
    two fields.
    """
    if not isinstance(entry, dict):
        return None, None
    address = entry.get("dt")
    if not isinstance(address, dict):
        return None, None
    codes = address.get("loine")
    if not isinstance(codes, dict):
        return None, None
    province = str(codes.get("cp") or "").strip() or None
    municipality = str(codes.get("cm") or "").strip() or None
    return province, municipality


def _reference(entry: Any) -> Optional[str]:
    """The 14-character cadastral reference, from its two halves."""
    if not isinstance(entry, dict):
        return None
    parcel = entry.get("pc")
    if not isinstance(parcel, dict):
        return None
    first = str(parcel.get("pc1") or "").strip()
    second = str(parcel.get("pc2") or "").strip()
    joined = f"{first}{second}"
    return joined or None


def _parse(payload: Any) -> List[Dict[str, Any]]:
    """The parcels in one answer, nearest first, or a `CadastreError`.

    The shape is
    `Consulta_RCCOOR_DistanciaResult -> coordenadas_distancias -> coordd[0] ->
    lpcd[]`, and every level of it is checked rather than indexed, because the
    error body arrives under the same top-level key with `control.cuerr` set.
    """
    if not isinstance(payload, dict):
        raise CadastreError(MALFORMED, "answer is not an object")
    result = payload.get("Consulta_RCCOOR_DistanciaResult")
    if not isinstance(result, dict):
        raise CadastreError(MALFORMED, "no Consulta_RCCOOR_DistanciaResult")

    # This endpoint has no answered-absence code: measured 2026-09-08, a point
    # in the Atlantic and a point in Paris both come back `cucoor: 1` with the
    # coordinate echoed and **no `lpcd` key at all**. So an absence is an empty
    # answer, handled below, and any error body here is a refused or malformed
    # request rather than "there is nothing there".
    error = _error_in(result, absence_codes=frozenset())
    if error:
        raise CadastreError(error["status"], error["detail"])

    block = result.get("coordenadas_distancias")
    if not isinstance(block, dict):
        raise CadastreError(MALFORMED, "no coordenadas_distancias")
    coords = block.get("coordd")
    if not isinstance(coords, list) or not coords:
        raise CadastreError(MALFORMED, "no coordd")
    first_coord = coords[0]
    if not isinstance(first_coord, dict):
        raise CadastreError(MALFORMED, "coordd[0] is not an object")

    entries = first_coord.get("lpcd")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise CadastreError(MALFORMED, "lpcd is not a list")

    parcels: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        distance = _as_float(entry.get("dis"))
        if distance is None or distance < 0:
            # A parcel with no readable distance cannot take part in either
            # verdict -- neither the measurement nor the lower bound -- so it
            # is dropped rather than defaulted to zero.
            continue
        codes = _location_codes(entry)
        parcels.append(
            {
                "reference": _reference(entry),
                "number": street_number(entry),
                "address": str(entry.get("ldt") or "").strip() or None,
                "distance_m": distance,
                "province_code": codes[0],
                "municipality_code": codes[1],
            }
        )
    parcels.sort(key=lambda item: item["distance_m"])
    return parcels


# The error codes that mean Catastro looked and there is nothing, rather than
# that the request was wrong. Measured against the live service on 2026-09-08:
# `33` is "LA VIA NO EXISTE" and `43` is "EL NUMERO NO EXISTE", both answers
# about the address asked for. Everything else -- `12` for an unknown province,
# `3` for an unknown reference -- says the request was malformed, which is an
# absence of measurement and never a fact about the listing (#98).
#
# The set is passed in rather than read from module scope because it is not the
# same for both endpoints: `Consulta_RCCOOR_Distancia` reports "no parcel here"
# by omitting the parcel list, and has no absence code at all.
ADDRESS_ABSENCE_CODES = frozenset({"33", "43"})


def parcels_at(lat: float, lon: float) -> Dict[str, Any]:
    """The cadastral parcels at and around one coordinate.

    Returns `{"status": ok, "parcels": [...]}` nearest first, or a status that
    is an absence of measurement. Only `not_found` means Catastro looked and
    there is nothing there.
    """
    try:
        latitude, longitude = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"status": MALFORMED, "detail": f"not a coordinate: {lat!r},{lon!r}"}

    params = {"SRS": WGS84, "CoorX": f"{longitude:.7f}", "CoorY": f"{latitude:.7f}"}
    try:
        parcels = _parse(_fetch_json(RCCOOR_DISTANCIA_URL, params))
    except CadastreError as exc:
        return {"status": exc.state, "detail": exc.detail}

    if not parcels:
        return {"status": NOT_FOUND, "detail": "no parcel at or near the coordinate"}
    return {"status": OK, "parcels": parcels}


def cadastral_street(address: Any) -> str:
    """The street name out of a cadastral `ldt`, normalised, or `""`.

    `ldt` is written `"<SIGLA> <STREET> <NUMBER> <place> (<PROVINCE>)"`, so the
    street is what stands between the two-letter sigla and the first bare
    number. The number may carry a duplicate suffix -- `"7(D)"`, `"6(A)"` --
    which `normalize_street` has already split into two words by the time this
    looks, so the digits terminate the name on their own.

    This is the free-text parse the module's header says the *house number*
    must never come from, and the difference is what the result is allowed to
    do: a number read wrongly here would become a measurement, whereas this
    feeds only `_shares_a_word`, whose sole effect is to demote an agreement to
    a cannot-tell. Its failure mode is a row a person has to look at, which is
    the safe direction.
    """
    words = normalize_street(address).split()
    if len(words) > 1 and len(words[0]) <= 2:
        words = words[1:]  # the sigla: CL, AV, LG, RU, PZ, BO, AL, CR, CM, CS
    kept = []
    for word in words:
        if word.isdigit():
            break
        kept.append(word)
    return " ".join(kept)


def _shares_a_word(one: str, other: str) -> bool:
    """Do two street names have any word in common?

    Deliberately the weakest test that can separate a spelling from a different
    street. `services/address_agreement.py` refuses to compare street names at
    all because 13 of its 36 token mismatches were spelling -- but every one of
    those spellings ("Rbla. de la Llibertat" against "RAMBLA DE LA LLIBERTAT")
    keeps at least one word, while "TARANCON" and "SALAMANCA DE" keep none. So
    a shared word is treated as agreement and only a complete absence of one is
    allowed to raise a doubt, which is why this can never produce a negative
    finding on its own.
    """
    if not one or not other:
        return True
    left = set(one.split()) - _STREET_STOPWORDS
    right = set(other.split()) - _STREET_STOPWORDS
    if not left or not right:
        # Nothing but articles on one side: there is no evidence either way,
        # and the quiet answer is the conservative one.
        return True
    return bool(left & right)


def compare_to_asked_number(
    parcels: List[Dict[str, Any]],
    asked: Optional[str],
    asked_street: Optional[str] = None,
) -> Dict[str, Any]:
    """What the parcels around a coordinate say about the number that was asked.

    The four findings are kept apart because they are worth different things to
    a slack band: `on_asked_parcel` bounds the error by the size of a parcel,
    `displaced` is a metre measurement, `beyond_neighbours` is a lower bound
    that says nothing about how much further, and `no_number_asked` is not a
    finding at all.

    The comparison is on the digits alone. Catastro writes a duplicate as
    `6(A)` in `ldt` and carries the letter in a separate `plp` field, while a
    query writes "6 A"; comparing the whole token would call those two
    different houses. Two parcels on the same number -- the duplicates -- are
    both accepted, and the nearest is the one reported.
    """
    wanted = _digits(asked)
    if not wanted:
        return {"finding": NO_NUMBER_ASKED}
    if not parcels:
        return {"finding": NO_PARCEL}

    matches = [p for p in parcels if _digits(p.get("number")) == wanted]
    nearest = parcels[0]

    if matches:
        match = matches[0]
        wanted_street = normalize_street(asked_street)
        found_street = cadastral_street(match.get("address"))
        if not _shares_a_word(wanted_street, found_street):
            return {
                "finding": NUMBER_MATCHED_OTHER_STREET,
                "asked_street": wanted_street,
                "cadastral_street": found_street,
                "distance_m": match["distance_m"],
                "reference": match.get("reference"),
                "address": match.get("address"),
            }
        if match["distance_m"] <= 0:
            return {
                "finding": ON_ASKED_PARCEL,
                "reference": match.get("reference"),
                "address": match.get("address"),
            }
        return {
            "finding": DISPLACED,
            "distance_m": match["distance_m"],
            "reference": match.get("reference"),
            "address": match.get("address"),
            "landed_on": nearest.get("address"),
        }

    # The asked number is not among what came back, so the only established
    # fact is that it is further than the furthest parcel returned.
    return {
        "finding": BEYOND_NEIGHBOURS,
        "at_least_m": parcels[-1]["distance_m"],
        "landed_on": nearest.get("address"),
        "landed_on_number": nearest.get("number"),
    }


# --- the other direction: an address, to the parcel that carries it ---------
#
# `parcels_at` answers "what is under this point", which bounds the error only
# when the asked number happens to be within the neighbour list's reach. When
# it is not, the asked parcel has to be found by name, and that takes two more
# keyless requests: the municipality's street index, then the parcel.

CALLEJERO_URL = (
    "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
    "COVCCallejero.svc/json/ObtenerCallejero"
)
DNPLOC_URL = (
    "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
    "COVCCallejero.svc/json/Consulta_DNPLOC"
)

# The street was not found in the municipality's index under any spelling this
# module is willing to accept. Its own state, because "Catastro has no such
# street" and "Catastro has it under a name I did not recognise" are the same
# observation from here, and neither is a fact about where the listing is.
STREET_NOT_MATCHED = "street_not_matched"

# More than one street in the municipality normalises to the same name. Never
# resolved by picking one: two streets called the same thing are two places,
# and a coin toss between them would produce a coordinate with a provenance
# that reads as certain.
STREET_AMBIGUOUS = "street_ambiguous"


def normalize_street(name: Any) -> str:
    """A street name reduced to what two spellings of it share.

    Accents folded, case dropped, and **every** non-alphanumeric character
    turned into a space -- commas included, which is what Catastro's
    article-suffix convention needs: `"CALZADA,LA"` becomes the two words
    `"CALZADA LA"` rather than one token no spelling of "La Calzada" could ever
    equal. The first version kept the comma, and the measurement over
    production caught it: rows 63, 622 and 954 were reported as standing on a
    street with no word in common with the one asked, when the only difference
    was where Catastro puts the article.

    This is deliberately a *narrow* normaliser feeding comparisons that are
    exact, rather than a similarity score: `services/address_agreement.py` does
    not compare street names at all because 13 of 36 token mismatches there
    were spelling, and a fuzzy matcher inheriting that error rate would answer
    with the wrong parcel -- which, unlike no answer, looks like a measurement.
    """
    import unicodedata

    text = unicodedata.normalize("NFKD", str(name or "").upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(text.split())


# Articles and prepositions carry no identity: two different streets in the
# same parish routinely share every one of them and nothing else. They are held
# out of the shared-word test so that "LA IGLESIA" and "LA BARROSA" are not
# read as the same street on the strength of "LA".
_STREET_STOPWORDS = frozenset(
    {
        "DE",
        "DEL",
        "DA",
        "DO",
        "DAS",
        "DOS",
        "LA",
        "EL",
        "LAS",
        "LOS",
        "A",
        "O",
        "Y",
        "E",
    }
)


def streets(province: str, municipality: str) -> Dict[str, Any]:
    """Every street Catastro holds for one municipality.

    One request for the whole index -- the endpoint ignores a name filter and
    returns all of them (769 for Rojales, 565 for Foz, measured 2026-09-08), so
    filtering happens here where the normalisation is visible.
    """
    try:
        payload = _fetch_json(
            CALLEJERO_URL,
            {
                "Provincia": province,
                "Municipio": municipality,
                "TipoVia": "",
                "NombreVia": "",
            },
        )
    except CadastreError as exc:
        return {"status": exc.state, "detail": exc.detail}

    result = (
        payload.get("consulta_callejeroResult") if isinstance(payload, dict) else None
    )
    if not isinstance(result, dict):
        return {"status": MALFORMED, "detail": "no consulta_callejeroResult"}
    error = _error_in(result)
    if error:
        return error

    block = result.get("callejero")
    entries = block.get("calle") if isinstance(block, dict) else None
    if entries is None:
        return {"status": NOT_FOUND, "detail": "no streets for this municipality"}
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return {"status": MALFORMED, "detail": "calle is neither list nor object"}

    found = []
    for entry in entries:
        direction = entry.get("dir") if isinstance(entry, dict) else None
        if not isinstance(direction, dict):
            continue
        name = str(direction.get("nv") or "").strip()
        if not name:
            continue
        found.append(
            {
                "code": str(direction.get("cv") or "").strip() or None,
                "sigla": str(direction.get("tv") or "").strip() or None,
                "name": name,
                "normalized": normalize_street(name),
            }
        )
    if not found:
        return {"status": NOT_FOUND, "detail": "no readable street in the index"}
    return {"status": OK, "streets": found}


# The words a Spanish address puts in front of a street name, as they appear in
# a geocoding query. Catastro's index holds the name alone and the type in a
# separate two-letter sigla, so a query's "calle Tarancon" has to lose its
# first word before it can equal the index's "TARANCON".
#
# The word is *tried* off rather than stripped, because some of these are also
# part of a name: the cadastre's own "AV RAMBLA DE LA LLIBERTAT" is a rambla
# inside an avenida. Both keys are looked up and a hit on either is the match.
_STREET_TYPE_WORDS = frozenset(
    {
        "CALLE",
        "AVENIDA",
        "AVDA",
        "PLAZA",
        "PZA",
        "PZ",
        "RUA",
        "LUGAR",
        "CAMINO",
        "CAMIN",
        "CARRETERA",
        "CTRA",
        "CRTA",
        "BARRIO",
        "ALDEA",
        "CASERIO",
        "CASERIA",
        "TRAVESIA",
        "TVA",
        "PASEO",
        "RAMBLA",
        "URBANIZACION",
        "POLIGONO",
    }
)


def street_key(name: Any) -> Tuple[str, ...]:
    """A street name as the *set* of words in it, sorted.

    Word order is dropped because Catastro's index moves the article to the
    end -- "RETELA,LA" for the "Lugar la Retela" a query names -- and an exact
    comparison on the joined string reads that as a different street. Measured
    over the production cohort, matching on the joined string left most of the
    rural Galician and Asturian lugares unmatched for that reason alone.

    Articles and prepositions are dropped for the same reason: Catastro's index
    holds "CASTRELOS" for the avenue a query names "avenida de Castrelos", and
    the "de" alone was enough to leave row 1759 unplaced. A name that is
    *nothing but* articles keeps them, because an empty key would match every
    other empty key.

    It stays an exact comparison: two names match only when they contain
    exactly the same content words. What it stops distinguishing is word order
    and the articles between them, which are the two differences the Spanish
    and Galician spellings of the same street actually create. Two streets that
    genuinely differ only in those would collide, and `match_street` answers
    `street_ambiguous` rather than choosing.
    """
    words = normalize_street(name).split()
    content = [word for word in words if word not in _STREET_STOPWORDS]
    return tuple(sorted(content or words))


def _street_keys(wanted: Any) -> List[Tuple[str, ...]]:
    """The spellings of one asked street this module is willing to match on."""
    target = normalize_street(wanted)
    if not target:
        return []
    keys = [street_key(target)]
    words = target.split()
    if len(words) > 1 and words[0] in _STREET_TYPE_WORDS:
        keys.append(street_key(" ".join(words[1:])))
    return keys


def match_street(index: List[Dict[str, Any]], wanted: Any) -> Dict[str, Any]:
    """The one street in the index that is the one asked for, or why not.

    The comparison is on the set of content words, so word order and articles
    are absorbed and nothing else is. Two candidates left after that are
    `street_ambiguous` and never a choice between them -- with one exception,
    and the exception is narrow on purpose.

    **A tie among candidates that all carry the same sigla is a duplicate index
    entry, and the exact spelling breaks it.** Foz holds one street twice, as
    `RU XOIÑA` and `RU XOIÑA, DA`; those are not two places, and refusing them
    left row 1734 -- a row #559 named -- unplaced when a query spelled "Calle
    Xoiña" matches one of them character for character. A tie across *different*
    siglas is a different thing: `LG` and `CL` of one name are plausibly a lugar
    and a street named after it, so that stays ambiguous however the query is
    spelled.
    """
    keys = _street_keys(wanted)
    if not keys:
        return {"status": STREET_NOT_MATCHED, "detail": "no street name to look for"}

    hits = [street for street in index if street_key(street["name"]) in keys]
    if not hits:
        return {
            "status": STREET_NOT_MATCHED,
            "detail": f"no street whose words are {' '.join(keys[0])!r}",
        }

    ambiguous = {
        "status": STREET_AMBIGUOUS,
        "detail": f"{len(hits)} streets whose words are {' '.join(keys[0])!r}",
        "candidates": hits,
    }
    if len({(hit["sigla"], hit["code"]) for hit in hits}) == 1:
        return {"status": OK, "street": hits[0]}
    if len({hit["sigla"] for hit in hits}) > 1:
        return ambiguous

    exact = {" ".join(name) for name in _exact_names(wanted)}
    spelled = [hit for hit in hits if normalize_street(hit["name"]) in exact]
    if len({(hit["sigla"], hit["code"]) for hit in spelled}) == 1:
        return {"status": OK, "street": spelled[0]}
    return ambiguous


def _exact_names(wanted: Any) -> List[Tuple[str, ...]]:
    """The asked street verbatim, and without its leading street-type word."""
    target = normalize_street(wanted)
    if not target:
        return []
    words = target.split()
    names = [tuple(words)]
    if len(words) > 1 and words[0] in _STREET_TYPE_WORDS:
        names.append(tuple(words[1:]))
    return names


def parcel_for_address(
    province: str, municipality: str, sigla: str, street: str, number: Any
) -> Dict[str, Any]:
    """The cadastral reference at one street number, from `Consulta_DNPLOC`.

    Returns the reference of the first *bien inmueble* at the address. A
    building under horizontal division answers with one flat and names the
    whole plot in `finca.ldt` -- both are kept, because for a coordinate they
    are the same parcel and for a person reading the note they are not.
    """
    try:
        payload = _fetch_json(
            DNPLOC_URL,
            {
                "Provincia": province,
                "Municipio": municipality,
                "Sigla": sigla or "",
                "Calle": street,
                "Numero": str(number),
            },
        )
    except CadastreError as exc:
        return {"status": exc.state, "detail": exc.detail}

    result = payload.get("consulta_dnplocResult") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return {"status": MALFORMED, "detail": "no consulta_dnplocResult"}
    error = _error_in(result)
    if error:
        return error

    holder = result.get("bico") if isinstance(result.get("bico"), dict) else result
    item = holder.get("bi") if isinstance(holder, dict) else None
    if isinstance(item, list):
        item = item[0] if item else None
    if not isinstance(item, dict):
        # `lrcdnp` is the shape a street number with several parcels answers in.
        listed = result.get("lrcdnp")
        item = _first_listed(listed)
    if not isinstance(item, dict):
        return {"status": NOT_FOUND, "detail": "no bien inmueble at that number"}

    reference = _reference_from_rc(item)
    if not reference:
        return {"status": MALFORMED, "detail": "no readable cadastral reference"}

    finca = holder.get("finca") if isinstance(holder, dict) else None
    return {
        "status": OK,
        "reference": reference,
        "address": str(item.get("ldt") or "").strip() or None,
        "plot_address": (
            str(finca.get("ldt") or "").strip() or None
            if isinstance(finca, dict)
            else None
        ),
    }


def _first_listed(listed: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(listed, dict):
        return None
    entries = listed.get("rcdnp")
    if isinstance(entries, dict):
        return entries
    if isinstance(entries, list) and entries:
        first = entries[0]
        return first if isinstance(first, dict) else None
    return None


def _reference_from_rc(item: Dict[str, Any]) -> Optional[str]:
    """The 14-character parcel reference out of Catastro's five-part `rc`.

    `pc1` + `pc2` is the parcel; `car`/`cc1`/`cc2` identify one property inside
    it and are dropped, because what is wanted here is where the parcel is and
    every flat in a block shares that.
    """
    identity = item.get("idbi") if isinstance(item.get("idbi"), dict) else item
    parts = identity.get("rc") if isinstance(identity, dict) else None
    if not isinstance(parts, dict):
        return None
    first = str(parts.get("pc1") or "").strip()
    second = str(parts.get("pc2") or "").strip()
    joined = f"{first}{second}"
    return joined or None


def _error_in(
    result: Dict[str, Any], absence_codes: frozenset = ADDRESS_ABSENCE_CODES
) -> Optional[Dict[str, Any]]:
    """The refusal carried inside a 200, or `None` if the answer is an answer.

    `absence_codes` is which of this endpoint's codes mean "looked, nothing
    there". It is a parameter because the two endpoints disagree: the address
    lookups have two such codes and the coordinate lookup has none.
    """
    control = result.get("control")
    if not isinstance(control, dict) or not _as_float(control.get("cuerr")):
        return None
    errors = result.get("lerr")
    first = errors[0] if isinstance(errors, list) and errors else errors
    code = text = ""
    if isinstance(first, dict):
        code = str(first.get("cod") or "").strip()
        text = str(first.get("des") or "").strip()
    state = NOT_FOUND if code in absence_codes else REFUSED
    return {"status": state, "detail": f"{code}: {text}" if code else text or "refused"}


def _digits(value: Any) -> Optional[str]:
    """The leading digits of a house number, without its duplicate suffix."""
    token = str(value).strip() if value is not None else ""
    digits = ""
    for char in token:
        if char.isdigit():
            digits += char
        else:
            break
    return digits.lstrip("0") or (digits or None)


# --- Catastro's own names for a place ---------------------------------------

PROVINCES_URL = (
    "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
    "COVCCallejero.svc/json/ObtenerProvincias"
)
MUNICIPALITIES_URL = (
    "https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
    "COVCCallejero.svc/json/ObtenerMunicipios"
)


class PlaceIndex:
    """Catastro's province and municipality names, by its own codes.

    The address lookups take names, and the coordinate lookup answers with
    codes, so something has to join them. It is a small object a caller holds
    for the length of a run rather than module state: two indexes fetched once
    each are the difference between 5 requests and 5 per row, and module-level
    memoisation would make the test that fetches nothing depend on the test
    that fetched.

    A province is fetched once; a municipality index, once per province.
    """

    def __init__(self):
        self._provinces: Optional[Dict[str, str]] = None
        self._municipalities: Dict[str, Dict[str, str]] = {}

    def province(self, code: Any) -> Optional[str]:
        if self._provinces is None:
            self._provinces = self._fetch_provinces()
        return self._provinces.get(_code(code))

    def municipality(self, province_code: Any, code: Any) -> Optional[str]:
        name = self.province(province_code)
        if not name:
            return None
        if name not in self._municipalities:
            self._municipalities[name] = self._fetch_municipalities(name)
        return self._municipalities[name].get(_code(code))

    def place_of(self, parcel: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """The province and municipality names one parsed parcel sits in."""
        province = self.province(parcel.get("province_code"))
        municipality = self.municipality(
            parcel.get("province_code"), parcel.get("municipality_code")
        )
        if not province or not municipality:
            return None
        return {"province": province, "municipality": municipality}

    @staticmethod
    def _fetch_provinces() -> Dict[str, str]:
        try:
            payload = _fetch_json(PROVINCES_URL, {})
        except CadastreError:
            # An index that could not be fetched is an empty index, and every
            # lookup through it answers `None` -- which the callers already
            # read as "could not resolve". Never a partial index.
            return {}
        result = payload.get("consulta_provincieroResult")
        block = result.get("provinciero") if isinstance(result, dict) else None
        entries = block.get("prov") if isinstance(block, dict) else None
        if not isinstance(entries, list):
            return {}
        return {
            _code(entry.get("cpine")): str(entry.get("np") or "").strip()
            for entry in entries
            if isinstance(entry, dict) and entry.get("cpine") and entry.get("np")
        }

    @staticmethod
    def _fetch_municipalities(province: str) -> Dict[str, str]:
        try:
            payload = _fetch_json(
                MUNICIPALITIES_URL, {"Provincia": province, "Municipio": ""}
            )
        except CadastreError:
            return {}
        result = payload.get("consulta_municipieroResult")
        block = result.get("municipiero") if isinstance(result, dict) else None
        entries = block.get("muni") if isinstance(block, dict) else None
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return {}
        index = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            codes = entry.get("loine")
            name = str(entry.get("nm") or "").strip()
            if isinstance(codes, dict) and name:
                index[_code(codes.get("cm"))] = name
        return index


def _code(value: Any) -> str:
    """A Catastro code, without the leading zeros it is inconsistent about.

    The same municipality arrives as `"3"` from one endpoint and `"03"` from
    another; joining the two indexes on the raw strings silently found nothing
    for every province numbered below ten.
    """
    token = str(value if value is not None else "").strip()
    return token.lstrip("0") or ("0" if token else "")
