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


def test_extract_price_prefers_new_price_for_price_reduction_emails():
    parser = EmailParser()
    body = "The price of this listing has dropped from 290,000€ to 285,000€"
    assert parser._extract_price(body) == 285000.0


def test_extract_price_handles_dot_separators_for_price_reduction_emails():
    parser = EmailParser()
    body = "El precio ha bajado de 290.000 € a 285.000 €"
    assert parser._extract_price(body) == 285000.0
