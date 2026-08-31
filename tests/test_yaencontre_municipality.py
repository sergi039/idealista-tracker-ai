"""The municipality a yaencontre card names, and the three ways it is spelled.

Measured on production 2026-08-31 over the 227 rows this parser had written:
119 named a real municipality, **45 named a district instead** ("Teis en
Vigo", "Bocines - Nembro - Cardo en Gozón", "La Calzada en Gijón") and 63
named nothing at all. `utils/municipality_grouping.py` groups four surfaces on
this string and `group_key()` returns a valid, distinct key for a district, so
each invented one became its own row on `/municipalities` with its own medians
and its own option in the `/properties` dropdown — and **Vigo had no group at
all**, its 8 listings spread across 5 district options with the municipality
itself unselectable.

Two fixes, each in the one place that owns its rule:

* the parser takes the last comma segment as before, then whatever follows the
  last " en " — which also recovers the 63 titles that carry no street and so
  no comma ("Casa en venta en Boiro");
* `normalize()` learns yaencontre's spelling of the article inversion.
  INE writes "Laracha, A" and idealista "A Laracha", both already folded;
  yaencontre writes "Laracha (A)", which folded to "laracha a" and grouped
  apart. Production carried "Laracha (A)" (4 rows) beside "Laracha" (1)
  before the parser change, so that split is older than this ticket.

Together they take those 227 rows to 227 naming a real INE municipality, with
no district string and nothing unnamed. The fixture asserted below is the real
alert of 2026-08-30 already committed for `tests/test_portal_alert_ingestion.py`.
"""

import json
import pathlib

import pytest

from services.yaencontre_source import _municipality_from_title, cards_in_email
from utils.municipality_codes import normalize
from utils.municipality_grouping import group_key

DATA = pathlib.Path(__file__).parent / "data"
ALERT = DATA / "yaencontre_alert_boiro.html"
INE = pathlib.Path(__file__).parent.parent / "data" / "ine_municipal.json"


@pytest.fixture(scope="module")
def alert() -> str:
    return ALERT.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "title,expected",
    [
        # The street form, unchanged.
        ("Casa adosada en venta en avenida Compostela, Outes", "Outes"),
        # A district behind the comma -- 45 live rows looked like this.
        ("Casa adosada en venta en calle Rosa, Teis en Vigo", "Vigo"),
        (
            "Solar en venta en calle Vioño, Bocines - Nembro - Cardo en Gozón",
            "Gozón",
        ),
        ("Chalet en venta en calle Tierno Galván, Maianca en Oleiros", "Oleiros"),
        # No street, so no comma at all -- 63 live rows looked like this and
        # were dropped entirely.
        ("Casa en venta en Boiro", "Boiro"),
        ("Casa adosada en venta en Esteiro en Ferrol", "Ferrol"),
        (
            "Casa pareada en venta en Cornazo - Rubianes en Vilagarcía de Arousa",
            "Vilagarcía de Arousa",
        ),
        # Neither a comma nor an " en ": still refused rather than guessed.
        ("Salinas / subida a San Martín", None),
        ("", None),
        (None, None),
    ],
)
def test_the_municipality_is_read_from_either_shape(title, expected):
    assert _municipality_from_title(title) == expected


def test_a_street_after_the_comma_is_never_mistaken_for_a_municipality():
    """The comma is read first on purpose.

    "Chalet en venta en calle Malata Da, Barreiros" contains " en " twice
    inside "en venta en", so a rule that split the whole title on the last
    " en " would answer "calle Malata Da, Barreiros".
    """
    assert (
        _municipality_from_title("Chalet en venta en calle Malata Da, Barreiros")
        == "Barreiros"
    )


def test_every_card_in_the_real_alert_names_a_real_municipality(alert):
    """End to end over the committed 2026-08-30 alert."""
    names = {
        v["name"]
        for v in json.loads(INE.read_text(encoding="utf-8"))["municipalities"].values()
    }
    keys = {normalize(n) for n in names}

    cards = cards_in_email(alert)
    assert len(cards) == 10

    for card in cards:
        assert card.municipality, f"no municipality for {card.title!r}"
        assert normalize(card.municipality) in keys, (
            f"{card.municipality!r} is not an INE municipality ({card.title!r})"
        )


def test_the_alert_no_longer_invents_a_district(alert):
    """Three of these ten cards carried a district before the fix."""
    municipalities = {c.municipality for c in cards_in_email(alert)}

    assert "Vilagarcía de Arousa" in municipalities
    assert not [m for m in municipalities if " en " in m]


@pytest.mark.parametrize(
    "portal,ine",
    [
        ("Laracha (A)", "Laracha, A"),
        ("Coruña (A)", "A Coruña"),
        ("Somozas (As)", "Somozas, As"),
        ("Valadouro (O)", "O Valadouro"),
        ("Pobra do Caramiñal (A)", "Pobra do Caramiñal, A"),
        ("Franco (El)", "Franco, El"),
    ],
)
def test_the_parenthesised_article_folds_onto_the_other_two_spellings(portal, ine):
    assert normalize(portal) == normalize(ine)
    assert group_key(portal) == group_key(ine)


def test_a_parenthesis_that_is_not_an_article_is_left_alone():
    """The rule keys on the article, not on the brackets.

    Production carries a title ending "Gijón (address hidden)"; folding that
    to "gijon" would claim a municipality the row does not state.
    """
    assert normalize("Gijón (address hidden)") != normalize("Gijón")


def test_no_two_real_municipalities_fold_onto_one_key():
    """The widened fold must not merge two different places.

    Checked across all 391 municipalities of the five watched provinces.
    """
    names = [
        v["name"]
        for v in json.loads(INE.read_text(encoding="utf-8"))["municipalities"].values()
    ]
    seen: dict = {}
    for name in names:
        key = normalize(name)
        assert key not in seen, f"{name!r} folds onto {seen[key]!r}"
        seen[key] = name
