"""A precise answer about the wrong place is still wrong (issue #348).

#331 refuses a geocoding result that is too *coarse*. This is the other
failure: a result at exactly the right scale, about somewhere else. Two
production rows in Siero carry the parish "Viella-Granda-Meres" in their
query, and Google confidently returns `25530 Vielha, Lleida, Spain` -- a
`locality`, 539 km away in Val d'Aran. The size rule passes it, and
re-geocoding cannot repair it: the query is deterministic, so a second pass
returns the same wrong locality and stamps a fresher record on it.

Both sides reduce to a two-digit province code, the one number INE codes and
Spanish postal codes share (province+municipality, province+3). Siero is
33066; 25530 is province 25.

The three things the rule must not do, all pinned below:

* it must not become a province allowlist -- the owner's archived
  subscriptions hold real listings in Alicante, and those must still geocode;
* it must not refuse a row whose own municipality cannot be resolved -- most
  of them cannot, being parish lists, email truncations (#298) or title
  fragments -- because that would strand rows whose only defect is a bad
  municipality string;
* it must not let "could not compare" read as agreement. Four states are
  recorded, and three of them mean the coordinate was accepted unchecked.
"""

import pytest

from app import create_app, db
from models import Property
from services.property_location_service import PropertyLocationService
from tests import setup_test_environment


def _components(postcode=None, locality=None):
    parts = []
    if locality:
        parts.append(
            {"long_name": locality, "short_name": locality, "types": ["locality"]}
        )
    if postcode:
        parts.append(
            {"long_name": postcode, "short_name": postcode, "types": ["postal_code"]}
        )
    return parts


# The real answer from production, province 25.
VIELHA = {
    "lat": 42.7030823,
    "lng": 0.7933967,
    "formatted_address": "25530 Vielha, Lleida, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
    "address_components": _components("25530", "Vielha"),
}
# Pola de Siero, province 33 -- what the row should have got.
SIERO = {
    "lat": 43.3925,
    "lng": -5.6606,
    "formatted_address": "33510 Pola de Siero, Asturias, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
    "address_components": _components("33510", "Pola de Siero"),
}
# Province 33, and the row's municipality is Langreo: agreement.
LANGREO = {
    "lat": 43.2995441,
    "lng": -5.7105010,
    "formatted_address": "Lada, 33934 Langreo, Asturias, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
    "address_components": _components("33934", "Langreo"),
}
# A municipality centroid carries no postal code -- nothing to compare.
VILLAVICIOSA = {
    "lat": 43.4817214,
    "lng": -5.4355748,
    "formatted_address": "Villaviciosa, Asturias, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
    "address_components": _components(locality="Villaviciosa"),
}
# Real data from an archived subscription, province 03, outside INE scope.
ALICANTE = {
    "lat": 38.401717,
    "lng": -0.4356604,
    "formatted_address": "03550 Sant Joan d'Alacant, Alicante, Spain",
    "types": ["locality", "political"],
    "accuracy": "approximate",
    "address_components": _components("03550", "Sant Joan d'Alacant"),
}
SPAIN = {
    "lat": 40.463667,
    "lng": -3.749220,
    "formatted_address": "Spain",
    "types": ["country", "political"],
    "accuracy": "approximate",
    "address_components": _components(),
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
        source_email_id=kw.pop("source_email_id", "issue_348"),
        title=kw.pop("title", "Land in Tiñana, Viella-Granda-Meres, Siero"),
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _service(answers):
    """`answers` maps a substring of the query to the reply for it, in order."""
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


class TestTheWrongProvinceIsRefused:
    def test_vielha_does_not_become_a_siero_coordinate(self, app):
        with app.app_context():
            prop = _prop(municipality="Siero")
            ok = _service({"": VIELHA}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is False
            stored = _stored(prop.id)
            assert stored.location_lat is None
            assert stored.location_lon is None

    def test_the_row_records_which_provinces_disagreed(self, app):
        with app.app_context():
            prop = _prop(source_email_id="issue_348_why", municipality="Siero")
            _service({"": VIELHA}).ensure_coordinates(prop)
            db.session.commit()

            record = _stored(prop.id).enrichment["geocoding"]
            assert record["refused"] == "result_in_wrong_province"
            assert record["row_province"] == "33"
            assert record["result_province"] == "25"
            assert record["formatted_address"] == "25530 Vielha, Lleida, Spain"

    def test_the_next_candidate_query_is_still_tried(self, app):
        """The refusal must not end the search -- that is the whole point.

        The title fragment resolves to Val d'Aran; the bare municipality
        underneath it resolves to Siero, and that is the answer the row should
        keep.
        """
        with app.app_context():
            prop = _prop(source_email_id="issue_348_next", municipality="Siero")
            ok = _service({"tiñana": VIELHA, "": SIERO}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is True
            stored = _stored(prop.id)
            assert float(stored.location_lat) == pytest.approx(43.3925)
            assert stored.enrichment["geocoding"]["municipality_check"] == "agreed"


class TestWhatMustStillBeAccepted:
    def test_the_same_province_agrees(self, app):
        with app.app_context():
            prop = _prop(source_email_id="issue_348_ok", municipality="Langreo")
            ok = _service({"": LANGREO}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is True
            assert (
                _stored(prop.id).enrichment["geocoding"]["municipality_check"]
                == "agreed"
            )

    def test_an_unresolvable_municipality_is_not_a_contradiction(self, app):
        """Most rows cannot be resolved, and none of them is a defect."""
        with app.app_context():
            prop = _prop(
                source_email_id="issue_348_junk", municipality="Finca Offers For"
            )
            ok = _service({"": SIERO}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is True
            stored = _stored(prop.id)
            assert stored.location_lat is not None
            assert (
                stored.enrichment["geocoding"]["municipality_check"] == "row_unmatched"
            )

    def test_alicante_still_geocodes(self, app):
        """Not a province allowlist. These rows are real."""
        with app.app_context():
            prop = _prop(
                source_email_id="issue_348_alicante",
                title="Flat in Centro, San Juan de Alicante",
                municipality="San Juan de Alicante",
            )
            ok = _service({"": ALICANTE}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is True
            stored = _stored(prop.id)
            assert float(stored.location_lat) == pytest.approx(38.401717)
            assert (
                stored.enrichment["geocoding"]["municipality_check"] == "row_unmatched"
            )

    def test_a_result_without_a_postcode_is_not_agreement(self, app):
        with app.app_context():
            prop = _prop(
                source_email_id="issue_348_nopost",
                title="Land in Selorio - Tornón, Villaviciosa",
                municipality="Villaviciosa",
            )
            ok = _service({"": VILLAVICIOSA}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is True
            record = _stored(prop.id).enrichment["geocoding"]
            assert record["municipality_check"] == "result_has_no_postcode"


class TestTheCoarseRuleIsUntouched:
    def test_a_country_is_still_refused_under_its_own_reason(self, app):
        """#331's refusal must not be relabelled by this ticket's plumbing."""
        with app.app_context():
            prop = _prop(source_email_id="issue_348_country", municipality="Siero")
            ok = _service({"": SPAIN}).ensure_coordinates(prop)
            db.session.commit()

            assert ok is False
            assert _stored(prop.id).enrichment["geocoding"]["refused"] == (
                "result_too_coarse"
            )
