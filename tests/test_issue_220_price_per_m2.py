"""Issue #220: the price per m² was stored as the asking price.

Every idealista alert states both figures — `99,000 € 309 €/m²` — and two
regexes in `utils/idealista_extractors.py` counted the second one as a price:

* `extract_price_change()` reads "two amounts in one body" as old + new, so an
  ordinary new-listing email was classified as a price change;
* `extract_price()` asks that function first and returns its "new price", so it
  never reached its own patterns.

The owner saw `PRICE €309` on a €99,000 house, and both AI providers reported
the listing as mislabelled. 20 of 360 stored listings were affected, and since
`_score_price_per_m2` divides price by area — and pools every listing's price
per m² for the peer median — those rows also skewed the price score of the
whole database.

The fix is one `_PRICE_AMOUNT` building block that rejects an amount carrying a
per-unit suffix; the last test here is what stops a future pattern from
forgetting it.
"""

import re

import pytest

from utils import idealista_extractors as ex
from utils.email_parser import EmailParser
from utils.idealista_extractors import extract_price, extract_price_change

# The real wording of the alert that exposed this, listing 360.
OWNER_BODY = (
    "Hello Sergioalicante, 1 new listing that matches your search criteria "
    "99,000 € 309 €/m² 5 bed. 320 m2 CHANCE. Sale of housing under "
    "construction of 320 m2 and land of 1020 m2."
)


@pytest.mark.parametrize(
    "body, expected",
    [
        (OWNER_BODY, 99000.0),
        # Spanish grouping: '.' for thousands, and the same per-m² tail.
        ("Nueva vivienda 99.000 € 309 €/m² 5 hab. 320 m2", 99000.0),
        # A grouped per-m² figure is still a per-m² figure (listing 323).
        ("1 new listing 90,000 € 1,452 €/m² 1747 m2", 90000.0),
        # idealista writes both m² and m2.
        ("78,900 € 242 €/m2 4 bed. 326 m2", 78900.0),
        # Spacing around the slash must not smuggle it back in.
        ("110,000 € 840 € / m² 130 m2", 110000.0),
        ("Precio: 125.000 € 401 €/m²", 125000.0),
    ],
)
def test_the_asking_price_wins_over_the_price_per_m2(body, expected):
    assert extract_price(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "Reduced to 309 €/m²",
        "1.452 €/m² en esta zona",
        # A rental figure is not an asking price either.
        "Alquiler 950 €/mes",
    ],
)
def test_a_unit_price_alone_is_not_a_price(body):
    """Better no price than the wrong one: the importer skips a listing with no
    price, and that is the honest outcome for a body that never states one."""
    assert extract_price(body) is None


def test_a_plain_listing_is_not_a_price_change():
    """The two-amounts fallback is what turned '99,000 € 309 €/m²' into a
    reduction from 99,000 to 309."""
    assert extract_price_change(OWNER_BODY) == (None, None)


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            "The price of this listing has dropped from 150,000 € to 140,000 € "
            "422 €/m² 300 m2",
            (150000.0, 140000.0),
        ),
        (
            "El precio ha bajado de 150.000 € a 140.000 € 422 €/m² 300 m2",
            (150000.0, 140000.0),
        ),
        # The struck-through form, with the same per-m² tail after it.
        ("<s>150.000 €</s> 140.000 € 422 €/m² 300 m2", (150000.0, 140000.0)),
        # Two real amounts, no explicit wording: still old + new.
        ("was 150,000 € now 140,000 €", (150000.0, 140000.0)),
    ],
)
def test_a_real_price_change_still_resolves_to_the_new_price(body, expected):
    assert extract_price_change(body) == expected
    assert extract_price(body) == expected[1]


def test_the_importer_path_reads_the_same_price():
    """`EmailParser` is what ingestion actually calls; pin the whole path, not
    only the helper underneath it."""
    assert EmailParser()._extract_price(OWNER_BODY) == 99000.0


def test_the_area_is_untouched():
    """The exclusion must not disturb the m² the same line carries."""
    assert ex.extract_area_m2(OWNER_BODY) == 320.0


def test_every_price_regex_is_built_from_the_one_excluding_building_block():
    """A new `<number> €` pattern that skips `_PRICE_AMOUNT` brings the defect
    straight back, and it would look perfectly reasonable in review.

    Two spellings are legitimate: the amount carries `_PER_UNIT_SUFFIX` (it is a
    price), or it is followed by `/m²` (it is deliberately reading the unit
    price, as `extract_price_per_m2` does). Anything else is the defect."""
    with open(ex.__file__, encoding="utf-8") as handle:
        source = handle.read()

    offenders = re.findall(
        r"\{_PRICE_NUMBER\}\\s\*€(?!\{_PER_UNIT_SUFFIX\}|\\s\*/)", source
    )
    assert not offenders, (
        "a euro amount is matched without the per-unit exclusion; build it from "
        f"_PRICE_AMOUNT instead ({len(offenders)} occurrence(s))"
    )
