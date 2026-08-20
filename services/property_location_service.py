import logging
import re
from typing import Any, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from services.coordinate_quality import (
    KNOWN_ACCURACIES,
    SOURCE_MANUAL,
    clear_manual_coordinate,
    improves_on,
    manual_coordinate,
    normalize_accuracy,
    portal_coordinate,
    record_manual_coordinate,
)

from models import Property
from services.enrichment_write import check_writable, locked_write
from utils.geocoding import GeocodingService
from utils.municipality_codes import load_name_index
from utils.municipality_codes import match as match_municipality
from utils.municipality_codes import province_code_for_name

logger = logging.getLogger(__name__)


_LOCATION_FROM_TITLE_RE = re.compile(r"\b(?:in|en)\s+(?P<loc>.+)$", re.IGNORECASE)

# How far into a title the "in"/"en" may sit and still be the portal's
# `<type> in <location>` separator rather than an ordinary preposition in a
# sentence.
#
# Measured over all 401 production titles on 2026-08-16: 392 carry the marker
# at word 1, 2 or 3 -- "Land in", "Land plot in", "Flat / apartment in", the
# longest legitimate prefix being three words. Exactly 3 carry it at word 8,
# and those are descriptions rather than titles:
#
#   "FINCA 529 An excellent investment opportunity is presented in a farm for
#    sale, loca"
#
# from which the regex extracted "a farm for sale, loca" and asked Google to
# geocode it. Nothing at all falls between 4 and 7, so the threshold sits in a
# clean gap with headroom on both sides rather than on a guessed boundary.
# Past it the whole title is used, which is what a title carrying no marker
# already does.
_LOCATION_MARKER_MAX_WORDS = 4

# Idealista writes a missing street number as the literal "n/a", and it rides
# into the query as an address component: "Tiñana, n/a, Viella-Granda-Meres,
# Siero, Spain". 42 of those 401 titles carry one. It cannot help a geocoder
# and it is one more token for a fuzzy match to work with.
#
# "s/n" -- sin número -- is deliberately NOT dropped: it is a real Spanish
# addressing convention that geocoders understand, and it does not appear in
# this data anyway.
_PLACEHOLDER_COMPONENT_RE = re.compile(r"^n/a\.?$", re.IGNORECASE)


def _drop_placeholder_components(text: str) -> str:
    """Remove address components that are only a "no value" placeholder."""
    parts = [part.strip() for part in text.split(",")]
    kept = [
        part for part in parts if part and not _PLACEHOLDER_COMPONENT_RE.match(part)
    ]
    return ", ".join(kept)


# A result at these scales is not a place this listing is at -- it is what
# Google falls back to when the query means nothing to it. Every query built
# here ends in ", Spain", so a title fragment like "Finca offers for" resolves
# to the country and returns Spain's own point, 40.463667,-3.749220 (issue
# #331: eight properties sat there, and every travel target, the beaches block
# and the travel component of their score were measured from it -- "Hospital La
# Paz Peñagrande, 11 min" for a plot in Asturias).
#
# `location_type` cannot catch this: a street centroid and a country are both
# APPROXIMATE. The result's `types` can, and it does not go stale the way a
# blocklist of known centroids would.
#
# Measured on production 2026-08-16 over all 401 rows grouped by recorded
# formatted address: the only value coarser than a town is "Spain" (8 rows, one
# point). The administrative levels are refused too -- nothing hits them today,
# so it costs nothing now and stops a province centroid being the next version
# of this.
COARSE_RESULT_TYPES = frozenset(
    {
        "country",
        "administrative_area_level_1",
        "administrative_area_level_2",
    }
)


def _is_too_coarse(geo: dict) -> bool:
    """Whether Google matched something far larger than a property."""
    return bool(COARSE_RESULT_TYPES.intersection(geo.get("types") or ()))


# --- the result must be about the place we asked about (#348) ---------------
#
# `_is_too_coarse` is about *size*; this is about *place*. Google answered
# "25530 Vielha, Lleida, Spain" for two Siero listings whose queries carry the
# parish "Viella-Granda-Meres" -- a `locality`, exactly the scale a listing
# should match, and 539 km away in Val d'Aran. The size rule passes it, and
# re-geocoding cannot repair it: the query is deterministic and returns the
# same wrong locality, only with a fresher-looking record.
#
# The reference is the row's *own* municipality, never a fixed geography: the
# owner's archived subscriptions hold real listings in Alicante, so refusing
# anything outside the five watched provinces would refuse good data.
#
# Both sides reduce to a two-digit province code, which is the one number the
# two vocabularies share: an INE municipality code is province(2)+municipality
# (3), and a Spanish postal code is province(2)+3. So 25530 is province 25 and
# Siero is 33066, province 33 -- a contradiction that needs no knowledge of
# Val d'Aran.
#
# Both are read from `address_components`, which
# `GeocodingService.geocode_address` already returns and nothing read until
# #348. The postal code comes first; where Google returns none -- 104 of 406
# production rows on 2026-08-17 -- the answer's own
# `administrative_area_level_2` carries the province name instead, and #371
# reads that. Where neither is present the check reports "cannot tell" rather
# than agreement, and a name that is not one of Spain's 52 provinces (the
# autonomous community "Galicia", one production row) is exactly such a case.


def _row_province(prop) -> Optional[str]:
    """Province code of the row's own municipality, or None if unresolvable.

    None is the common case and must stay cheap to reach: a great many rows
    carry a municipality that is a parish list, an email truncation (#298) or
    a title fragment ("Finca Offers For"). None of those can contradict
    anything, and none of them is a reason to refuse a coordinate.
    """
    code = match_municipality(
        str(getattr(prop, "municipality", "") or ""), load_name_index()
    )
    return code[:2] if code else None


def _result_province(geo: dict) -> Optional[str]:
    """Province code of Google's answer, or None when it names no province.

    The postal code is read first because it is a code rather than a name and
    needs no table. Where Google returns none -- 104 of 406 production rows on
    2026-08-17, every one of them with a resolvable municipality, so the row
    side of the comparison was ready and only this side was missing -- the
    answer still names the province, and #371 reads it:

        component: ['administrative_area_level_2', 'political'] | Asturias | O

    verified with one live Geocoding call on "Municipality of Siero, Asturias,
    Spain", a query production had recorded without a postal code. The long
    name is the join; `short_name` there is the old vehicle-plate letter, a
    second vocabulary this does not need.

    Only `administrative_area_level_2` is read. An answer that names an
    autonomous community instead ("Galicia", one production row) has no level
    2 at all and lands on None, which is cannot-tell -- the right answer,
    since a community neither agrees nor disagrees with a province.
    """
    components = [
        c for c in (geo.get("address_components") or ()) if isinstance(c, dict)
    ]

    for component in components:
        if "postal_code" not in (component.get("types") or ()):
            continue
        raw = component.get("long_name") or component.get("short_name") or ""
        digits = "".join(char for char in str(raw) if char.isdigit())
        if len(digits) >= 2:
            return digits[:2]

    for component in components:
        if "administrative_area_level_2" not in (component.get("types") or ()):
            continue
        code = province_code_for_name(component.get("long_name") or "")
        if code:
            return code
    return None


def _province_agreement(prop, geo: dict):
    """(state, row_province, result_province).

    `state` is one of "agreed", "contradicted", "row_unmatched",
    "result_has_no_province" -- four values rather than a boolean, because
    "we could not compare" is a third answer and collapsing it into either
    real one is the mistake #98 is about. Only "contradicted" refuses.

    This is the check #348 built and #371 extended, under the name it always
    measured. It used to be stored as `municipality_check`, which is what
    GEO-001 is about: inside one province it can only ever answer "agreed",
    and every row in this database is Asturias.
    """
    row = _row_province(prop)
    result = _result_province(geo)
    if row is None:
        return "row_unmatched", row, result
    if result is None:
        return "result_has_no_province", row, result
    return ("agreed" if row == result else "contradicted"), row, result


# Component types that may name a municipality, and the two that may not.
#
# The distinction is the whole guard, and it is not available from
# `formatted_address`: four of the five watched provinces have a capital
# whose municipality carries the province's own name -- measured
# 2026-08-19, `match()` answers 15030 for "A Coruña", 27028 for "Lugo",
# 32054 for "Ourense" and 36038 for "Pontevedra". So a reader that splits
# the formatted string on commas reads the province as a municipality and
# invents a contradiction: simulated over the 725 production rows that
# carry a formatted address, that mistake alone produced 6 of 10
# "contradictions", every one of them an artifact.
#
# Google labels the province `administrative_area_level_2` and the
# autonomous community `administrative_area_level_1`, so reading components
# by type -- and never those two -- is what makes the comparison possible
# at all.
_MUNICIPALITY_COMPONENT_TYPES = (
    "locality",
    "postal_town",
    "administrative_area_level_3",
    "administrative_area_level_4",
)

# Google renders a Spanish municipality with an administrative word in front
# of it often enough to matter: 25 of those 725 formatted addresses say
# "Municipality of ...", and `match("Municipality of Siero")` is None while
# `match("Siero")` is 33066. The Spanish and Galician forms are here because
# the app's request language is a setting, not a law; each costs one
# alternation and its absence would cost a silent cannot-tell.
_MUNICIPALITY_PREFIX_RE = re.compile(
    r"^(?:municipality\s+of|municipio\s+de|concello\s+de|concejo\s+de"
    r"|ayuntamiento\s+de)\s+",
    re.IGNORECASE,
)


def _result_municipality_codes(geo: dict) -> set:
    """INE codes the result's components name as a municipality.

    Three guards, and every one exists because a wrong code here is worse
    than no code -- this set is what produces `contradicted`, and nothing
    downstream re-checks it.

    Neither the community nor the province component is read (see
    `_MUNICIPALITY_COMPONENT_TYPES`). A candidate is kept only when the
    answer's own province is known **and** the candidate sits in it: the
    index spans five provinces, so "Mieres" resolves to Asturias whatever
    province the answer is really in, and an answer that names no province at
    all cannot rule that out -- so it resolves nothing rather than guessing.
    And the portal alias table is not applied (`apply_aliases=False`): it is
    verified for what Idealista calls a council, and several of its source
    strings are real places in their own right, so on this side it would turn
    a correct answer into a code for somewhere else.

    An empty set is "this answer names no municipality I can resolve", which
    is a cannot-tell and never a disagreement.
    """
    components = [
        c for c in (geo.get("address_components") or ()) if isinstance(c, dict)
    ]
    result_province = _result_province(geo)
    index = load_name_index()

    codes = set()
    for component in components:
        types = component.get("types") or ()
        if not any(kind in types for kind in _MUNICIPALITY_COMPONENT_TYPES):
            continue
        raw = str(component.get("long_name") or component.get("short_name") or "")
        code = match_municipality(
            _MUNICIPALITY_PREFIX_RE.sub("", raw).strip(),
            index,
            apply_aliases=False,
        )
        if code is None:
            continue
        if result_province is None or code[:2] != result_province:
            # Either the answer names no province to check against, or the
            # candidate sits in a different one. Both are the same fact: this
            # name cannot be shown to belong to this answer.
            continue
        codes.add(code)
    return codes


def _municipality_agreement(prop, geo: dict):
    """(state, row_code, result_codes) -- about municipalities, as it says.

    Five values, for the reason the province check has four: every way of
    *not* being able to compare stays its own answer rather than borrowing
    one of the two real ones (#98).

    ``agreed``
        the row's own municipality is among the ones the answer names;
    ``contradicted``
        the answer names exactly one municipality and it is a different one;
    ``row_unmatched``
        the row's municipality resolves to no INE code -- a parish list, an
        email truncation (#298), a title fragment, or a province outside the
        five this index covers, which is where the archived Alicante
        subscriptions live;
    ``result_names_no_municipality``
        nothing in the answer's components resolves to one;
    ``result_names_several``
        two or more do, and disagree. A coin flip between them would be a
        verdict invented out of an ambiguity.

    Nothing here refuses a coordinate. The province contradiction still
    does, because a province code is unambiguous and #348 proved the case;
    a municipality disagreement is far likelier to be a parish sitting
    across a boundary, and `Property.municipality` is free text off an alert
    email. Measured on production 2026-08-19, refusing on this would have
    thrown away a *precise* street-level result (property 80, "Barrio
    Candín, 11" in Langreo against a row that says Siero). So this is a
    record, not a gate -- which is exactly the defect GEO-001 reports: the
    guard that would have caught property 559 was the one reporting success.
    """
    row_code = match_municipality(
        str(getattr(prop, "municipality", "") or ""), load_name_index()
    )
    result_codes = _result_municipality_codes(geo)
    if row_code is None:
        return "row_unmatched", row_code, result_codes
    if not result_codes:
        return "result_names_no_municipality", row_code, result_codes
    if row_code in result_codes:
        return "agreed", row_code, result_codes
    if len(result_codes) > 1:
        return "result_names_several", row_code, result_codes
    return "contradicted", row_code, result_codes


# What the reader answers when nobody took a check. A fourth presentation
# state, like `listing_verification`'s `unchecked`: no row is rewritten and
# the database keeps exactly what it knows.
CHECK_UNCHECKED = "unchecked"

# `result_has_no_postcode` is what this state was called before #371 taught
# the province check to read the name as well. 115 production records still
# carry it (measured 2026-08-19). Same state, earlier name -- folded here, in
# the one reader, rather than by rewriting rows.
_LEGACY_PROVINCE_STATES = {"result_has_no_postcode": "result_has_no_province"}


def read_geocoding_checks(record) -> dict:
    """`{"province": state, "municipality": state}` for a stored record.

    Two spellings of one key exist in the table and they mean different
    things, so this is the one place that knows which is which.

    A record written before GEO-001 carries `municipality_check` alone, and
    that value is a **province** verdict under a municipality's name -- 201
    production rows read "agreed" on 2026-08-19 meaning nothing stronger than
    "Asturias is Asturias". Reading those as municipality agreement is the
    defect the ticket reports, so this reader answers `province` from them and
    `municipality` as `unchecked`: nobody looked, which is neither agreement
    nor disagreement (#98).

    A record written after carries both keys and is read literally. The
    discriminator is the presence of `province_check`, not a date or a
    version -- the block is rewritten whole on every geocode, so its own
    shape is the only fact that travels with it.
    """
    if not isinstance(record, dict):
        return {"province": CHECK_UNCHECKED, "municipality": CHECK_UNCHECKED}

    province = record.get("province_check")
    if province is not None:
        return {
            "province": _LEGACY_PROVINCE_STATES.get(province, province),
            "municipality": record.get("municipality_check") or CHECK_UNCHECKED,
        }

    legacy = record.get("municipality_check")
    if legacy is not None:
        return {
            "province": _LEGACY_PROVINCE_STATES.get(legacy, legacy),
            "municipality": CHECK_UNCHECKED,
        }
    return {"province": CHECK_UNCHECKED, "municipality": CHECK_UNCHECKED}


def _normalize_query(value: str) -> Optional[str]:
    text = " ".join(str(value or "").replace("\xa0", " ").split()).strip()
    if not text:
        return None
    text = re.sub(r"\s+\d[\d.,]*\s*€.*$", "", text).strip()
    text = re.sub(r"\s+\d[\d.,]*\s*m[²2].*$", "", text).strip()
    text = _drop_placeholder_components(text)
    return text or None


def _build_geocoding_queries(prop: Property) -> List[str]:
    queries: List[str] = []

    title = _normalize_query(prop.title or "")
    if title:
        match = _LOCATION_FROM_TITLE_RE.search(title)
        # A marker buried in a sentence is not a separator. Fall back to the
        # whole title, exactly as a title with no marker at all already does.
        if match and len(title[: match.start()].split()) <= _LOCATION_MARKER_MAX_WORDS:
            loc = _normalize_query(match.group("loc"))
        else:
            loc = title
        if loc:
            queries.append(loc)

    municipality = _normalize_query(prop.municipality or "")
    if municipality:
        queries.append(municipality)

    out: List[str] = []
    seen = set()
    for q in queries:
        if not q:
            continue
        # Prefer explicit Spain bias without overriding full addresses.
        q_with_country = (
            q if re.search(r"\bspain\b|\bespaña\b", q, re.IGNORECASE) else f"{q}, Spain"
        )
        key = q_with_country.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q_with_country)

    return out


class PropertyLocationService:
    def __init__(self, geocoding_service: Optional[GeocodingService] = None):
        self.geocoding_service = geocoding_service or GeocodingService()

    @staticmethod
    def _keep_portal_pin(
        prop: Property,
        portal_pin,
        previous_accuracy: str,
        *,
        query,
        answered,
        answered_accuracy,
        refused_reason: str = "",
    ) -> None:
        """Put the portal's pin back, and say on the row that it was tried.

        The record goes under `geocoding` like any other outcome of this
        method, because that is where the next reader looks; `kept` is what
        makes it legible as a decision rather than a failure. Without it the
        row reads exactly like one that was never refreshed, and the button
        gets pressed again for the same money.
        """
        lat, lon, source = portal_pin
        prop.location_lat = lat
        prop.location_lon = lon
        prop.location_accuracy = previous_accuracy

        enrichment = dict(prop.enrichment or {})
        record = {
            "query": query,
            "formatted_address": answered,
            "accuracy": previous_accuracy,
            "kept": f"{source} coordinate",
            "kept_because": (
                "the geocode did not improve on it"
                if answered_accuracy
                else "the geocode returned nothing"
            ),
        }
        if answered_accuracy:
            record["answered_accuracy"] = answered_accuracy
        if refused_reason:
            record["refused"] = refused_reason
        enrichment["geocoding"] = record
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

    def _geocode_outcome(self, prop: Property, *, refresh: bool) -> dict:
        """Ask Google, and decide nothing that touches the row.

        Split out of `ensure_coordinates` for #400: every write this method
        used to perform happened *after* minutes of network calls, against the
        copy of the row its own session loaded before them. Measured on
        property 733 (2026-08-17): an operator wrote the portal's pin and a
        provenance record at 15:36 while this chain was blocked on Overpass,
        and the chain's commit at 15:44 replaced both without trace.

        So the loop is pure. It reads `prop` -- the title and municipality the
        queries are built from, and the row's own accuracy for the even-trade
        comparison -- and returns what it found. Nothing is applied until the
        row is held; see `_apply_geocode_outcome`.

        The pre-lock reading of `portal_pin` and `previous_accuracy` is
        deliberately *provisional*: it decides which candidate this pass
        prefers, and the same comparison is made again under the lock against
        whatever the row says then.
        """
        portal_pin = portal_coordinate(prop) if refresh else None
        previous_accuracy = normalize_accuracy(prop.location_accuracy)

        refused = None
        for query in _build_geocoding_queries(prop):
            try:
                geo = self.geocoding_service.geocode_address(query)
            except Exception as e:
                logger.warning("Geocoding failed for %r: %s", query, e)
                continue
            if not geo:
                continue

            if _is_too_coarse(geo):
                # Not a location for this listing. Keep the last one so the
                # absence can be explained on the row, and try the next
                # candidate -- a title that means nothing to Google is often
                # followed by a municipality that does.
                logger.warning(
                    "Refusing %r: Google matched %r (%s), which is not a property",
                    query,
                    geo.get("formatted_address"),
                    ", ".join(geo.get("types") or []),
                )
                refused = {
                    "reason": "result_too_coarse",
                    "query": query,
                    "formatted_address": geo.get("formatted_address"),
                    "result_types": list(geo.get("types") or []),
                }
                continue

            agreement, row_province, result_province = _province_agreement(prop, geo)
            municipality_state, row_municipality, result_municipalities = (
                _municipality_agreement(prop, geo)
            )
            if agreement == "contradicted":
                logger.warning(
                    "Refusing %r: Google matched %r in province %s, but this row's "
                    "municipality %r is in province %s",
                    query,
                    geo.get("formatted_address"),
                    result_province,
                    prop.municipality,
                    row_province,
                )
                refused = {
                    "reason": "result_in_wrong_province",
                    "query": query,
                    "formatted_address": geo.get("formatted_address"),
                    "result_types": list(geo.get("types") or []),
                    "row_province": row_province,
                    "result_province": result_province,
                }
                continue

            try:
                new_lat = float(geo["lat"])
                new_lon = float(geo["lng"])
            except Exception:
                continue

            accuracy = normalize_accuracy(geo.get("accuracy"))
            if accuracy not in KNOWN_ACCURACIES:
                accuracy = "unknown"

            if portal_pin is not None and not improves_on(accuracy, previous_accuracy):
                # An even trade is not a trade: see the note on
                # `ensure_coordinates`. The attempt is recorded on the row so
                # the next reader -- or the next press of the button -- knows
                # it was made and what it answered, rather than trying it again
                # for the same money and the same result.
                return {
                    "kind": "keep_pin",
                    "query": query,
                    "answered": geo.get("formatted_address"),
                    "answered_accuracy": accuracy,
                }

            if municipality_state == "contradicted":
                logger.info(
                    "Municipality check disagrees for %r: row %r is %s, Google "
                    "matched %r in %s",
                    query,
                    prop.municipality,
                    row_municipality,
                    geo.get("formatted_address"),
                    sorted(result_municipalities),
                )

            return {
                "kind": "found",
                "lat": new_lat,
                "lon": new_lon,
                "accuracy": accuracy,
                "query": query,
                "formatted_address": geo.get("formatted_address"),
                "province_check": agreement,
                "municipality_check": municipality_state,
                "row_municipality": row_municipality,
                "result_municipalities": result_municipalities,
            }

        return {"kind": "nothing", "refused": refused}

    def _apply_geocode_outcome(
        self, prop: Property, outcome: dict, *, refresh: bool
    ) -> bool:
        """Write what the geocode found, against the row as it is *now*.

        Called with the row held (`services/enrichment_write.locked_write`), so
        everything the decision rests on is re-read here rather than carried
        across the network calls: `portal_coordinate` and the row's accuracy
        both come off the refreshed instance. The even-trade comparison is made
        again for the same reason -- an operator who wrote a better pin while
        the geocode ran must not have it replaced by a candidate that was only
        an improvement on what the row said before.
        """
        portal_pin = portal_coordinate(prop) if refresh else None
        previous_accuracy = normalize_accuracy(prop.location_accuracy)
        kind = outcome.get("kind")

        if kind == "found" and portal_pin is not None:
            # Re-decided under the lock. The pre-lock pass preferred this
            # candidate against a row that may since have changed.
            if not improves_on(outcome["accuracy"], previous_accuracy):
                kind = "keep_pin"
                outcome = {
                    "kind": "keep_pin",
                    "query": outcome["query"],
                    "answered": outcome["formatted_address"],
                    "answered_accuracy": outcome["accuracy"],
                }

        if kind == "found":
            prop.location_lat = outcome["lat"]
            prop.location_lon = outcome["lon"]
            prop.location_accuracy = outcome["accuracy"]

            enrichment = dict(prop.enrichment or {})
            record = {
                "query": outcome["query"],
                "formatted_address": outcome["formatted_address"],
                "accuracy": outcome["accuracy"],
                # Two checks, each under the name of what it compares
                # (GEO-001). Recorded rather than assumed: most of these
                # values mean the coordinate was accepted *unchecked*, and a
                # row that reads "agreed" is a stronger claim than one that
                # reads "row_unmatched". Same reason `sea_view_service` stamps
                # `origin_unverified` instead of quietly treating unverified
                # provenance as verified.
                #
                # `province_check` is the one that refuses, and it is the
                # comparison this key held under the other name until GEO-001.
                # `municipality_check` now compares municipalities, so a
                # record carrying both is a stronger statement than one
                # carrying only the first -- which is how
                # `read_geocoding_checks` tells them apart.
                "province_check": outcome["province_check"],
                "municipality_check": outcome["municipality_check"],
            }
            if outcome["municipality_check"] == "contradicted":
                # The codes, so a reader can act on the row without
                # re-geocoding it. Only on the interesting outcome: the
                # province check already stores its two codes only when it
                # refuses.
                record["row_municipality"] = outcome["row_municipality"]
                record["result_municipalities"] = sorted(
                    outcome["result_municipalities"]
                )
            enrichment["geocoding"] = record
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
            return True

        if kind == "keep_pin":
            self._keep_portal_pin(
                prop,
                portal_pin,
                previous_accuracy,
                query=outcome.get("query"),
                answered=outcome.get("answered"),
                answered_accuracy=outcome.get("answered_accuracy"),
                refused_reason=outcome.get("refused_reason", ""),
            )
            return True

        refused = outcome.get("refused")
        if portal_pin is not None:
            # Nothing answered, and the row has a pin. Putting it back is not
            # undoing the refresh -- the refusal is recorded with it -- it is
            # refusing to make the row *less* located than it was.
            self._keep_portal_pin(
                prop,
                portal_pin,
                previous_accuracy,
                query=(refused or {}).get("query"),
                answered=(refused or {}).get("formatted_address"),
                answered_accuracy=None,
                refused_reason=(refused or {}).get("reason") or "no_result",
            )
            return True

        if refresh:
            # Only a portal pin is defended (#393). A row without one is left
            # unlocated by a refresh that answered nothing, which is the
            # behaviour this method has always had -- it is applied here, under
            # the lock, rather than eagerly before the network calls. Doing it
            # eagerly is what made a `refresh()` under the lock autoflush this
            # run's own clearing and read it straight back as the "fresh" row.
            prop.location_lat = None
            prop.location_lon = None
            prop.location_accuracy = "unknown"

        if refused is not None:
            # No coordinates, and the row says why. An empty travel block is an
            # honest "nobody could locate this listing"; a country centroid is
            # six confident measurements taken 450 km from the property.
            enrichment = dict(prop.enrichment or {})
            record = {
                "query": refused["query"],
                "formatted_address": refused["formatted_address"],
                "accuracy": "unknown",
                # The reason travels with the refusal rather than being
                # hardcoded here: there are two of them now, and a row refused
                # for sitting in the wrong province must not read as one
                # refused for being a country.
                "refused": refused["reason"],
                "result_types": refused["result_types"],
            }
            if refused.get("row_province") or refused.get("result_province"):
                record["row_province"] = refused.get("row_province")
                record["result_province"] = refused.get("result_province")
            enrichment["geocoding"] = record
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
        elif refresh:
            # A refresh that found nothing and had nothing to say: the old
            # record described a coordinate this row no longer has.
            enrichment = dict(prop.enrichment or {})
            if enrichment.pop("geocoding", None) is not None:
                prop.enrichment = enrichment or None
                flag_modified(prop, "enrichment")

        return False

    def ensure_coordinates(
        self, prop: Property, refresh: bool = False, *, commit: bool = False
    ) -> bool:
        """Best-effort: populate property.location_lat/lon from title/municipality.

        `Property.enrichment` is a plain `db.Column(JSON)`, so SQLAlchemy tracks
        *assignment*, not mutation -- and `prop.enrichment or {}` hands back the
        very object already on the instance, so mutating it and assigning it
        back is not a change at all. On a fresh row that is invisible, because
        the column is NULL and the `or {}` builds a new dict; on an already
        enriched row the write is silently dropped.

        Measured 2026-08-15: a re-geocode of 168 production rows wrote every
        scalar column -- coordinates and `location_accuracy` both correct -- and
        not one `enrichment["geocoding"]` record. The tool that ran it reads
        that record to decide which rows are still unmeasured, so it would have
        re-geocoded, and re-paid for, all 168 on the next run while reporting
        itself resumable.

        `flag_modified` is the idiom already used for this column in
        `services/sea_distance_service.py`, `services/quality_of_life_service.py`
        and `services/pool_service.py`.


        **The write happens with the row held** (#400). Every write this method
        performs used to land after minutes of Google and Overpass calls,
        against the copy of the row this session loaded before them. Measured
        on property 733 (2026-08-17): an operator, concluding the request had
        died, wrote the portal's own pin and a provenance record at 15:36 and
        committed; the still-running chain committed its own view at 15:44 and
        both were gone without trace. That is #339 in the scalar columns.

        The shape is `services/enrichment_write.py`'s, which owns this rule for
        the `enrichment` column and is column-agnostic in fact -- neither
        `check_writable` nor `locked_write` names a column:

        * the caller is validated **before** the measurement, so an unwritable
          caller costs a raise rather than a round of billed lookups;
        * the geocode runs **unlocked** -- holding a row across those seconds is
          the cost #196 refused, and the loop writes nothing;
        * the row is locked, re-read, and only then decided and written. The
          even-trade comparison is made again there, because a pin an operator
          wrote while the geocode ran must not lose to a candidate that only
          improved on what the row said before.

        `commit=False` is the default and takes **no** lock, per that module's
        contract: the caller owns a transaction whose end this code cannot see.
        `PropertyEnrichmentService.enrich_property` passes `commit=True` and
        calls this first, before anything else dirties the session -- see the
        note there.

        One consequence is deliberate and visible: with `refresh=True` the
        columns are no longer cleared *before* the network calls. Clearing them
        eagerly made the `refresh()` under the lock autoflush this run's own
        `None`s and read them straight back as the "fresh" row, defeating the
        re-read entirely. The clearing now happens in the locked tail, on the
        same paths as before.
        """
        if not prop:
            return False

        # Before the measurement, never after: a caller that cannot commit here
        # should cost a raise, not a round of billed Google lookups.
        locked = check_writable(prop, commit)

        # A location a person established is never overwritten, and the geocode
        # is not even attempted -- the shape `services/advertiser.enrich` uses
        # for a hand-set seller verdict, and for the same reason: the answer is
        # already on the row, and asking again spends money to be told
        # something worse. Placed before the `refresh` branch so the rule reads
        # on its own rather than depending on the one under it.
        #
        # The return value is what this method's return value has always meant
        # -- does the row have a coordinate -- rather than an unconditional
        # `True`. A hand-set block whose columns have since been nulled by some
        # other writer is a row with no coordinate, and saying otherwise would
        # be the kind of claim this file exists to refuse.
        if manual_coordinate(prop) is not None:
            return bool(prop.location_lat and prop.location_lon)

        # `refresh` used to reach this by clearing the columns first; it says so
        # directly now, for the reason in the docstring.
        if not refresh and prop.location_lat and prop.location_lon:
            return True

        outcome = self._geocode_outcome(prop, refresh=refresh)

        with locked_write(prop, locked=locked, commit=commit):
            return self._apply_geocode_outcome(prop, outcome, refresh=refresh)


def set_location_by_hand(
    prop: Property,
    *,
    lat: Any,
    lon: Any,
    accuracy: str,
    note: str,
    source: str = SOURCE_MANUAL,
    commit: bool = True,
) -> dict:
    """Record the location a person established, and defend it from the geocoder.

    This is the writer `manual_coordinate` reads and `ensure_coordinates`
    refuses in front of. It exists because the app had **no** hand-set path for
    a coordinate at all: measured 2026-08-20, the only writers of
    `location_accuracy` in the tree are the geocoder, the fotocasa import, the
    `Land` migration and the restore half of `utils/refresh_property_accuracy.py`.
    Everything else that ever set one -- three production rows, in three
    different shapes -- was an ad-hoc script run through `docker exec`, which is
    exactly the boundary `services/ingest_policy.py` records as the one a flag
    cannot close.

    It is two functions rather than `advertiser.set_by_hand`'s one, which takes
    `None` to clear. A verdict is a single value and a sentinel reads fine in
    its place; a location is five keyword-only arguments, and a `None` among
    them that silently ignores the other four is a worse interface than a
    second name.

    `displaced` is read from the **locked** row, not from the copy the caller
    loaded -- the #400 rule, which applies here for the same reason it applies
    to the geocode: what this row said before is only knowable once nobody else
    can be writing it.

    What it must not become is a bulk path. One row, one finding, one note.
    """
    from services.enrichment_write import check_writable, locked_write

    if not prop:
        raise ValueError("no property")

    locked = check_writable(prop, commit)
    with locked_write(prop, locked=locked, commit=commit):
        displaced = None
        if prop.location_lat is not None and prop.location_lon is not None:
            displaced = {
                "lat": str(prop.location_lat),
                "lon": str(prop.location_lon),
                "accuracy": normalize_accuracy(prop.location_accuracy),
            }

        # Validated inside the lock, before anything is assigned: a bad
        # accuracy label or an out-of-range coordinate must leave the row as it
        # was, not half-written.
        enrichment = record_manual_coordinate(
            prop.enrichment,
            lat=lat,
            lon=lon,
            accuracy=accuracy,
            note=note,
            source=source,
            displaced=displaced,
        )

        prop.location_lat = float(lat)
        prop.location_lon = float(lon)
        prop.location_accuracy = normalize_accuracy(accuracy)
        prop.enrichment = enrichment
        flag_modified(prop, "enrichment")

    return {
        "stored": True,
        "accuracy": normalize_accuracy(accuracy),
        "displaced": displaced,
    }


def clear_location_by_hand(prop: Property, *, commit: bool = True) -> dict:
    """Take the hand-set block off, putting the row back on the computed path.

    The coordinate columns are **left alone**. Clearing does not restore what
    the block displaced, and that is deliberate: the block is not guaranteed to
    be newer than the columns -- a later script may have moved the point
    without touching it -- so putting the old coordinate back could undo a
    deliberate act rather than the one being cleared. The displaced values stay
    in the returned record so a person can put them back on purpose.
    """
    from services.enrichment_write import check_writable, locked_write

    if not prop:
        raise ValueError("no property")

    locked = check_writable(prop, commit)
    with locked_write(prop, locked=locked, commit=commit):
        previous = manual_coordinate(prop)
        if previous is None:
            return {"cleared": False, "previous": None}
        prop.enrichment = clear_manual_coordinate(prop.enrichment) or None
        flag_modified(prop, "enrichment")

    return {
        "cleared": True,
        "previous": {
            "lat": previous.lat,
            "lon": previous.lon,
            "accuracy": previous.accuracy,
            "note": previous.note,
            "source": previous.source,
        },
    }
