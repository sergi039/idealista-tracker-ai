"""Regression coverage for unclassified property CSV exports (#115)."""

import csv
import io
import re

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment


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


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def unclassified_properties(app):
    with app.app_context():
        from services.search_profile_service import SearchProfileService

        profile = SearchProfileService.get_default_profile(create=True)
        assert profile is not None

        properties = [
            Property(
                source_email_id="issue_115_null_category",
                title="Null category",
                search_profile_id=profile.id,
                property_category=None,
                property_subtype="apartment",
            ),
            Property(
                source_email_id="issue_115_empty_category",
                title="Empty category",
                search_profile_id=profile.id,
                property_category="",
                property_subtype="villa",
            ),
            Property(
                source_email_id="issue_115_null_subtype",
                title="Null subtype",
                search_profile_id=profile.id,
                property_category="housing",
                property_subtype=None,
            ),
            Property(
                source_email_id="issue_115_empty_subtype",
                title="Empty subtype",
                search_profile_id=profile.id,
                property_category="housing",
                property_subtype="",
            ),
            Property(
                source_email_id="issue_115_classified",
                title="Classified",
                search_profile_id=profile.id,
                property_category="housing",
                property_subtype="apartment",
            ),
        ]
        db.session.add_all(properties)
        db.session.commit()

        return {
            "profile_id": profile.id,
            "category_ids": {properties[0].id, properties[1].id},
            "subtype_ids": {properties[2].id, properties[3].id},
        }


def _property_ids_from_page(response):
    return {
        int(property_id)
        for property_id in re.findall(
            r'href="/properties/(\d+)"', response.get_data(as_text=True)
        )
    }


def _property_ids_from_csv(response):
    rows = csv.DictReader(io.StringIO(response.get_data(as_text=True)))
    return {int(row["ID"]) for row in rows}


@pytest.mark.parametrize(
    ("filter_name", "expected_ids_key"),
    [("category", "category_ids"), ("subtype", "subtype_ids")],
)
def test_page_and_csv_export_match_for_unclassified_filter(
    client, unclassified_properties, filter_name, expected_ids_key
):
    query = f"profile_id={unclassified_properties['profile_id']}&{filter_name}=__none__"

    page_response = client.get(f"/properties?{query}")
    export_response = client.get(f"/properties/export.csv?{query}")

    assert page_response.status_code == 200
    assert export_response.status_code == 200

    page_ids = _property_ids_from_page(page_response)
    export_ids = _property_ids_from_csv(export_response)

    assert page_ids == unclassified_properties[expected_ids_key]
    assert export_ids == page_ids
