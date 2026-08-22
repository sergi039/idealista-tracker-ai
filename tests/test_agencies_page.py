"""The /agencies page (owner request 2026-08-22).

Contracts:
* the table is a *dated* measurement read from ``data/top_agencies.json`` and
  the page prints that date;
* rows are ranked by the idealista detached-house count, unmeasured rows last;
* a missing or unreadable file refuses the page (503) and says so -- never an
  empty table that reads as "no agencies";
* the navbar carries the tab;
* the committed data file itself is well-formed, so the page the mini serves
  cannot be a 503 by accident.
"""

import json

import pytest

from app import create_app, db
from services import agency_directory
from tests import setup_test_environment

FIXTURE = {
    "measured_at": "2026-08-22",
    "criteria": {"max_price_eur": 300000, "regions": ["Asturias", "Cantabria"]},
    "method": "fixture method note",
    "agencies": [
        {
            "name": "Small Agency",
            "region": "Cantabria",
            "base": "Selaya",
            "website": "https://small.example/",
            "idealista": {
                "detached": 7,
                "independientes": 2,
                "casas_chalets": 9,
                "url": "https://www.idealista.com/pro/small/venta-viviendas/cantabria/con-precio-hasta_300000,chalets-independientes,casas-de-pueblo/",
            },
            "fotocasa": {"count": None, "url": None, "note": "not on fotocasa"},
            "reviews": {"google_rating": None, "google_count": None},
            "description": "A small rural agency.",
        },
        {
            "name": "Big Agency",
            "region": "Asturias",
            "base": "Oviedo",
            "founded": "2004",
            "website": "https://big.example/",
            "phone": "985 000 000",
            "idealista": {
                "detached": 92,
                "independientes": 91,
                "casas_chalets": 95,
                "url": "https://www.idealista.com/pro/big/venta-viviendas/asturias/con-precio-hasta_300000,chalets-independientes,casas-de-pueblo/",
            },
            "fotocasa": {
                "count": 88,
                "url": "https://www.fotocasa.es/es/inmobiliaria-big/comprar/viviendas/asturias-provincia/todas-las-zonas/l?clientId=1&maxPrice=300000&propertySubtypeIds=3;9",
            },
            "reviews": {
                "google_rating": 4.6,
                "google_count": 120,
                "source": "https://reviews.example/big",
            },
            "description": "A big agency.",
            "other_links": [
                {"label": "habitaclia", "url": "https://habitaclia.example/big"}
            ],
        },
        {
            "name": "Unmeasured Agency",
            "region": "Asturias",
            "website": "https://unmeasured.example/",
            "idealista": {"detached": None},
            "description": "Not measured.",
        },
    ],
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _point_at(monkeypatch, tmp_path, payload):
    target = tmp_path / "top_agencies.json"
    if payload is not None:
        target.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(agency_directory, "TOP_AGENCIES_PATH", str(target))
    return target


def test_loader_ranks_by_detached_count_with_unmeasured_last(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, FIXTURE)
    table = agency_directory.load_top_agencies()
    assert table["measured_at"] == "2026-08-22"
    names = [a["name"] for a in table["agencies"]]
    assert names == ["Big Agency", "Small Agency", "Unmeasured Agency"]
    assert [a["rank"] for a in table["agencies"]] == [1, 2, 3]
    big = table["agencies"][0]
    assert big["idealista"]["detached"] == 92
    assert big["idealista"]["independientes"] == 91
    assert big["fotocasa"]["count"] == 88
    assert big["reviews"]["google_rating"] == 4.6
    assert big["other_links"] == [
        {"label": "habitaclia", "url": "https://habitaclia.example/big"}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,  # file missing
        {"agencies": []},  # no measured_at
        {"measured_at": "2026-08-22", "agencies": {"not": "a list"}},
        "not json at all",
    ],
)
def test_loader_refuses_missing_or_malformed_file(monkeypatch, tmp_path, payload):
    if payload == "not json at all":
        target = tmp_path / "top_agencies.json"
        target.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr(agency_directory, "TOP_AGENCIES_PATH", str(target))
    else:
        _point_at(monkeypatch, tmp_path, payload)
    with pytest.raises(agency_directory.AgencyDataUnavailable):
        agency_directory.load_top_agencies()


def test_page_renders_ranked_table_with_date_and_links(client, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, FIXTURE)
    response = client.get("/agencies")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-test="agencies-table"' in html
    assert "2026-08-22" in html
    # Ranked: the big agency's row comes before the small one's.
    assert html.index('data-agency="Big Agency"') < html.index(
        'data-agency="Small Agency"'
    )
    assert html.index('data-agency="Small Agency"') < html.index(
        'data-agency="Unmeasured Agency"'
    )
    # Counts link to the filtered page they were read from.
    assert (
        'href="https://www.idealista.com/pro/big/venta-viviendas/asturias/con-precio-hasta_300000,chalets-independientes,casas-de-pueblo/"'
        in html
    )
    assert "clientId=1&amp;maxPrice=300000&amp;propertySubtypeIds=3;9" in html
    assert "https://big.example/" in html
    assert "4.6" in html and "(120)" in html
    assert "fixture method note" in html
    assert 'data-test="agencies-unavailable"' not in html


def test_missing_file_refuses_with_503_and_says_so(client, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)
    response = client.get("/agencies")
    assert response.status_code == 503
    html = response.get_data(as_text=True)
    assert 'data-test="agencies-unavailable"' in html
    assert 'data-test="agencies-table"' not in html


def test_navbar_carries_the_tab(client, monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, FIXTURE)
    html = client.get("/agencies").get_data(as_text=True)
    assert 'href="/agencies"' in html
    assert ">Agencies" in html


def test_committed_data_file_is_well_formed():
    """The file the deployment serves must load, or the page ships as a 503."""
    table = agency_directory.load_top_agencies()
    assert len(table["agencies"]) >= 5
    for agency in table["agencies"]:
        assert agency["name"]
        assert agency["website"], agency["name"]
        assert agency["idealista"]["url"], agency["name"]
        assert isinstance(agency["idealista"]["detached"], int), agency["name"]
    assert table["agencies"][0]["rank"] == 1
