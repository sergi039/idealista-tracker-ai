import logging
import re
from typing import List, Optional

from sqlalchemy.orm.attributes import flag_modified

from services.coordinate_quality import (
    improves_on,
    normalize_accuracy,
    portal_coordinate,
)

from models import Property
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


def _municipality_agreement(prop, geo: dict):
    """(state, row_province, result_province).

    `state` is one of "agreed", "contradicted", "row_unmatched",
    "result_has_no_province" -- four values rather than a boolean, because
    "we could not compare" is a third answer and collapsing it into either
    real one is the mistake #98 is about. Only "contradicted" refuses.
    """
    row = _row_province(prop)
    result = _result_province(geo)
    if row is None:
        return "row_unmatched", row, result
    if result is None:
        return "result_has_no_province", row, result
    return ("agreed" if row == result else "contradicted"), row, result


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

    def ensure_coordinates(self, prop: Property, refresh: bool = False) -> bool:
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
        """
        if not prop:
            return False

        # What the row is being asked to give up, remembered before `refresh`
        # throws it away. Two separate hazards, and the clearing below creates
        # both:
        #
        # * the geocode answers, no better than what was there. Measured on
        #   property 733 (2026-08-17): a fotocasa pin placed for that advert
        #   was replaced by the Llaranes district centroid 2447 m away, still
        #   `approximate`, so nothing was unlocked and the listing-specific
        #   point was gone. Issue #393.
        # * the geocode answers *nothing*. Every candidate query is refused,
        #   the method returns False, and the row is left with no coordinate at
        #   all -- worse than the one it started with, and not what anybody
        #   pressing "refresh" is asking for.
        #
        # Only a portal pin is defended. A coordinate this same geocoder wrote
        # last month has no better claim than the one it writes today.
        portal_pin = portal_coordinate(prop) if refresh else None
        previous_accuracy = normalize_accuracy(prop.location_accuracy)

        if refresh:
            prop.location_lat = None
            prop.location_lon = None
            prop.location_accuracy = "unknown"
            enrichment = prop.enrichment if isinstance(prop.enrichment, dict) else {}
            if isinstance(enrichment, dict):
                enrichment.pop("geocoding", None)
                prop.enrichment = enrichment or None
                flag_modified(prop, "enrichment")

        if prop.location_lat and prop.location_lon:
            return True

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

            agreement, row_province, result_province = _municipality_agreement(
                prop, geo
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

            accuracy = str(geo.get("accuracy") or "").strip().lower() or "unknown"
            if accuracy not in {"precise", "approximate", "unknown"}:
                accuracy = "unknown"

            if portal_pin is not None and not improves_on(accuracy, previous_accuracy):
                # An even trade is not a trade: see the note at the top of this
                # method. The attempt is recorded on the row so the next reader
                # -- or the next press of the button -- knows it was made and
                # what it answered, rather than trying it again for the same
                # money and the same result.
                self._keep_portal_pin(
                    prop,
                    portal_pin,
                    previous_accuracy,
                    query=query,
                    answered=geo.get("formatted_address"),
                    answered_accuracy=accuracy,
                )
                return True

            prop.location_lat = new_lat
            prop.location_lon = new_lon
            prop.location_accuracy = accuracy

            enrichment = prop.enrichment or {}
            enrichment["geocoding"] = {
                "query": query,
                "formatted_address": geo.get("formatted_address"),
                "accuracy": accuracy,
                # Whether anyone confirmed this result is about the row's own
                # municipality. Recorded rather than assumed: three of these
                # four values mean the coordinate was accepted *unchecked*, and
                # a row that reads "agreed" is a stronger claim than one that
                # reads "row_unmatched". Same reason `sea_view_service` stamps
                # `origin_unverified` instead of quietly treating unverified
                # provenance as verified.
                "municipality_check": agreement,
            }
            prop.enrichment = enrichment
            flag_modified(prop, "enrichment")
            return True

        if portal_pin is not None:
            # Nothing answered, and the row had a pin before this method threw
            # it away. Putting it back is not undoing the refresh -- the refusal
            # is still recorded below, on the row -- it is refusing to make the
            # row *less* located than it was.
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

        return False
