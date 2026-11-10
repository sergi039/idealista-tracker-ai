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

# What the form submits when housing / house / Needs verification are chosen:
# the two hidden markers, the sort select, the filters, and a page number the
# reader happened to be on. `search=` empty is the search box left blank.
CHOSEN = (
    "/properties?mode=recommendation&view_type=list&category=housing"
    "&subtype=house&recommendation_scope=review&sort=created_at&order=desc&search=&page=1"
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
    assert recalled["recommendation_scope"] == "review"
    assert recalled["view_type"] == "list"
    assert "page" not in recalled, "page=1 is the first page: absence, never stored"
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


# --- The page is remembered too (owner, 2026-11-10, issue #630) -------------

PAGED = (
    "/properties?mode=combined&view_type=list&per_page=10&sort=created_at&order=desc"
)


@pytest.fixture
def many(app):
    """Twenty-five live houses: three pages at ten per page."""
    with app.app_context():
        profile = SearchProfile(
            name="Paged",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        for index in range(25):
            db.session.add(
                Property(
                    source_email_id=f"paged_{index}",
                    title=f"Paged {index:02d}",
                    municipality="Bergondo",
                    property_category="housing",
                    property_subtype="house",
                    price=200000 + index,
                    url=f"https://www.idealista.com/inmueble/80{index:03d}/",
                    search_profile_id=profile.id,
                    listing_status="active",
                )
            )
        db.session.commit()


def _titles(body: str) -> set[str]:
    return set(re.findall(r"Paged \d\d", body))


def test_a_pagination_click_is_remembered_and_recalled(client, many):
    assert client.get(f"{PAGED}&page=2").status_code == 200

    response = client.get("/properties")
    assert response.status_code == 302
    assert _query(response.headers["Location"])["page"] == "2"
    landed = client.get(response.headers["Location"])
    assert landed.status_code == 200
    assert len(_titles(landed.get_data(as_text=True))) == 10


def test_page_one_is_never_stored(client, many):
    client.get(f"{PAGED}&page=2")
    client.get(f"{PAGED}&page=1")
    recalled = _query(client.get("/properties").headers["Location"])
    assert "page" not in recalled


def test_a_filter_change_lands_on_page_one(client, many):
    """The form has no page field and the sort header names page 1, so the
    request that follows a change never carries the old page."""
    body = client.get(f"{PAGED}&page=3").get_data(as_text=True)
    form = re.search(r'<form[^>]*id="filter-form".*?</form>', body, re.S).group(0)
    assert 'name="page"' not in form
    sort_links = re.findall(r'href="([^"]*)"\s+class="lands-th-link', body)
    assert sort_links
    for href in sort_links:
        assert _query(unescape(href)).get("page") == "1", href

    changed = client.get(f"{PAGED}&sort=price&order=asc")
    assert changed.status_code == 200
    recalled = _query(client.get("/properties").headers["Location"])
    assert "page" not in recalled
    assert recalled["sort"] == "price"


def test_clear_forgets_the_page(client, many):
    client.get(f"{PAGED}&page=3")
    assert client.get("/properties?remember=forget").status_code == 302
    bare = client.get("/properties")
    assert bare.status_code == 200


def test_a_remembered_page_past_the_end_shows_the_last_page(client, many):
    response = client.get(f"{PAGED}&page=99")
    assert response.status_code == 200
    assert len(_titles(response.get_data(as_text=True))) == 5

    with client.session_transaction() as session:
        session[listing_filter_memory.SESSION_KEY] = PAGED.split("?")[1] + "&page=99"
    followed = client.get(client.get("/properties").headers["Location"])
    assert followed.status_code == 200
    assert len(_titles(followed.get_data(as_text=True))) == 5


def test_a_hand_typed_junk_page_is_not_stored(client, many):
    for junk in ("0", "-3", "abc", "1"):
        client.get(f"{PAGED}&page={junk}")
        recalled = _query(client.get("/properties").headers["Location"])
        assert "page" not in recalled, junk


@pytest.mark.parametrize("view", ["list", "cards"])
def test_every_row_carries_its_anchor(app, client, many, view):
    body = client.get(
        f"/properties?mode=combined&view_type={view}&per_page=10"
    ).get_data(as_text=True)
    with app.app_context():
        ids = [p.id for p in Property.query.all()]
    anchors = set(re.findall(r'id="listing-(\d+)"', body))
    assert len(anchors) == 10
    assert anchors <= {str(i) for i in ids}


def test_the_back_link_returns_to_the_row(app, client, many):
    with app.app_context():
        prop_id = Property.query.first().id
    body = client.get(f"/properties/{prop_id}").get_data(as_text=True)
    assert re.search(
        rf'href="/properties#listing-{prop_id}"[^>]*>\s*<i class="fas fa-arrow-left',
        body,
    ), "the back link must end with the row's fragment"


def test_an_absurdly_long_page_number_is_ignored_not_an_error(client, many):
    """Python refuses to convert thousands of digits to an int; the page must
    render and the junk must not be stored (#630 review)."""
    response = client.get("/properties?mode=combined&view_type=list&page=" + "9" * 5000)
    assert response.status_code == 200

    recalled = client.get("/properties")
    assert "page=" not in (recalled.headers.get("Location") or "")
