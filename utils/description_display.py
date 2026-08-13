"""Display-side cleanup of the Gmail-alert boilerplate in descriptions.

The `description` column stores the raw text the alert email carried:
"Hello Sergioalicante, 1 new listing that matches your search criteria
99,000 € 309 €/m² 5 bed. 320 m2 CHANCE. Sale of housing …". The salutation,
the criteria sentence and the price/area token run repeat what the page
already shows in its own tiles; the words after them are the listing.

This module never touches the database (proposal D13): it produces what the
description card *shows*, the template keeps the raw text behind
"show original", and anything the patterns do not recognize with certainty
is left exactly as it came. Patterns are pinned by fixtures taken from the
owner's real emails in tests/test_description_display.py.
"""

from __future__ import annotations

import re

# The two alert openings, exactly as Gmail delivers them. `Hello <one word>,`
# only — a description that merely starts with "Hello" stays untouched.
_SALUTATION_RE = re.compile(r"^\s*Hello\s+\S+,\s*")
_CRITERIA_RE = re.compile(
    r"^(?:\d+\s+new\s+listings?\s+that\s+match(?:es)?\s+your\s+search\s+criteria\s*)"
)
_PRICE_DROP_RE = re.compile(
    r"^(?:The\s+price\s+of\s+this\s+listing\s+has\s+dropped\s+from\s+"
    r"[\d.,]+\s*€\s+to\s+[\d.,]+\s*€\s*)"
)

# The token run after the opening: prices, areas, bed counts, €/m², the ↓N%
# drop marker. Numbers and units only — words such as "urban" or "buildable"
# stay, because a real description can begin with them. The (?=\s|$) boundary
# is load-bearing: without it "3 bed" matched inside "3 bedroom" and "20 m"
# inside "20 minutes", so the loop bit into the listing's own first word
# (found by the Phase-1 diff review, 2026-08-13).
_NOISE_TOKEN_RE = re.compile(
    r"^(?:"
    r"[\d.,]+\s*€(?:/m²|/m2)?"  # 99,000 €  ·  309 €/m²
    r"|[\d.,]+\s*m2?²?"  # 320 m2 · 1,930 m²
    r"|\d+\s*bed\.?"  # 5 bed.
    r"|↓\s*\d+%"  # ↓10%
    r"|€"
    r")(?=\s|$)\s*",
    re.IGNORECASE,
)


def clean_description_for_display(text) -> dict:
    """The description as the card shows it.

    Returns ``{"text": str, "stripped": bool}``. ``stripped`` says whether
    anything was removed, which is what gates the "show original" control.
    A cleanup that would leave nothing keeps the original instead: an email
    that was *all* boilerplate still reads as itself rather than as blank.
    """
    if not isinstance(text, str) or not text.strip():
        return {"text": text or "", "stripped": False}

    cleaned = text
    for pattern in (_SALUTATION_RE, _PRICE_DROP_RE, _CRITERIA_RE):
        cleaned = pattern.sub("", cleaned, count=1)

    # Only an opening the patterns positively recognized licenses the token
    # loop: a description that merely *starts* with a figure ("320 m2 plot
    # with views") is the listing's own text, and eating its numbers is the
    # guessing this module promises not to do (diff review, 2026-08-13).
    if cleaned == text:
        return {"text": text, "stripped": False}

    # The price-drop alert repeats the figures after the sentence; the
    # criteria alert leads with them. Either way they are tokens, eaten one
    # at a time until the first thing that is not a number-with-unit.
    while True:
        trimmed = _NOISE_TOKEN_RE.sub("", cleaned, count=1)
        if trimmed == cleaned:
            break
        cleaned = trimmed

    cleaned = cleaned.strip()
    if not cleaned or cleaned == text.strip():
        return {"text": text, "stripped": False}
    return {"text": cleaned, "stripped": True}
