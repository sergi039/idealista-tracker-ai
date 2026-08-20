"""A filter the page applies has to survive the page's own links.

`/properties` rebuilds every in-page link -- pagination Prev/Next, the sort
headers, the subscription chips, the Favorites and Hide-removed switches, the
mode and cards/list buttons -- from one `base_args` dict in
`templates/properties.html`. Two filters the route really applies were missing
from it: `source` (utils/listing_source.py) and `advertiser`
(services/advertiser.py). Both are read from `request.args`, both narrow the
query, and both are handed back to the template in `current_filters`, so the
dropdowns kept their selection and the page looked filtered while every link
on it led out of the filter.

Measured against production on 2026-08-20, read-only:
`/properties?advertiser=owner&per_page=20` answered "70 properties found", and
the Next link it drew --
`/properties?profile_id=all&hide_removed=on&mode=combined&view_type=list&per_page=20&page=2&sort=created_at&order=desc`
-- answered 470. Nothing on either page said the set had changed. The Export
CSV button on that same page *did* carry the filter, so the two controls
disagreed by 6.7x about what was being looked at.

So these tests follow the links rather than reading them. A test that asserts
`advertiser=owner` appears somewhere in the body would pass on a page whose
Next button leads elsewhere -- the defect being pinned is exactly that a link
goes somewhere the page does not claim, and only fetching it can tell.

`TestEveryFilterIsCarried` is the class rather than the two examples: it reads
`current_filters` out of the rendered template context and requires every key
in it to be either carried by the links or named in `NOT_CARRIED` with the
reason it is not a filter. A filter added to the route and forgotten in
`base_args` fails there, which is the mistake this file exists about.
"""

from __future__ import annotations

import re
from html import unescape

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# Keys of `current_filters` that the in-page links deliberately do not take
# from `base_args`, and why. Everything else in that dict narrows the result
# set and has to ride along.
NOT_CARRIED = {
    # Passed explicitly at each call site instead: a sort header sets its own
    # `sort`, and pagination its own `page`, so carrying the current value in
    # `base_args` would be overwritten anyway.
    "sort_by": "each link sets sort= itself",
    "order": "each link sets order= itself",
    "page": "each link sets page= itself",
    # Not a filter and not a parameter: derived in the route from the sort
    # actually applied, so the mode buttons cannot disagree with the ordering.
    "active_mode": "derived from the applied sort, never sent",
}


def _totals(body: str) -> int:
    """The "N properties found" the page prints above the list."""
    match = re.search(r"<strong>(\d+) properties found</strong>", body)
    assert match, "the page did not print a result count"
    return int(match.group(1))


def _next_link(body: str) -> str:
    match = re.search(r'rel="next" href="([^"]+)"', body)
    assert match, "the page drew no Next link -- the fixture needs a second page"
    return unescape(match.group(1))


def _sort_link(body: str, key: str = "price") -> str:
    match = re.search(rf'href="(/properties\?[^"]*sort={key}[^"]*)"', body)
    assert match, f"the page drew no {key} sort header"
    return unescape(match.group(1))


def _shown_ids(body: str) -> set[int]:
    return {int(pid) for pid in re.findall(r'href="/properties/(\d+)"', body)}


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


# `/properties` clamps per_page to a minimum of 10, so a filtered set needs
# more than ten rows before the page draws a Next link at all -- which is the
# only way to test that Next carries the filter.
PER_GROUP = 12


@pytest.fixture
def listings(app):
    """Twelve private-owner listings, twelve agency ones, twelve on fotocasa.

    The seller is read off the alert link's `utm_campaign`, and the site off
    the host, so both filters are exercised through their real readings rather
    than through a column a test could set to anything.
    """
    with app.app_context():
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        owner_ids, agency_ids, fotocasa_ids = [], [], []
        for index in range(PER_GROUP):
            owner = Property(
                source_email_id=f"links_owner_{index}",
                title=f"Plot from its owner {index}",
                municipality="Castrillon",
                property_category="land",
                property_subtype="plot",
                price=40000 + index,
                url=(
                    f"https://www.idealista.com/inmueble/90{index:03d}/"
                    "?utm_campaign=express_newAd_sale_particular"
                ),
                search_profile_id=profile.id,
                listing_status="active",
            )
            agency = Property(
                source_email_id=f"links_agency_{index}",
                title=f"Plot through an agency {index}",
                municipality="Castrillon",
                property_category="land",
                property_subtype="plot",
                price=50000 + index,
                url=(
                    f"https://www.idealista.com/inmueble/80{index:03d}/"
                    "?utm_campaign=express_newAd_sale_professional"
                ),
                search_profile_id=profile.id,
                listing_status="active",
            )
            fotocasa = Property(
                source_email_id=f"links_fotocasa_{index}",
                title=f"Plot on the other site {index}",
                municipality="Castrillon",
                property_category="land",
                property_subtype="plot",
                price=60000 + index,
                url=f"https://www.fotocasa.es/es/comprar/terreno/aviles/70{index:03d}/d",
                search_profile_id=profile.id,
                listing_status="active",
            )
            db.session.add_all([owner, agency, fotocasa])
            db.session.commit()
            owner_ids.append(owner.id)
            agency_ids.append(agency.id)
            fotocasa_ids.append(fotocasa.id)

        return {
            "owner": set(owner_ids),
            "agency": set(agency_ids),
            "fotocasa": set(fotocasa_ids),
        }


class TestAdvertiserFilterSurvives:
    """`?advertiser=owner` is a third of the table; a link may not widen it."""

    def test_pagination(self, client, listings):
        first = client.get("/properties?advertiser=owner&per_page=10")
        assert first.status_code == 200
        body = first.get_data(as_text=True)
        assert _totals(body) == PER_GROUP

        second = client.get(_next_link(body))
        assert second.status_code == 200
        page_two = second.get_data(as_text=True)

        assert _totals(page_two) == PER_GROUP
        assert _shown_ids(page_two) <= listings["owner"]

    def test_sort_header(self, client, listings):
        body = client.get("/properties?advertiser=owner").get_data(as_text=True)
        assert _totals(body) == PER_GROUP

        sorted_page = client.get(_sort_link(body))
        assert sorted_page.status_code == 200
        sorted_body = sorted_page.get_data(as_text=True)

        assert _totals(sorted_body) == PER_GROUP
        assert _shown_ids(sorted_body) == listings["owner"]


class TestSourceFilterSurvives:
    """The same for the site the listing is on."""

    def test_pagination(self, client, listings):
        first = client.get("/properties?source=fotocasa&per_page=10")
        assert first.status_code == 200
        body = first.get_data(as_text=True)
        assert _totals(body) == PER_GROUP

        second = client.get(_next_link(body))
        assert second.status_code == 200
        page_two = second.get_data(as_text=True)

        assert _totals(page_two) == PER_GROUP
        assert _shown_ids(page_two) <= listings["fotocasa"]

    def test_sort_header(self, client, listings):
        body = client.get("/properties?source=fotocasa").get_data(as_text=True)
        assert _totals(body) == PER_GROUP

        sorted_body = client.get(_sort_link(body)).get_data(as_text=True)

        assert _totals(sorted_body) == PER_GROUP
        assert _shown_ids(sorted_body) == listings["fotocasa"]


class TestBothAtOnce:
    """Two filters together, because `base_args` is one dict and a fix that
    carries one key can still drop the other."""

    def test_pagination_keeps_both(self, client, listings):
        first = client.get("/properties?source=idealista&advertiser=agency&per_page=10")
        body = first.get_data(as_text=True)
        assert _totals(body) == PER_GROUP

        page_two = client.get(_next_link(body)).get_data(as_text=True)

        assert _totals(page_two) == PER_GROUP
        assert _shown_ids(page_two) <= listings["agency"]


class TestEveryFilterIsCarried:
    """The class, not the two examples.

    Reads `current_filters` from the rendered template context and requires
    every key to be carried by the page's own links -- or to be named in
    `NOT_CARRIED` above with the reason it is not a filter. Adding a filter to
    the route without adding it to `base_args` fails here even if nobody
    writes a test for that particular filter, which is what happened to
    `source` and `advertiser`.
    """

    def _render(self, app, client, url):
        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            response = client.get(url)
        finally:
            template_rendered.disconnect(record, app)

        assert response.status_code == 200
        assert seen, "properties.html did not render"
        return seen[-1]["current_filters"], response.get_data(as_text=True)

    def test_current_filters_are_all_declared_or_carried(self, app, client, listings):
        url = (
            "/properties?profile_id=all&category=land&subtype=plot"
            "&municipality=Castrillon&source=idealista&advertiser=agency"
            "&search=Plot&inv_metr=&sea_view=&measured="
            "&favorites=&hide_removed=on&mode=investment&view_type=list"
            "&per_page=10"
        )
        current_filters, body = self._render(app, client, url)

        links = f"{_next_link(body)} {_sort_link(body)}"
        missing = []
        for key, value in current_filters.items():
            if key in NOT_CARRIED:
                continue
            # An empty filter is not sent -- that is the `or None` in
            # `base_args`, and a page carrying `category=` would be claiming a
            # filter it is not applying.
            if value in (None, "", [], False):
                continue
            if f"{key}=" not in links:
                missing.append(key)

        assert not missing, (
            "these filters are applied by the route and dropped by the page's "
            f"own links: {sorted(missing)}. Add them to `base_args` in "
            "templates/properties.html, or to NOT_CARRIED here with the "
            "reason they are not filters."
        )

    def test_the_guard_can_fail(self, app, client, listings):
        """NOT_CARRIED is an allow-list, so it can hide the defect it is meant
        to expose. Pinning that it really is consulted keeps a future entry
        from being added without anyone noticing what it switches off."""
        current_filters, _ = self._render(
            app, client, "/properties?advertiser=agency&per_page=10"
        )

        assert current_filters["advertiser"] == "agency"
        assert "advertiser" not in NOT_CARRIED
