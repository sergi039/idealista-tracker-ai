"""The filters chosen on `/properties` survive leaving the page and coming back.

Owner, 2026-09-11: the filters -- housing, house, Recommendations, any other --
must be remembered, not reset on every navigation. Every road back onto the
page without a query string (the navbar, "back to properties" on a listing,
the redirects after the page's own POSTs) used to open it on its defaults.

`utils/listing_filter_memory.py` keeps the last state the page's own form or
links produced in the session cookie, and a bare `/properties` redirects to it.
These tests follow the redirect and read the result count on the far side, so
a memory that is stored but never recalled, or recalled to the wrong state,
goes red; asserting that the query string appears in a header would not.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qsl, urlparse

import pytest

from app import create_app, db
from models import Property, SearchProfile
from routes.main_routes import _listing_reveal_link
from tests import setup_test_environment
from utils import listing_filter_memory

HOUSES = 3
PLOTS = 2

# What the form submits when housing / house / Recommendations are chosen:
# the two hidden markers, the sort select, the filters, and a page number the
# reader happened to be on. `search=` empty is the search box left blank.
CHOSEN = (
    "/properties?mode=recommendation&view_type=list&category=housing"
    "&subtype=house&sort=created_at&order=desc&search=&page=1"
)


def _count(body: str) -> int:
    match = re.search(r"<strong>(\d+) properties found</strong>", body)
    assert match, "the page printed no result count -- did it render at all?"
    return int(match.group(1))


def _query(location: str) -> dict[str, str]:
    parsed = urlparse(location)
    assert parsed.path == "/properties", location
    return dict(parse_qsl(parsed.query))


def _clear_button(body: str) -> str:
    match = re.search(r'href="([^"]*)"[^>]*title="Clear every filter"', body)
    assert match, "the page drew no Clear button"
    return unescape(match.group(1))


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
def listings(app):
    """Three houses and two plots on one subscription, all live."""
    with app.app_context():
        profile = SearchProfile(
            name="Galicia costa",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        for index in range(HOUSES + PLOTS):
            house = index < HOUSES
            db.session.add(
                Property(
                    source_email_id=f"memory_{index}",
                    title=f"Chalet {index}" if house else f"Plot {index}",
                    municipality="Bergondo",
                    property_category="housing" if house else "land",
                    property_subtype="house" if house else "plot",
                    price=300000 + index,
                    url=f"https://www.idealista.com/inmueble/70{index:03d}/",
                    search_profile_id=profile.id,
                    listing_status="active",
                )
            )
        db.session.commit()
        return profile.id


def _choose(client) -> int:
    """Apply the form once, as the browser would, and return its count."""
    response = client.get(CHOSEN)
    assert response.status_code == 200
    count = _count(response.get_data(as_text=True))
    assert count == HOUSES
    return count


def test_a_bare_page_returns_to_the_last_state_chosen_on_it(client, listings):
    _choose(client)

    response = client.get("/properties")
    assert response.status_code == 302, "a bare page after a choice must recall it"
    recalled = _query(response.headers["Location"])
    assert recalled["category"] == "housing"
    assert recalled["subtype"] == "house"
    assert recalled["mode"] == "recommendation"
    assert recalled["view_type"] == "list"
    assert "page" not in recalled, "a page number is where the reader was, not a filter"
    assert "search" not in recalled, "an empty value is not a filter"

    landed = client.get(response.headers["Location"])
    assert landed.status_code == 200
    assert _count(landed.get_data(as_text=True)) == HOUSES


def test_a_fresh_browser_gets_the_defaults(client, listings):
    """The deploy's render check curls the bare page without a cookie and
    demands a 200; a fresh browser is the same request."""
    response = client.get("/properties")
    assert response.status_code == 200
    assert _count(response.get_data(as_text=True)) == HOUSES + PLOTS


def test_the_clear_button_forgets(client, listings):
    _choose(client)
    page = client.get("/properties").headers["Location"]
    body = client.get(page).get_data(as_text=True)

    clear = _clear_button(body)
    assert _query(clear) == {"remember": "forget"}, clear

    forgotten = client.get(clear)
    assert forgotten.status_code == 302
    assert urlparse(forgotten.headers["Location"]).path == "/properties"
    assert not urlparse(forgotten.headers["Location"]).query

    bare = client.get("/properties")
    assert bare.status_code == 200, "after Clear the bare page is the defaults"
    assert _count(bare.get_data(as_text=True)) == HOUSES + PLOTS


def test_a_cross_page_link_is_a_visit_not_a_choice(client, listings):
    """A link from `/profiles` or `/map` carries neither marker: it is shown,
    and the bare page still returns to what was chosen on the page itself."""
    _choose(client)

    visit = client.get("/properties?category=land")
    assert visit.status_code == 200
    assert _count(visit.get_data(as_text=True)) == PLOTS

    recalled = _query(client.get("/properties").headers["Location"])
    assert recalled["category"] == "housing"


def test_the_reveal_link_is_shown_and_not_kept(app, client, listings):
    _choose(client)

    with app.app_context():
        plot = Property.query.filter_by(property_category="land").first()
        with app.test_request_context(CHOSEN):
            reveal = _listing_reveal_link(plot, plot.title)
    assert _query(reveal)["remember"] == "off", reveal

    shown = client.get(reveal)
    assert shown.status_code == 200
    assert _count(shown.get_data(as_text=True)) == 1

    recalled = _query(client.get("/properties").headers["Location"])
    assert recalled["category"] == "housing"
    assert "remember" not in recalled


def test_the_memory_outlives_the_browser_window(client, listings):
    response = client.get(CHOSEN)
    cookies = "\n".join(response.headers.getlist("Set-Cookie"))
    assert "Expires=" in cookies or "Max-Age=" in cookies, cookies


def test_a_memory_that_cannot_be_followed_is_dropped(client, listings):
    """Not a string, too long, or without the markers: the bare page renders
    rather than redirecting into a loop, and the memory is gone."""
    for stored in (
        "x" * (listing_filter_memory.MAX_STORED_CHARS + 1),
        ["a"],
        "category=housing",
    ):
        with client.session_transaction() as session:
            session[listing_filter_memory.SESSION_KEY] = stored
        response = client.get("/properties")
        assert response.status_code == 200, repr(stored)[:40]
        with client.session_transaction() as session:
            assert listing_filter_memory.SESSION_KEY not in session


def test_an_oversized_choice_is_forgotten_not_truncated(client, listings):
    _choose(client)
    long_search = "x" * listing_filter_memory.MAX_STORED_CHARS
    response = client.get(
        f"/properties?mode=combined&view_type=list&search={long_search}"
    )
    assert response.status_code == 200
    bare = client.get("/properties")
    assert bare.status_code == 200, "a state that does not fit leaves no memory"
