"""Did the geocoder answer the address that was asked? (#535)

`location_accuracy = precise` is Google's ROOFTOP, and
`services/coordinate_quality.py` grants it zero slack: sea distance, drive
times and the hazard band are all scored as if the coordinate were the parcel.
#493 spent its whole argument on what `approximate` is worth and never asked
whether `precise` earns its zero. #535 did.

Measured on production, 2026-09-02 (the comment on #535): 152 of the 166
`precise` rows were written by the geocoder, every one of them a ROOFTOP by
construction, and **not one carries a second coordinate** to check against --
no portal pin, no cadastre block, no hand-set location. The only ROOFTOP a
person ever checked is row 360, "Barrio de Prendonés, 1": Google matched house
1 on a *travesía named after* the village, in the municipal capital, 2868 m
from house 1 in it. What the stored record *can* say for free is whether the
ROOFTOP is the address that was asked, and for 23 of the 152 it was not: 17
answered a house number to a query that named none ("calle Fiobre, Bergondo"
-> "Rua Fiobre, 100"), 6 answered a different street or number ("pillarno -
el calello, 83" -> "Av. Eysines, 10").

This module is that comparison, at the one granularity the record supports:
the **house number**. A ROOFTOP is Google's claim to have matched one
building. A query that named no building cannot have earned that claim, and a
query that named a different one refutes it. Withdrawn, the label becomes
`approximate` -- what any other answer to the same query is worth, the street
or the village -- and the row takes the slack that label already carries.
Nothing here widens `precise` globally, and nothing relabels from a distance:
the issue names both as the fix that must not happen.

The street name is deliberately NOT compared. Google answers in the local
language and its own abbreviations -- "Rambla de la Libertad" comes back
"Rbla. de la Llibertat", "SI-6, 24" comes back "SI-6, 24b" -- and the hand
review behind #535 discarded 13 of the 36 token mismatches as spelling. A rule
with a measured one-in-three false-positive rate would take `precise` off rows
that earned it, which is the "guard too wide" mistake the issue names.

Two blind spots, stated so nobody reads a passing check as verification:

* **a same-number answer somewhere else.** Row 360 asked for "Prendonés, 1"
  and Google answered "Tr.ª de Prendonés, 1". The number agrees; this check
  passes it; only the owner's own pin fixed that row;
* **a different street with the same number**, for the reason above. Row 25:
  "calle Tarancon, 6" -> "Av. de Salamanca, 6".

Re-run over the same rows on 2026-09-07 (168 `precise` by then) through the
stored `query` and `formatted_address`: 21 refuted -- 20 of the hand review's
23, plus row 1759, ingested after the review -- and none of the 129 rows the
review passed. The three of the 23 it does not catch are the blind spots
above (25) and two rows whose answer names no number at all (438, 765). The 15
a naive form flagged beyond that were the same number twice: a letter suffix
Google added or dropped ("24" against "24b", "3 a" against "3, a") and a
number the query wrote without a comma ("Barrio Otero 15"). Both are
refinements, not contradictions -- the distinction the issue itself draws
between moving a pin 28 m and 2868 m.
"""

import re
from typing import Any, Optional, Set

# The four states the check can answer. Every way of *not* being able to
# compare stays its own answer rather than borrowing one of the two real ones
# (#98) -- the same reason the province and municipality checks in
# `services/property_location_service.py` have four and five.
AGREED = "agreed"
NUMBER_NOT_ASKED = "number_not_asked"
DIFFERENT_NUMBER = "different_number"
NO_NUMBER_ANSWERED = "answer_names_no_number"

# Only a positive finding withdraws `precise`. `answer_names_no_number` is a
# cannot-tell -- a caserío matched by name has no number to refute -- and a
# cannot-tell keeps the label, because widening the refusal to everything
# unverified would take `precise` off 129 rows on the strength of one
# measured error.
REFUTING_STATES = frozenset({NUMBER_NOT_ASKED, DIFFERENT_NUMBER})

# The grammar of a Spanish house number and nothing else: one to four digits,
# then optionally a short suffix ("24b", "3 a", "12 bis") and optionally a
# second number for a range ("12-14"). Never five digits -- in a Spanish
# address that is the postal code -- and never a word longer than three
# letters, which is how "2 Planta" and "1 de Mayo" stay street text. The
# independent review of #556 supplied both misreadings the grammar now
# refuses: "12 bis" read as no number at all, and the 8 of "Avenida 8 de
# Marzo" read as a number the query had asked for.
_NUMBER_GRAMMAR = (
    r"(?P<digits>\d{1,4})(?:[\s\-]?[A-Za-z]{1,3})?(?:[\s\-]\d{1,4}[A-Za-z]?)?"
)

# A component that is a house number and nothing else: Google's
# `street_number`, or the component its Spanish formatted addresses put
# between the route and the postal code ("Rúa Xoiña, 8, 27788 Foz, Lugo").
_NUMBER_COMPONENT_RE = re.compile(rf"^{_NUMBER_GRAMMAR}$")

# A house number a query named: the number that ENDS a comma-separated
# component, the way a Spanish address is written -- "Lugar Costenla, 31",
# "Barrio Otero 15" -- standing after a space or a comma. A number inside a
# street's name ("Avenida 8 de Marzo") or glued to a road code ("SI-6") or a
# price ("1.500") is not one.
_QUERY_NUMBER_RE = re.compile(rf"(?:^|\s){_NUMBER_GRAMMAR}\s*$")
_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def query_house_numbers(query: Any) -> Set[str]:
    """Every house number the query named, as digits. Empty when it named none.

    Read per comma-separated component, at its end, with a trailing
    parenthetical dropped first ("Lugar el Pueblo 128 (Gozón)" names 128).
    Still generous where it is safe to be: a "km 3" reads as a number the
    query named, and that can only make the check *keep* a label, never
    withdraw one. The digits alone are kept because the suffix is the part
    Google rewrites ("24" against "24b").
    """
    numbers = set()
    for part in str(query or "").split(","):
        part = _TRAILING_PARENTHETICAL_RE.sub("", part.strip())
        match = _QUERY_NUMBER_RE.search(part)
        if match:
            numbers.add(match.group("digits"))
    return numbers


def _digits(value: Any) -> Optional[str]:
    match = _NUMBER_COMPONENT_RE.match(str(value or "").strip())
    return match.group("digits") if match else None


def answered_house_number(geo: Any) -> Optional[str]:
    """The house number Google's answer names, as digits, from its components.

    `street_number` is the typed component, so this reads a fact rather than
    parsing prose. A compound number reads as its leading digits ("12 bis" is
    12, "12-14" is 12), so a mismatch behind a suffix is still a mismatch.
    "S/N" -- sin número -- is a component with no digits and reads as none. A
    missing component is none too: a rooftop matched by name names no number
    to refute.
    """
    if not isinstance(geo, dict):
        return None
    for component in geo.get("address_components") or ():
        if not isinstance(component, dict):
            continue
        if "street_number" not in (component.get("types") or ()):
            continue
        return _digits(component.get("long_name") or component.get("short_name"))
    return None


def house_number_in_formatted(formatted: Any) -> Optional[str]:
    """The house number a stored `formatted_address` names, as digits.

    For the records written before this module existed, which kept the
    string and dropped the components. The first comma-separated component
    that is a number and nothing else. A postal code never is, because it is
    five digits and travels with the town ("15165 A Coruña"); a road number
    never is, because it travels with the road ("Nacional 632").
    """
    for part in str(formatted or "").split(","):
        digits = _digits(part)
        if digits is not None:
            return digits
    return None


def house_number_agreement(query: Any, answered: Optional[str]) -> str:
    """One of the four states, for a query and the number its answer named."""
    if answered is None:
        return NO_NUMBER_ANSWERED
    asked = query_house_numbers(query)
    if not asked:
        return NUMBER_NOT_ASKED
    if answered in asked:
        return AGREED
    return DIFFERENT_NUMBER


def earns_precise(state: str) -> bool:
    """Does a ROOFTOP with this address check keep the `precise` label?"""
    return state not in REFUTING_STATES
