"""
Tests for EmailParser municipality extraction.
"""

import pytest

from utils.email_parser import EmailParser


def test_extract_municipality_from_title_handles_numbers_and_price_commas():
    parser = EmailParser()
    title = "Land in La Faza, 280, Caldones, Gijón 85,000 €"
    municipality = parser._extract_municipality_from_title(title)
    assert municipality is not None
    assert municipality.lower().startswith("gij")


def test_extract_municipality_from_title_handles_spanish_prefix_and_price_commas():
    parser = EmailParser()
    title = "Terreno en La Faza, 280, Caldones, Gijón 85,000 €"
    municipality = parser._extract_municipality_from_title(title)
    assert municipality is not None
    assert municipality.lower().startswith("gij")


def test_extract_price_prefers_new_price_for_price_reduction_emails():
    parser = EmailParser()
    body = "The price of this listing has dropped from 290,000€ to 285,000€"
    assert parser._extract_price(body) == 285000.0


def test_extract_price_handles_dot_separators_for_price_reduction_emails():
    parser = EmailParser()
    body = "El precio ha bajado de 290.000 € a 285.000 €"
    assert parser._extract_price(body) == 285000.0


@pytest.mark.parametrize(
    "area_text, expected",
    [
        ("1.373 m²", 1373.0),  # Spanish thousands grouping (dot)
        ("25.000 m²", 25000.0),  # Spanish thousands grouping, round number
        ("1373 m²", 1373.0),  # plain digits, no separator
        ("1,373 m²", 1373.0),  # English thousands grouping (comma)
    ],
)
def test_extract_area_handles_spanish_format_and_unseparated_areas(area_text, expected):
    """Regression for GH #22: EmailParser._extract_area() had the identical
    unanchored-regex defect as extract_area_m2() (and extract_price(), #21)
    -- it returned 373.0 for "1.373 m²" instead of 1373.0, and its >= 100 m²
    sanity floor didn't catch it because 373 is itself >= 100."""
    parser = EmailParser()
    text = f"Land in Bar, Gijón {area_text}"
    assert parser._extract_area(text) == expected


def test_extract_area_enforces_land_sanity_floor():
    """The legacy land-only pipeline rejects areas below 100 m² as a parse
    artifact rather than a real plot size (unaffected by the GH #22 fix)."""
    parser = EmailParser()
    assert parser._extract_area("85 m²") is None
