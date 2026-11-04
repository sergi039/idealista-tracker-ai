"""Workspace regressions at the existing server/form boundary.

Browser evidence on a built image owns layout, keyboard and no-auto-submit.
These cases own the query/session, honest state and executable confirmation.
"""

import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app import create_app, db
from models import Property, SearchProfile, TasteProfile
from tests import setup_test_environment


class Tags(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.tags = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app(testing=True)
    with app.app_context():
        db.create_all()
        profile = SearchProfile(name="Galicia", is_active=True)
        db.session.add(profile)
        db.session.flush()
        for index, city in enumerate(("Arteixo", "Bergondo")):
            db.session.add(
                Property(
                    source_email_id=f"workspace:{index}",
                    title=f"Workspace house {city}",
                    municipality=city,
                    property_category="housing",
                    property_subtype="house",
                    search_profile_id=profile.id,
                    listing_status="active",
                    price=250000,
                    area=230,
                    location_lat=43.3,
                    location_lon=-8.5,
                    location_accuracy="approximate",
                )
            )
        db.session.add(
            TasteProfile(
                provider="claude",
                signals_fingerprint="a" * 64,
                source={"signals": []},
                profile={"likes": [], "dislikes": [], "dealbreakers": []},
            )
        )
        db.session.commit()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def test_remove_applied_filter_stays_removed_after_session_recall(client):
    chosen = (
        "/properties?mode=combined&view_type=list&municipality=Arteixo&category=housing"
    )
    body = client.get(chosen).get_data(as_text=True)
    remove = next(
        attrs["href"]
        for tag, attrs in Tags(body).tags
        if tag == "a"
        and attrs.get("class") == "filter-chip"
        and "Municipality" in attrs.get("aria-label", "")
    )
    params = parse_qs(urlparse(unescape(remove)).query)
    assert "municipality" not in params
    assert params["category"] == ["housing"]
    assert params["mode"] == ["combined"] and params["view_type"] == ["list"]
    landed = client.get(unescape(remove))
    assert landed.status_code == 200
    assert "Workspace house Arteixo" in landed.text
    assert "Workspace house Bergondo" in landed.text
    recalled = client.get("/properties")
    assert recalled.status_code == 302
    assert "municipality" not in parse_qs(urlparse(recalled.location).query)


def test_explicit_form_and_external_sort_keep_one_owner(client):
    tags = Tags(client.get("/properties").text).tags
    form = next(a for tag, a in tags if tag == "form" and a.get("id") == "filter-form")
    assert form["method"] == "GET" and form["data-submit"] == "explicit"
    for name in ("sort", "order"):
        fields = [a for tag, a in tags if tag == "select" and a.get("name") == name]
        assert len(fields) == 1 and fields[0]["form"] == "filter-form"
    ids = [a["id"] for _, a in tags if "id" in a]
    assert len(ids) == len(set(ids)), (
        "duplicate IDs make controls target the wrong node"
    )


def test_retrain_and_refresh_confirmation_are_complete_html_attributes(client):
    state = {"state": "dirty", "version": 1, "usable_signal_count": 2}
    with patch(
        "routes.main_routes.taste_service.recommendation_profile_state",
        return_value=state,
    ):
        body = client.get("/properties?mode=recommendation&view_type=list").text
    forms = [
        a for tag, a in Tags(body).tags if tag == "form" and a.get("method") == "POST"
    ]
    for endpoint in ("/taste/retrain", "/recommendations/refresh"):
        form = next(a for a in forms if a.get("action", "").endswith(endpoint))
        handler = form["onsubmit"]
        assert handler.startswith("return confirm(") and handler.endswith(");")
        message = json.loads(handler[len("return confirm(") : -2])
        assert isinstance(message, str) and len(message) > 20


@pytest.mark.parametrize(
    "state,version,refresh,previous",
    [
        ("current", 1, False, False),
        ("dirty", 1, True, True),
        ("pending", 1, False, True),
        ("failed", 1, True, True),
        ("none", None, True, False),
        ("pending", None, False, False),
        ("failed", None, True, False),
    ],
)
def test_profile_states_do_not_invent_previous_data_or_repeat_work(
    client, state, version, refresh, previous
):
    summary = {"state": state, "version": version, "usable_signal_count": 2}
    with (
        patch(
            "routes.main_routes.taste_service.recommendation_profile_state",
            return_value=summary,
        ),
        patch("routes.main_routes.taste_service.refresh_recommendations") as paid,
    ):
        response = client.get("/properties?mode=recommendation&view_type=list")
    assert response.status_code == 200
    assert f'data-recommendation-profile-state="{state}"' in response.text
    assert ('id="recommendation-refresh-button"' in response.text) is refresh
    assert ("Previous recommendations remain available." in response.text) is previous
    paid.assert_not_called()


def test_component_reference_is_read_only_and_uses_the_shared_shell(client):
    response = client.get("/design-system")
    assert response.status_code == 200
    assert "Component reference" in response.text
    assert "css/design-tokens.css" in response.text
    assert not [a for tag, a in Tags(response.text).tags if tag == "form"]
    assert client.post("/design-system").status_code == 405


def test_shared_design_layer_cannot_add_another_palette_or_framework():
    root = Path(__file__).resolve().parents[1]
    workspace_css = (root / "static/css/workspace.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", workspace_css)
    base = (root / "templates/base.html").read_text()
    assert base.count("bootstrap@5.3.2/dist/css/") == 1
    assert "tabler.min.css" not in base and "lucide" not in base


@pytest.mark.parametrize(
    "lang,key,value,meaning",
    [
        ("en", "sea_dist", "400", "≤ 400 m"),
        ("en", "sea_view", "likely", "confirmed or likely"),
        ("en", "build", "claimed", "claimed by seller"),
        ("en", "similar", "70", "≥ 70"),
        ("en", "category", "__none__", "Unknown"),
        ("es", "sea_view", "likely", "confirmada o probable"),
        ("es", "build", "claimed", "según el vendedor"),
    ],
)
def test_applied_conditions_disclose_units_and_the_actual_option_meaning(
    client, lang, key, value, meaning
):
    with client.session_transaction() as session:
        session["language"] = lang
    response = client.get(
        "/properties",
        query_string={"mode": "combined", "view_type": "list", key: value},
    )
    assert response.status_code == 200
    labels = re.findall(
        r'class="filter-chip"[^>]*>\s*<span>(.*?)</span>', response.text, re.S
    )
    assert any(meaning in " ".join(unescape(label).split()) for label in labels)
    assert all("__none__" not in label for label in labels)


@pytest.mark.parametrize(
    "accuracy,located,warning",
    [
        (None, True, "Location accuracy unknown"),
        ("unknown", True, "Location accuracy unknown"),
        ("street", True, "Location accuracy unknown"),
        ("approximate", True, "Approximate location"),
        ("precise", True, None),
        ("approximate", False, "Not located"),
    ],
)
def test_visible_location_preserves_unknown_accuracy(
    client, accuracy, located, warning
):
    row = Property.query.filter_by(municipality="Arteixo").one()
    row.location_accuracy = accuracy
    if not located:
        row.location_lat = row.location_lon = None
    db.session.commit()
    response = client.get(
        "/properties?mode=combined&view_type=list&search=Workspace+house+Arteixo"
    )
    assert response.status_code == 200
    warnings = re.findall(
        r'<div class="workspace-location-warning">(.*?)</div>', response.text
    )
    assert warnings == ([warning] if warning else [])


@pytest.mark.parametrize("lat,lon", [(0.0, -8.5), (43.3, 0.0)])
def test_zero_coordinate_remains_located_with_unknown_accuracy(client, lat, lon):
    row = Property.query.filter_by(municipality="Arteixo").one()
    row.location_lat, row.location_lon, row.location_accuracy = lat, lon, None
    db.session.commit()
    body = client.get(
        "/properties?mode=combined&view_type=list&search=Workspace+house+Arteixo"
    ).text
    assert (
        '<div class="workspace-location-warning">Location accuracy unknown</div>'
        in body
    )
    assert "0.0000" in body
    assert re.search(rf'href="/map\?[^\"]*focus={row.id}(?:&amp;|\")', body)


def subscription_options(body):
    map_link = re.search(r'<a\b[^>]*id="view-map-btn"[^>]*href="([^"]+)"', body)
    if not map_link:
        map_link = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*id="view-map-btn"', body)
    assert map_link, "the applied selection has no map representation link"
    state = parse_qs(urlparse(unescape(map_link.group(1))).query)
    options = {}
    for pid, label in re.findall(
        r'<label\b[^>]*for="profile-option-([^" ]+)"[^>]*>(.*?)</label>', body, re.S
    ):
        match = re.match(
            r'\s*([^<]+)\s*<span class="text-body-secondary">\((\d+)\)</span>', label
        )
        assert match, "subscription label must disclose its filtered count"
        params = dict(state, profile_id=[pid], page=["1"])
        options[unescape(match.group(1)).strip()] = (
            int(match.group(2)),
            "/properties?" + urlencode(params, doseq=True),
        )
    return options
