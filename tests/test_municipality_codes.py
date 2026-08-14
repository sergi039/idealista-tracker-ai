"""Joining Idealista municipality names to INE codes.

`utils/municipality_codes.py` is the join key between what the portal calls a
place and what INE calls it. What is pinned here: normalization folds accents,
case, hyphens and INE's comma-article form ("Franco, El" vs "El Franco") onto
one key; the alias table bridges the portal names verified against the live
database on 2026-08-13; and everything else is an honest miss — `match`
returns None rather than fuzzy-guessing, and a code outside the five watched
provinces (15/27/32/33/36) is never returned, because country-wide the names
collide ("Mieres" exists in Girona too).
"""

import pytest

from utils.municipality_codes import (
    ALIASES,
    PROVINCE_CODES,
    build_index,
    match,
    normalize,
)

# A small index in the shape `build_index` produces: normalized INE name ->
# 5-digit code. Codes are the real ones from diccionario26.xlsx.
INDEX = {
    "navia": "33041",
    "franco": "33023",  # INE spells it "Franco, El"
    "valdes": "33034",
    "mieres": "33037",
    "pilona": "33049",
    "muros de nalon": "33039",
    "vilalba": "27065",
    "coruna": "15030",
    "pontes de garcia rodriguez": "15070",
}


class TestNormalize:
    def test_lowercases_and_strips_accents(self):
        assert normalize("Valdés") == "valdes"
        assert normalize("Piloña") == "pilona"
        # The portal itself is inconsistent about accents ("Castrillon" is a
        # live DB spelling of Castrillón) — both sides land on the same key.
        assert normalize("Castrillon") == normalize("Castrillón")

    def test_collapses_whitespace_and_hyphens(self):
        assert normalize("Luarca - Valdés") == "luarca valdes"
        assert normalize("Cerdedo-Cotobade") == "cerdedo cotobade"
        assert normalize("  Soto  Del   Barco ") == "soto del barco"

    def test_ine_article_form_matches_portal_form(self):
        # INE writes "Franco, El"; the portal writes "El Franco".
        assert normalize("Franco, El") == "franco"
        assert normalize("El Franco") == "franco"
        # Galician articles, both genders and plural.
        assert normalize("Coruña, A") == normalize("A Coruña") == "coruna"
        assert (
            normalize("Pontes de García Rodríguez, As")
            == normalize("As Pontes de García Rodríguez")
            == "pontes de garcia rodriguez"
        )
        assert normalize("Barco de Valdeorras, O") == normalize("O Barco de Valdeorras")

    def test_article_only_dropped_as_a_whole_word(self):
        # "La" inside "Laracha" or "Lalín" is not an article.
        assert normalize("Laracha, A") == "laracha"
        assert normalize("Lalín") == "lalin"
        assert normalize("Oviedo") == "oviedo"


class TestAliases:
    def test_alias_table_is_normalized_on_both_sides(self):
        for key, value in ALIASES.items():
            assert key == normalize(key)
            assert value == normalize(value)

    @pytest.mark.parametrize(
        "portal_name,expected_code",
        [
            ("Villalba", "27065"),  # INE: Vilalba
            ("Mieres del Camino", "33037"),  # INE: Mieres
            ("Luarca - Valdés", "33034"),  # INE: Valdés
            ("Infiesto", "33049"),  # capital of Piloña
            ("San Esteban", "33039"),  # capital of Muros de Nalón
        ],
    )
    def test_verified_portal_names_resolve(self, portal_name, expected_code):
        assert match(portal_name, INDEX) == expected_code


class TestMatch:
    def test_plain_and_article_forms_resolve(self):
        assert match("Navia", INDEX) == "33041"
        assert match("El Franco", INDEX) == "33023"
        assert match("A Coruña", INDEX) == "15030"

    def test_unknown_name_is_none_not_a_guess(self):
        # Close to "Navia", but no fuzzy matching: an honest miss.
        assert match("Navia de Suarna II", INDEX) is None
        assert match("", INDEX) is None

    def test_out_of_scope_province_is_never_returned(self):
        # "Mieres" also exists in Girona (17109). An index polluted with a
        # country-wide code must not produce a cross-province join.
        assert match("Mieres", {"mieres": "17109"}) is None

    def test_province_codes_are_the_five_watched(self):
        assert PROVINCE_CODES == {"15", "27", "32", "33", "36"}


class TestBuildIndex:
    def test_inverts_and_restricts_to_watched_provinces(self):
        index = build_index(
            {
                "33041": "Navia",
                "33023": "Franco, El",
                "17109": "Mieres",  # Girona: dropped, not indexed
            }
        )
        assert index == {"navia": "33041", "franco": "33023"}

    def test_collision_raises_instead_of_picking_one(self):
        with pytest.raises(ValueError):
            build_index({"33041": "Navia", "27901": "Navia"})

    def test_same_code_listed_twice_is_not_a_collision(self):
        index = build_index({"33041": "Navia"})
        index2 = build_index({"33041": "Navia", "33023": "Franco, El"})
        assert index["navia"] == index2["navia"] == "33041"
