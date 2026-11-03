"""Export the recommendation selection the owner actually sees.

Before the fix, the real Export link lost recommendation mode: CSV restored
hard-excluded rows and treated recommendation sorting as creation-date sorting.
Only the external profile-model answer is stubbed; profile publication,
descriptors, hard rules, ranking, page rendering and CSV all run normally.
"""

import csv
import io
import json
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlencode
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import taste_service
from tests import setup_test_environment


@pytest.fixture
def selection():
    setup_test_environment()
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        profile = SearchProfile(name="Export scope", is_active=True)
        db.session.add(profile)
        db.session.flush()
        rows = {}
        for index, (name, price, extra) in enumerate(
            [
                (
                    "reference",
                    400000,
                    {
                        "is_favorite": True,
                        "owner_verdict": "interested",
                        "owner_verdict_reason": (
                            "Нравится вид на море. Никогда не хочу участок, а не дом."
                        ),
                    },
                ),
                (
                    "matching",
                    300000,
                    {"enrichment": {"environment": {"sea_view": "yes"}}},
                ),
                ("unknown", 200000, {}),
                ("excluded", 50000, {"property_category": "land"}),
                ("outside_scope", 100000, {"municipality": "Navia"}),
            ]
        ):
            values = {
                "source_email_id": f"recommendation-export:{name}",
                "title": name,
                "search_profile_id": profile.id,
                "property_category": "residential",
                "property_subtype": "house",
                "area_type": "built",
                "area": 180,
                "price": price,
                "municipality": "Laxe",
                "created_at": datetime(2020 + index, 1, 1),
            }
            values.update(extra)
            rows[name] = Property(**values)
        db.session.add_all(rows.values())
        db.session.commit()
        model_answer = {
            "likes": [
                {
                    "trait": "sea view",
                    "weight": 1,
                    "evidence": "Нравится вид на море.",
                    "evidence_property_ids": [rows["reference"].id],
                }
            ],
            "dislikes": [],
            "dealbreakers": [],
            "summary_ru": "Дом с видом на море.",
        }
        with patch(
            "services.subscription_transport.complete",
            return_value={"text": json.dumps(model_answer), "model": "fixture"},
        ):
            assert taste_service.build_profile()["status"] == "ok"
        yield app.test_client(), {name: row.id for name, row in rows.items()}
        db.session.remove()
        db.drop_all()


class _SelectionPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.property_ids = []
        self.export_href = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (
            tag == "button"
            and "property-favorite-btn" in attributes.get("class", "").split()
        ):
            self.property_ids.append(int(attributes["data-property-id"]))
        if tag == "a" and attributes.get("href", "").startswith(
            "/properties/export.csv"
        ):
            self.export_href = attributes["href"]


def _page_and_export(client, **query):
    params = {"profile_id": "all", "municipality": "Laxe", **query}
    response = client.get("/properties?" + urlencode(params))
    assert response.status_code == 200
    page = _SelectionPage()
    page.feed(response.get_data(as_text=True))
    assert page.export_href is not None
    response = client.get(page.export_href)
    assert response.status_code == 200
    csv_ids = [
        int(row["ID"])
        for row in csv.DictReader(io.StringIO(response.get_data(as_text=True)))
    ]
    return page.property_ids, csv_ids


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_recommendation_export_keeps_ranked_ids_and_hard_exclusions(selection, order):
    client, rows = selection
    page_ids, csv_ids = _page_and_export(
        client, mode="recommendation", sort="recommendation", order=order
    )
    expected = [rows[name] for name in ("matching", "unknown", "reference")]
    if order == "asc":
        expected.reverse()
    assert page_ids == expected
    assert csv_ids == expected


def test_recommendation_mode_keeps_exclusions_when_sorted_by_price(selection):
    client, rows = selection
    page_ids, csv_ids = _page_and_export(
        client, mode="recommendation", sort="price", order="asc"
    )
    expected = [rows[name] for name in ("unknown", "matching", "reference")]
    assert page_ids == expected
    assert csv_ids == expected


def test_recommendation_sort_without_mode_keeps_the_same_selection(selection):
    client, rows = selection
    page_ids, csv_ids = _page_and_export(client, sort="recommendation")
    expected = [rows[name] for name in ("matching", "unknown", "reference")]
    assert page_ids == expected
    assert csv_ids == expected


@pytest.mark.parametrize("query", [{}, {"mode": "combined", "sort": "price"}])
def test_ordinary_export_keeps_hard_conflicts_and_existing_order(selection, query):
    client, rows = selection
    page_ids, csv_ids = _page_and_export(client, **query)
    names = (
        ("reference", "matching", "unknown", "excluded")
        if query
        else ("excluded", "unknown", "matching", "reference")
    )
    expected = [rows[name] for name in names]
    assert page_ids == expected
    assert csv_ids == expected
