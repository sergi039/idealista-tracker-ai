"""A Spanish portal title's own "en" is not the location separator (GEO-003, #265).

`_LOCATION_FROM_TITLE_RE` takes the text after the leftmost "in"/"en", which
is right for idealista's English alerts ("Land in Tiñana, Siero") and wrong for
the Spanish portals, whose type name carries an "en" of its own: in "Chalet en
venta en calle Fiobre, Bergondo" the marker sits at word 2, and the query sent
to Google was `venta en calle Fiobre, Bergondo, Spain`. Measured on production
2026-09-02: 375 stored geocoding queries begin "venta en " (203 in the Galicia
subscription, three of them `precise`), and 451 titles carry the grammar --
yaencontre 377, fotocasa 64, idealista 10.

The fix anchors on the portal grammar -- `<type> en (venta|alquiler) en
<location>`, the type bounded by the same measured cap -- and takes what follows
as the location. What it deliberately does NOT do is split on the *last* "en",
or re-apply the leftmost-marker rule to what the grammar leaves: yaencontre
writes a second "en" between a district and its municipality ("..., Feal-Xuvia
en Narón", 137 of the 451), and either reading cuts row 1377's query to
`Narón, Spain`. That tail is read the way `services/yaencontre_source` already
reads it for the municipality column, and written as a comma component.

Every expected query below is pinned by value. The titles are production
rows' own, verbatim; the controls are the lines that were already correct.

This changes only the question asked of the geocoder from now on. The 375
stored coordinates are untouched; re-geocoding them is a separate, billed
decision.
"""

import pytest

from models import Property
from services.property_location_service import _build_geocoding_queries
from services.yaencontre_source import _municipality_from_title, split_district


def _prop(title, municipality=None):
    return Property(source_email_id="geo-003", title=title, municipality=municipality)


class TestTheTypeGoesAsAWhole:
    @pytest.mark.parametrize(
        "title,municipality,expected",
        [
            # 1379 (and 1680, the same advert twice): yaencontre, a street.
            (
                "Chalet en venta en calle Fiobre, Bergondo",
                "Bergondo",
                "calle Fiobre, Bergondo, Spain",
            ),
            # 1445: yaencontre, the third `precise` row.
            (
                "Casa en venta en calle Lugar Vilacendoisan Martiño, Foz",
                "Foz",
                "calle Lugar Vilacendoisan Martiño, Foz, Spain",
            ),
            # 1284: idealista's Spanish alert, and a four-word type name that
            # passes the cap exactly -- this is not a yaencontre-only defect.
            (
                "Casa o chalet independiente en venta en Estrada de Castela, 907, Narón",
                "Narón",
                "Estrada de Castela, 907, Narón, Spain",
            ),
        ],
    )
    def test_the_three_real_shapes(self, title, municipality, expected):
        queries = _build_geocoding_queries(_prop(title, municipality))
        assert queries[0] == expected
        assert not any(q.lower().startswith("venta en") for q in queries)

    def test_the_rental_spelling_is_the_same_grammar(self):
        """Not a production title -- no rental alert has arrived -- but the
        portal's other spelling of the same marker."""
        queries = _build_geocoding_queries(
            _prop("Piso en alquiler en calle Real, Ferrol")
        )
        assert queries[0] == "calle Real, Ferrol, Spain"


class TestTheDistrictTailIsAComponent:
    def test_row_1377_keeps_its_street(self):
        """The shape that rules out a last-"en" split, and rules out searching
        the remainder again: "calle De Castela, Feal-Xuvia" is exactly four
        words, so the leftmost-marker rule would pass it and answer `Narón`."""
        queries = _build_geocoding_queries(
            _prop("Chalet en venta en calle De Castela, Feal-Xuvia en Narón", "Narón")
        )
        assert queries == ["calle De Castela, Feal-Xuvia, Narón, Spain", "Narón, Spain"]

    def test_a_title_with_no_street_has_no_comma_and_still_splits(self):
        """Row 1378's shape."""
        queries = _build_geocoding_queries(
            _prop("Chalet en venta en Esteiro en Ferrol", "Ferrol")
        )
        assert queries == ["Esteiro, Ferrol, Spain", "Ferrol, Spain"]

    @pytest.mark.parametrize(
        "title",
        [
            "Chalet en venta en calle De Castela, Feal-Xuvia en Narón",
            "Chalet en venta en Esteiro en Ferrol",
            "Terreno en venta en calle Fuente Feans, Mesoiro en Coruña (A)",
            "Casa adosada en venta en avenida Compostela, Outes",
        ],
    )
    def test_the_querys_last_component_is_the_municipality_the_parser_stores(
        self, title
    ):
        """One split, two readers: the geocoding query and the municipality
        column come from `split_district`, so they cannot name different places."""
        query = _build_geocoding_queries(_prop(title))[0]
        assert query.endswith(", Spain")
        assert query.rsplit(", ", 2)[-2] == _municipality_from_title(title)

    def test_the_split_is_on_the_last_en(self):
        assert split_district("Lugar en Medio en Foz") == ("Lugar en Medio", "Foz")
        assert split_district("Teis en Vigo") == ("Teis", "Vigo")
        assert split_district("Outes") is None
        assert split_district("") is None
        assert split_district(None) is None


class TestWhatIsUnchanged:
    @pytest.mark.parametrize(
        "title,expected",
        [
            # idealista, English: the form the rule was written for.
            (
                "Detached house in Barrio de Prendonés, 1, El Franco",
                "Barrio de Prendonés, 1, El Franco, Spain",
            ),
            # idealista, Spanish, without the grammar: the type's "en" IS the
            # separator here, and 232 production titles are this shape.
            (
                "Casa o chalet independiente en Lugar Costenla, 31, Carballo",
                "Lugar Costenla, 31, Carballo, Spain",
            ),
            # fotocasa, English (row 733's, #393).
            ("Land for sale in Llaranes, Avilés", "Llaranes, Avilés, Spain"),
        ],
    )
    def test_the_controls(self, title, expected):
        assert _build_geocoding_queries(_prop(title))[0] == expected

    def test_prose_before_the_grammar_is_still_prose(self):
        """The cap's intent survives: a description that happens to say "en
        venta en" behind a six-word run is not a title, and it falls through to
        the whole title exactly as the leftmost-marker rule already did. No
        production title has a type name past four words (451 measured)."""
        title = "Magnífica casa de piedra con finca en venta en Foz"
        assert _build_geocoding_queries(_prop(title))[0] == f"{title}, Spain"
