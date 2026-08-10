"""Issue #227: two controls on /properties that carried the wrong state.

* Changing "Show: N" while on a later page kept `page`, so page 6 of 25 became
  page 6 of 100 — past the end. The list came back empty under a count that
  still reported the real total: the owner asked for a bigger page and got an
  empty one.
* The "show them" link for unassigned listings was the only filter link on the
  page that omitted `sort`/`order`, so a cheapest-first list silently reverted
  to the mode default.

Both halves are pinned at the surface the owner uses: a rendered page, the
links it actually contains, and the rows a request returns.
"""

import html
import re
from urllib.parse import parse_qs, urlparse

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

ROW_ID_RE = re.compile(r"/properties/(\d+)")


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
    """30 assigned rows (so 25-per-page has a second page) and 2 unassigned."""
    profile = SearchProfile(
        name="Houses in Asturias",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()

    for index in range(30):
        db.session.add(
            Property(
                source_email_id=f"nav-{index}",
                title=f"Listing {index}",
                search_profile_id=profile.id,
                municipality="Gijón",
                price=100000 + index * 1000,
                area=200,
            )
        )
    for index in range(2):
        db.session.add(
            Property(
                source_email_id=f"nav-orphan-{index}",
                title=f"Orphan {index}",
                municipality="Gijón",
                price=50000 + index * 1000,
                area=200,
            )
        )
    db.session.commit()
    return profile


def _rows(body: str) -> list[int]:
    seen: list[int] = []
    for match in ROW_ID_RE.finditer(body):
        listing_id = int(match.group(1))
        if listing_id not in seen:
            seen.append(listing_id)
    return seen


def _anchor_href(body: str, marker: str) -> str:
    index = body.index(marker)
    start = body.rindex(
        "href=",
        0,
        index + len(marker) + 400,
    )
    quote = body[start + 5]
    end = body.index(quote, start + 6)
    return html.unescape(body[start + 6 : end])


class TestAPageBeyondTheEnd:
    def test_it_renders_the_last_page_instead_of_nothing(self, client, listings):
        """Exactly what the page-size control produced: ?page=2&per_page=100."""
        body = client.get("/properties?page=2&per_page=100").get_data(as_text=True)

        assert _rows(body), (
            "an out-of-range page rendered an empty table under a count that "
            "still reported every row"
        )
        assert "of 30 results" in body, "the count must stay the real total"

    def test_a_page_within_range_is_untouched(self, client, listings):
        first = client.get("/properties?page=1&per_page=25").get_data(as_text=True)
        second = client.get("/properties?page=2&per_page=25").get_data(as_text=True)

        assert len(_rows(first)) == 25
        assert _rows(second), "the real second page must still exist"
        assert not set(_rows(first)) & set(_rows(second))

    def test_page_zero_is_the_first_page(self, client, listings):
        body = client.get("/properties?page=0&per_page=25").get_data(as_text=True)

        assert len(_rows(body)) == 25

    def test_the_page_size_control_resets_the_page(self, client, listings):
        """The onchange handler is the fix's other half; pin its shape."""
        body = client.get("/properties?page=2").get_data(as_text=True)

        assert "updateUrlParameter(window.location.href, 'per_page', this.value)" in (
            body.replace("updateUrlParameter(updateUrlParameter", "updateUrlParameter")
        )
        assert "'page', 1)" in body, (
            "changing the page size must start at the first page of that size"
        )


class TestTheUnassignedLinkKeepsTheSort:
    @pytest.mark.parametrize(
        "sort_key,order",
        [("price", "asc"), ("area", "desc"), ("score_total", "asc")],
    )
    def test_it_carries_the_current_sort(self, client, listings, sort_key, order):
        body = client.get(
            f"/properties?profile_id=all&sort={sort_key}&order={order}&per_page=100"
        ).get_data(as_text=True)

        href = _anchor_href(body, 'id="unassigned-count-link"')
        params = parse_qs(urlparse(href).query)

        assert params.get("profile_id") == ["unassigned"]
        assert params.get("sort") == [sort_key], (
            "the only filter link on the page that dropped the sort"
        )
        assert params.get("order") == [order]

    def test_the_landing_page_really_is_sorted_that_way(self, client, listings):
        body = client.get(
            "/properties?profile_id=all&sort=price&order=asc&per_page=100"
        ).get_data(as_text=True)
        href = _anchor_href(body, 'id="unassigned-count-link"')

        landed = client.get(f"{href}&per_page=100").get_data(as_text=True)
        prices = [
            float(prop.price)
            for prop in (db.session.get(Property, row) for row in _rows(landed))
            if prop is not None and prop.price is not None
        ]

        assert prices == sorted(prices), "cheapest-first reverted to the default"
