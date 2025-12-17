"""
Tests for EmailParser municipality extraction.
"""

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

