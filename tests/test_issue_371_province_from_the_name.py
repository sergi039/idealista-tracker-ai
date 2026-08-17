"""The province check reads the name when there is no postal code (issue #371).

#348 compares the province of Google's answer with the province of the row's
own municipality, and refuses a contradiction. It read that province from the
postal code alone, so it could say nothing about the 104 of 406 production rows
whose answer carries none — every one of which has a resolvable municipality,
so only the answer's side of the comparison was missing. A Vielha-shaped error
(precise, plausible, wrong province) was therefore undetectable for a quarter
of the table because Google happened to omit a postcode.

Verified with one live Geocoding call on a query production had recorded
without one:

    component: ['administrative_area_level_2', 'political'] | Asturias | O

so the province is in the answer either way. What these tests pin is that
reading it did not cost the third state: a name that is not one of Spain's 52
provinces — the autonomous community "Galicia", which one production row
carries — still reports "cannot tell" rather than contradicting anything.
"""

import pytest

from app import create_app, db
from models import Property
from services.property_location_service import PropertyLocationService
from tests import setup_test_environment
from utils.municipality_codes import province_code_for_name


def _named(province=None, community=None, postcode=None, locality="Somewhere"):
    """An answer as Google shapes it, with only the parts a case needs."""
    parts = [{"long_name": locality, "short_name": locality, "types": ["locality"]}]
    if postcode:
        parts.append(
            {"long_name": postcode, "short_name": postcode, "types": ["postal_code"]}
        )
    if province:
        parts.append(
            {
                "long_name": province,
                "short_name": province[:1],
                "types": ["administrative_area_level_2", "political"],
            }
        )
    if community:
        parts.append(
            {
                "long_name": community,
                "short_name": community,
                "types": ["administrative_area_level_1", "political"],
            }
        )
    return parts


def _answer(components, formatted, lat=43.4, lng=-5.6):
    return {
        "lat": lat,
        "lng": lng,
        "formatted_address": formatted,
        "types": ["locality", "political"],
        "accuracy": "approximate",
        "address_components": components,
    }


# Asturias by name, no postal code -- the 83-row shape in production.
ASTURIAS_BY_NAME = _answer(
    _named(province="Asturias", locality="Municipality of Siero"),
    "Municipality of Siero, Asturias, Spain",
)
# The Vielha error, arriving without a postcode this time.
LLEIDA_BY_NAME = _answer(
    _named(province="Lleida", locality="Vielha"),
    "Vielha, Lleida, Spain",
    lat=42.7030823,
    lng=0.7933967,
)
# An answer that names the community and no province at all.
GALICIA_ONLY = _answer(
    _named(community="Galicia", locality="Cedeira"),
    "Cedeira, Galicia, Spain",
    lat=43.66,
    lng=-8.06,
)
# Both present and disagreeing: the postal code is the one that counts.
POSTCODE_BEATS_THE_NAME = _answer(
    _named(province="Lleida", postcode="33510", locality="Pola de Siero"),
    "33510 Pola de Siero, Asturias, Spain",
)
# The Nominatim fallback returns no components at all (utils/geocoding.py).
NO_COMPONENTS = _answer([], "Somewhere, Spain")
# An archived subscription's real province, by name.
ALICANTE_BY_NAME = _answer(
    _named(province="Alicante", locality="Sant Joan d'Alacant"),
    "Sant Joan d'Alacant, Alicante, Spain",
    lat=38.401717,
    lng=-0.4356604,
)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _prop(municipality, **kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", "issue_371"),
        title=kw.pop("title", "Land in Tiñana, Viella-Granda-Meres, Siero"),
        municipality=municipality,
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _service(answer):
    service = PropertyLocationService()
    service.geocoding_service.geocode_address = lambda address: answer
    return service


def _stored(prop_id):
    db.session.expire_all()
    return db.session.get(Property, prop_id)


class TestTheNameIsReadWhenThereIsNoPostcode:
    def test_the_right_province_by_name_agrees(self, app):
        with app.app_context():
            prop = _prop("Siero")
            assert _service(ASTURIAS_BY_NAME).ensure_coordinates(prop) is True
            db.session.commit()

            record = _stored(prop.id).enrichment["geocoding"]
            assert record["municipality_check"] == "agreed"
            assert _stored(prop.id).location_lat is not None

    def test_the_wrong_province_by_name_is_refused(self, app):
        with app.app_context():
            prop = _prop("Siero", source_email_id="issue_371_wrong")
            assert _service(LLEIDA_BY_NAME).ensure_coordinates(prop) is False
            db.session.commit()

            stored = _stored(prop.id)
            assert stored.location_lat is None
            record = stored.enrichment["geocoding"]
            assert record["refused"] == "result_in_wrong_province"
            assert (record["row_province"], record["result_province"]) == ("33", "25")


class TestTheThirdStateSurvives:
    def test_an_autonomous_community_is_not_a_province(self, app):
        """ "Galicia" names no province, so it can neither agree nor disagree."""
        with app.app_context():
            prop = _prop("Cedeira", source_email_id="issue_371_galicia")
            assert _service(GALICIA_ONLY).ensure_coordinates(prop) is True
            db.session.commit()

            record = _stored(prop.id).enrichment["geocoding"]
            assert record["municipality_check"] == "result_has_no_province"

    def test_an_answer_with_no_components_cannot_be_compared(self, app):
        """The Nominatim fallback returns none; that is not agreement."""
        with app.app_context():
            prop = _prop("Siero", source_email_id="issue_371_nocomp")
            assert _service(NO_COMPONENTS).ensure_coordinates(prop) is True
            db.session.commit()

            record = _stored(prop.id).enrichment["geocoding"]
            assert record["municipality_check"] == "result_has_no_province"


class TestWhatMustNotChange:
    def test_the_postal_code_still_decides_when_both_are_present(self, app):
        """A code needs no name table, so it stays the preferred source."""
        with app.app_context():
            prop = _prop("Siero", source_email_id="issue_371_both")
            assert _service(POSTCODE_BEATS_THE_NAME).ensure_coordinates(prop) is True
            db.session.commit()

            record = _stored(prop.id).enrichment["geocoding"]
            assert record["municipality_check"] == "agreed"

    def test_alicante_still_geocodes_by_name(self, app):
        """The archived subscriptions are outside INE scope and must survive."""
        with app.app_context():
            prop = _prop(
                "Sant Joan d'Alacant",
                source_email_id="issue_371_alicante",
                title="Flat in Sant Joan d'Alacant",
            )
            assert _service(ALICANTE_BY_NAME).ensure_coordinates(prop) is True
            db.session.commit()

            assert _stored(prop.id).location_lat is not None


class TestTheProvinceTable:
    @pytest.mark.parametrize(
        "name,code",
        [
            ("Asturias", "33"),
            ("A Coruña", "15"),
            ("La Coruña", "15"),  # the same province, the other spelling
            ("Lugo", "27"),
            ("Ourense", "32"),
            ("Orense", "32"),
            ("Pontevedra", "36"),
            ("Lleida", "25"),
            ("Lérida", "25"),
            ("Alicante", "03"),
            ("Las Palmas", "35"),  # the article must not eat the name
            ("La Rioja", "26"),
        ],
    )
    def test_a_province_resolves_however_it_is_spelled(self, name, code):
        assert province_code_for_name(name) == code

    @pytest.mark.parametrize(
        "name", ["Galicia", "Asturias, Principado de Asturias", "Spain", "", "Siero"]
    )
    def test_what_is_not_a_province_resolves_to_nothing(self, name):
        assert province_code_for_name(name) is None
