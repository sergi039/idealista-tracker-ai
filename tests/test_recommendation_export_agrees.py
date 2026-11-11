"""Export the recommendation selection the owner actually sees.

Before the fix, the real Export link lost recommendation mode: CSV restored
hard-excluded rows and treated recommendation sorting as creation-date sorting.
Only the external profile-model answer is stubbed; profile publication,
descriptors, hard rules, ranking, page rendering and CSV all run normally.
"""

import csv
import io
import json
import re
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import taste_recommendation, taste_service
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
                            "Нравится вид на море. Никогда не хочу участок, а не дом. За 300."
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
                "location_lat": 43.2,
                "location_lon": -8.9,
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
    expected = [rows["matching"]]
    assert page_ids == expected
    assert csv_ids == expected


def test_recommendation_mode_keeps_exclusions_when_sorted_by_price(selection):
    client, rows = selection
    page_ids, csv_ids = _page_and_export(
        client, mode="recommendation", sort="price", order="asc"
    )
    expected = [rows["matching"]]
    assert page_ids == expected
    assert csv_ids == expected


def test_recommendation_sort_without_mode_keeps_the_same_selection(selection):
    client, rows = selection
    page_ids, csv_ids = _page_and_export(client, sort="recommendation")
    expected = [rows["matching"]]
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


def _href(body, element_id):
    return unescape(re.search(rf'id="{element_id}"[^>]*href="([^"]+)"', body)[1])


@pytest.mark.parametrize(
    ("ranking", "expected", "tab"),
    [
        ("mode=recommendation&sort=price", "matching", "mode-recommendation-btn"),
        ("sort=recommendation", "matching", "mode-recommendation-btn"),
        (
            "mode=recommendation&recommendation_scope=review&sort=price",
            "unknown",
            "mode-recommendation-review-btn",
        ),
    ],
)
def test_recommendation_map_keeps_the_collection_and_returns_to_it(
    selection, ranking, expected, tab
):
    client, rows = selection
    start = f"/properties?profile_id=all&municipality=Laxe&view_type=list&order=asc&{ranking}"
    page = client.get(start)
    map_page = client.get(_href(page.text, "view-map-btn"))
    markers = json.loads(re.search(r"const markers = (\[.*?\]);", map_page.text)[1])
    assert [marker["id"] for marker in markers] == [rows[expected]]
    returned = client.get(_href(map_page.text, "map-list-view-link"))
    parsed = _SelectionPage()
    parsed.feed(returned.text)
    assert parsed.property_ids == [rows[expected]]
    assert 'aria-current="true"' in re.search(rf'<a id="{tab}"[^>]+>', returned.text)[0]


@pytest.mark.parametrize("dirty", [False, True], ids=["current", "dirty-profile"])
def test_a_similar_row_with_unknown_facts_is_on_every_recommended_surface(
    selection, dirty
):
    client, rows = selection
    reference = db.session.get(Property, rows["reference"])
    close = Property(
        source_email_id="recommendation-export:close",
        title="close to the favorite",
        search_profile_id=reference.search_profile_id,
        property_category="residential",
        property_subtype="house",
        area_type="built",
        area=178,
        price=395000,
        municipality="Laxe",
        location_lat=43.21,
        location_lon=-8.91,
    )
    db.session.add(close)
    if dirty:
        reference.owner_verdict_reason += " Хочу тишину."
    db.session.commit()
    expected = {rows["matching"], close.id}

    page_ids, csv_ids = _page_and_export(
        client, mode="recommendation", sort="price", order="asc"
    )
    assert set(page_ids) == set(csv_ids) == expected

    page = client.get(
        "/properties?profile_id=all&municipality=Laxe&view_type=list"
        "&mode=recommendation&sort=price&order=asc"
    )
    assert "Earlier recommendation" not in page.text
    map_page = client.get(_href(page.text, "view-map-btn"))
    markers = json.loads(re.search(r"const markers = (\[.*?\]);", map_page.text)[1])
    assert {marker["id"] for marker in markers} == expected


def test_review_tab_exports_incomplete_listings_and_discloses_unapplied_rules(
    selection,
):
    client, rows = selection
    body = client.get("/properties?mode=recommendation&municipality=Laxe").text
    assert 'id="recommendation-unapplied-preferences"' in body
    assert "За 300" in body
    assert "numeric budget predicate is not explicit" in body
    assert f"/properties/{rows['reference']}" in body
    review = _href(body, "mode-recommendation-review-btn")
    assert "recommendation_scope=review" in review and "municipality=Laxe" in review
    page_ids, csv_ids = _page_and_export(
        client, mode="recommendation", recommendation_scope="review"
    )
    assert page_ids == csv_ids == [rows["unknown"]]


def test_strict_empty_state_links_to_unmeasured_and_counts_its_clear_target(
    selection, monkeypatch
):
    client, rows = selection
    # Similarity would keep a recommendation after the filters are cleared;
    # this is the case where none remains and the link falls back to the catalog.
    monkeypatch.setattr(taste_recommendation, "RECOMMENDED_MIN_SIMILARITY", 101.0)
    reference = db.session.get(Property, rows["reference"])
    reference.search_profile.criteria = {"min_house_m2": 150, "min_plot_m2": 700}
    db.session.commit()
    page = client.get(
        "/properties?profile_id=all&mode=recommendation&view_type=list"
        "&municipality=Laxe&criteria=pass"
    )
    assert "0 properties found" in page.text
    explanation = re.search(
        r'<p[^>]*id="empty-state-strict-criteria".*?</p>', page.text, re.S
    )[0]
    href = unescape(re.search(r'href="([^"]+)"', explanation)[1])
    unmeasured = _SelectionPage()
    unmeasured.feed(client.get(href).text)
    assert set(unmeasured.property_ids) == {rows["matching"], rows["unknown"]}
    assert "Your subscription selection holds 5 listings." in page.text
    assert (
        'hx-post="/api/ingest/email/run"'
        not in page.text.split('id="empty-state-strict-criteria"', 1)[1]
    )
    assert (
        "5 properties found"
        in client.get(_href(page.text, "empty-state-clear-link")).text
    )


def test_recommendations_group_portal_aliases(selection):
    client, rows = selection
    reference = db.session.get(Property, rows["unknown"])
    reference.url = "https://www.idealista.com/inmueble/93478124/"
    reference.taste_score = 95
    alias = Property(
        source_email_id="recommendation-export:alias",
        title="Chalet en venta en calle Dp",
        url="https://www.yaencontre.com/venta/casa/inmueble-45594-93478124",
        search_profile_id=reference.search_profile_id,
        property_category=reference.property_category,
        property_subtype="house",
        area_type="built",
        area=reference.area,
        price=reference.price,
        municipality=reference.municipality,
        location_lat=reference.location_lat,
        location_lon=reference.location_lon,
        taste_score=50,
    )
    seaia = Property(
        source_email_id="recommendation-export:seaia",
        title="Chalet en venta en calle Lugar de Seaia",
        url="https://www.yaencontre.com/venta/casa/inmueble-81643-106962112",
        search_profile=SearchProfile(name="Yaencontre", is_active=True),
        property_category=reference.property_category,
        property_subtype="house",
        area_type="built",
        area=585,
        price=reference.price + 5000,
        municipality=reference.municipality,
        location_lat=reference.location_lat,
        location_lon=reference.location_lon,
        attributes={"bedrooms": 6, "bathrooms": 3},
        taste_score=68,
    )
    db.session.add_all([alias, seaia])
    db.session.commit()
    alias_id = alias.id

    params = {
        "mode": "recommendation",
        "recommendation_scope": "review",
        "sort": "price",
    }
    page_ids, csv_ids = _page_and_export(client, **params)
    assert reference.id in page_ids
    assert {alias_id, seaia.id}.isdisjoint(page_ids)
    assert csv_ids == page_ids
    card_ids, csv_ids = _page_and_export(client, **params, view_type="cards")
    assert card_ids == csv_ids == page_ids

    start = (
        "/properties?profile_id=all&municipality=Laxe&mode=recommendation"
        "&recommendation_scope=review&sort=price"
    )
    page = client.get(start)
    map_href = _href(page.text, "view-map-btn")
    map_page = client.get(map_href)
    markers = json.loads(re.search(r"const markers = (\[.*?\]);", map_page.text)[1])
    map_ids = set(page_ids)
    assert {marker["id"] for marker in markers} == map_ids
    focused_map = client.get(f"{map_href}&focus={alias_id}")
    markers = json.loads(re.search(r"const markers = (\[.*?\]);", focused_map.text)[1])
    assert {marker["id"] for marker in markers} == (map_ids - {reference.id}) | {
        alias_id
    }

    # The alias remains accessible from its own portal filter and in the catalog.
    page_ids, csv_ids = _page_and_export(client, **params, source="yaencontre")
    assert page_ids == csv_ids == [alias_id]
    # The catalog folds the three publications of one home into its
    # reference (#633: the five seed pairs are `confirmed`), page and export
    # agreeing; `duplicates=show` is the way back to all three.
    page_ids, csv_ids = _page_and_export(client, mode="combined")
    assert reference.id in page_ids
    assert {alias_id, seaia.id}.isdisjoint(page_ids)
    assert set(page_ids) == set(csv_ids)
    page_ids, csv_ids = _page_and_export(client, mode="combined", duplicates="show")
    assert {reference.id, alias_id, seaia.id} <= set(page_ids) == set(csv_ids)

    # A canonical advert that needs verification cannot hide a confirmed alias.
    alias.enrichment = {"environment": {"sea_view": "yes"}}
    db.session.commit()
    page_ids, csv_ids = _page_and_export(client, mode="recommendation")
    assert set(page_ids) == set(csv_ids) == {rows["matching"], alias_id}
    alias.enrichment = {}

    # Even a matching numeric ID and figures need independent corroboration.
    reference.url = "https://www.idealista.com/inmueble/99999999/"
    alias.url = "https://www.yaencontre.com/venta/casa/inmueble-45594-99999999"
    db.session.commit()
    page_ids, csv_ids = _page_and_export(client, **params)
    assert {reference.id, alias_id} <= set(page_ids) == set(csv_ids)
