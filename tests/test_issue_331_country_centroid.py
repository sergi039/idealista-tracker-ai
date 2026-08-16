"""A country is not a location for a listing (issue #331).

Every query `_build_geocoding_queries` builds ends in ", Spain". When the rest
is a title fragment -- "Finca offers for", "a farm for sale, loca" -- Google
resolves the whole string to the country and returns Spain's own point,
40.463667,-3.749220. Eight production rows sat there, and because every travel
target is measured *from* the stored coordinate, all six presets, the beaches
block and the travel component of their scores were measured from central
Spain. After the #323 hospital fix those rows read "Hospital La Paz
Peñagrande, 11 min" for plots in Asturias: confident, plausible, and wrong by
450 km of origin.

`location_type` cannot distinguish this -- a street centroid and a country are
both APPROXIMATE -- so the geocoder now surfaces the result's `types` and this
service refuses the coarse ones.

The tests commit and re-read, because the refusal record is written to the
`enrichment` JSON column, and a write there that is not flagged never reaches
the row (that was #326, one day earlier).
"""

import pytest

from app import create_app, db
from models import Property
from services.property_location_service import PropertyLocationService
from tests import setup_test_environment

SPAIN = {
    "lat": 40.463667,
    "lng": -3.749220,
    "formatted_address": "Spain",
    "types": ["country", "political"],
    "accuracy": "approximate",
}
TOWN = {
    "lat": 43.5,
    "lng": -5.65,
    "formatted_address": "Carreño, Asturias, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
}


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


def _prop(**kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", "issue_331"),
        title=kw.pop("title", "Finca offers for"),
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _service(answers):
    """`answers` maps a substring of the query to the reply for it."""
    service = PropertyLocationService()

    def fake(address):
        for needle, reply in answers.items():
            if needle.lower() in address.lower():
                return reply
        return None

    service.geocoding_service.geocode_address = fake
    return service


def _stored(prop_id):
    db.session.expire_all()
    return db.session.get(Property, prop_id)


class TestACountryIsRefused:
    def test_the_row_gets_no_coordinates(self, app):
        with app.app_context():
            prop = _prop(municipality="Finca Offers For")
            ok = _service({"": SPAIN}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is False
            stored = _stored(prop.id)
            assert stored.location_lat is None
            assert stored.location_lon is None

    def test_the_row_says_why_it_has_none(self, app):
        """An empty travel block must be explainable, not merely empty."""
        with app.app_context():
            prop = _prop(
                source_email_id="issue_331_why", municipality="Finca Offers For"
            )
            _service({"": SPAIN}).ensure_coordinates(prop)
            db.session.commit()

            record = _stored(prop.id).enrichment["geocoding"]
            assert record["refused"] == "result_too_coarse"
            assert record["formatted_address"] == "Spain"
            assert "country" in record["result_types"]
            assert record["accuracy"] == "unknown"

    def test_a_region_and_a_province_are_refused_too(self, app):
        with app.app_context():
            for level in ("administrative_area_level_1", "administrative_area_level_2"):
                prop = _prop(
                    source_email_id=f"issue_331_{level}", municipality="Asturias"
                )
                reply = dict(
                    SPAIN, types=[level, "political"], formatted_address="Asturias"
                )
                assert _service({"": reply}).ensure_coordinates(prop) is False


class TestItDoesNotRefuseTooMuch:
    def test_a_town_is_still_accepted(self, app):
        with app.app_context():
            prop = _prop(source_email_id="issue_331_town", municipality="Carreño")
            assert _service({"": TOWN}).ensure_coordinates(prop) is True
            db.session.commit()

            stored = _stored(prop.id)
            assert float(stored.location_lat) == pytest.approx(43.5)
            assert "refused" not in stored.enrichment["geocoding"]

    def test_a_useless_title_falls_through_to_a_usable_municipality(self, app):
        """The reason a refusal continues the loop instead of ending it.

        The title query resolves to the country; the municipality query behind
        it resolves to a real town. Ending the loop at the first refusal would
        throw that away and leave the row unlocatable for no reason.
        """
        with app.app_context():
            prop = _prop(
                source_email_id="issue_331_fallthrough",
                title="Land in calle As-110, n/a, Carreño 49,000 €",
                municipality="Carreño",
            )
            service = _service({"As-110": SPAIN, "Carreño, Spain": TOWN})

            assert service.ensure_coordinates(prop) is True
            db.session.commit()

            stored = _stored(prop.id)
            assert float(stored.location_lat) == pytest.approx(43.5)
            assert stored.enrichment["geocoding"]["formatted_address"] == (
                "Carreño, Asturias, Spain"
            )

    def test_a_reply_with_no_types_is_not_refused(self, app):
        """Absence of the field is not evidence of coarseness.

        The Nominatim fallback carries no `types` at all. Treating that as
        "too coarse" would silently drop every fallback geocode.
        """
        with app.app_context():
            prop = _prop(source_email_id="issue_331_notypes", municipality="Carreño")
            reply = {k: v for k, v in TOWN.items() if k != "types"}
            assert _service({"": reply}).ensure_coordinates(prop) is True
