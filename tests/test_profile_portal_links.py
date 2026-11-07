"""Original portal searches remain reachable in every subscription state."""

from html.parser import HTMLParser

import pytest

from app import db
from services import search_management as management
from services.search_profile_service import SearchProfileService
from tests.test_recommendation_feedback import scope as feedback_scope

scope = feedback_scope


class ProfileRow(HTMLParser):
    def __init__(self, profile_id):
        super().__init__()
        self.profile_id = str(profile_id)
        self.inside = False
        self.links = []
        self.unavailable = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.inside = attrs.get("data-profile-id") == self.profile_id
        if self.inside:
            if tag == "a":
                self.links.append(attrs)
            if attrs.get("data-role") == "portal-search-unavailable":
                self.unavailable = True

    def handle_endtag(self, tag):
        if tag == "tr":
            self.inside = False


@pytest.mark.parametrize("language", ["en", "es"])
@pytest.mark.parametrize(
    "state", ["owner", "archived", "hidden", "technical", "deleted", "routed"]
)
def test_portal_link_preserves_search_for_every_subscription_state(
    scope, state, language
):
    app, profile, rows = scope
    profile.source_search_url = (
        "http://www.idealista.com/en/areas/venta-viviendas/con-precio-hasta_350000/"
        "?shape=%28%28abc%29%29&amp;utm_campaign=alert&amp;token=synthetic"
    )
    if state == "routed":
        SearchProfileService.route_profile(profile.id, rows[5].search_profile_id)
    elif state in {"technical", "deleted"}:
        management.set_state(profile, state)
        profile.is_hidden = True
    elif state == "archived":
        profile.is_active = False
    elif state == "hidden":
        profile.is_hidden = True
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["language"] = language
    paths = ["/profiles?view=technical"]
    if management.owner_search(profile):
        paths.append("/profiles")
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200
        row = ProfileRow(profile.id)
        row.feed(response.text)
        links = [
            link for link in row.links if link.get("data-role") == "portal-search-link"
        ]
        assert len(links) == 1
        assert links[0]["href"] == (
            "https://idealista.com/areas/venta-viviendas/con-precio-hasta_350000"
            "?shape=%28%28abc%29%29"
        )
        assert links[0]["target"] == "_blank"
        assert {"noopener", "noreferrer"} <= set(links[0]["rel"].split())
        assert not row.unavailable


@pytest.mark.parametrize(
    "url",
    [
        None,
        "javascript:alert(1)",
        "https://idealista.com.evil.example/areas/venta-viviendas/?shape=abc",
        "https://example.com/areas/venta-viviendas/?shape=abc",
        "https://www.idealista.com/areas/venta-viviendas/",
    ],
)
def test_portal_link_is_unavailable_without_a_recognized_search(scope, url):
    app, profile, _ = scope
    profile.source_search_url = url
    db.session.commit()
    response = app.test_client().get("/profiles?view=technical")
    assert response.status_code == 200
    row = ProfileRow(profile.id)
    row.feed(response.text)
    assert not any(link.get("data-role") == "portal-search-link" for link in row.links)
    assert row.unavailable
