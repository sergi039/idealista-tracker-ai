"""The sea-distance filter keeps only rows *measured* within the cut.

The owner's ask: one link that collects, across every subscription, whatever
sits within a ten-minute walk of the sea. The distance the app holds is
`enrichment["sea"]` -- straight-line metres to the OSM coastline
(services/sea_distance_service.py) -- so the filter cuts on that, and the
option labels say both the metres and the walk they roughly buy.

What must hold, and why each case is here:

* A precise row matches on `distance_m`; an approximate row matches on
  `origin_distance_m` -- the centroid's figure, the one every surface already
  shows captioned (#358). Excluding those rows entirely would shrink the
  answer to the handful of precise coordinates while the likeliest candidates
  (coastal villages, whose centroid IS near the sea) sit unmatched.
* A measured "no coastline within the radius", a refusal (`unavailable`) and
  a row nobody measured never match any cut: absence is not nearness (#98).
* An unknown value applies no filter *and reports no narrowing* --
  `filter_bar_active` reads object identity, so the helper must hand back the
  same query object.
* The filter has to survive the page's own links (base_args), reach the CSV
  export and the map -- the three surfaces one URL describes (#439/#445).
"""

from __future__ import annotations

import re
from html import unescape

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

SEA_BLOCKS = {
    "precise_near": {"status": "ok", "distance_m": 350.0},
    "precise_far": {"status": "ok", "distance_m": 1200.0},
    "centroid_near": {
        "status": "approximate_origin",
        "distance_m": None,
        "origin_distance_m": 600.0,
        "min_distance_m": 0.0,
        "max_distance_m": 5600.0,
    },
    "no_coastline": {"status": "no_coastline_within_radius", "distance_m": None},
    "refused": {"status": "unavailable", "distance_m": None},
    "unmeasured": None,
}


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        for key, sea in SEA_BLOCKS.items():
            db.session.add(
                Property(
                    source_email_id=f"seadist_{key}",
                    title=f"Row-{key}",
                    municipality="Castrillon",
                    property_category="land",
                    property_subtype="plot",
                    price=40000,
                    location_lat=43.5,
                    location_lon=-6.5,
                    search_profile_id=profile.id,
                    listing_status="active",
                    enrichment={"sea": sea} if sea is not None else None,
                )
            )
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _shown(body: str) -> set[str]:
    return set(re.findall(r"Row-(\w+)", body))


class TestTheCut:
    def test_800_keeps_the_measured_near_rows_only(self, client):
        body = client.get("/properties?profile_id=all&sea_dist=800").get_data(
            as_text=True
        )
        assert _shown(body) == {"precise_near", "centroid_near"}

    def test_400_drops_the_centroid_at_600(self, client):
        body = client.get("/properties?profile_id=all&sea_dist=400").get_data(
            as_text=True
        )
        assert _shown(body) == {"precise_near"}

    def test_1600_admits_the_far_precise_row(self, client):
        body = client.get("/properties?profile_id=all&sea_dist=1600").get_data(
            as_text=True
        )
        assert _shown(body) == {"precise_near", "precise_far", "centroid_near"}

    def test_absence_never_matches(self, client):
        """A refusal, a measured negative and a never-measured row stay out of
        every cut -- the #98 rule, in the filter's own terms."""
        body = client.get("/properties?profile_id=all&sea_dist=1600").get_data(
            as_text=True
        )
        assert not _shown(body) & {"no_coastline", "refused", "unmeasured"}


class TestUnknownValues:
    def _context(self, app, client, url):
        seen = []

        def record(sender, template, context, **extra):
            if template.name == "properties.html":
                seen.append(context)

        template_rendered.connect(record, app)
        try:
            assert client.get(url).status_code == 200
        finally:
            template_rendered.disconnect(record, app)
        assert seen, "properties.html did not render"
        return seen[-1]

    def test_an_unknown_value_filters_nothing_and_reports_no_narrowing(
        self, app, client
    ):
        context = self._context(
            app, client, "/properties?profile_id=all&sea_dist=banana"
        )
        assert context["pagination"].total == len(SEA_BLOCKS)
        # Identity contract: an unapplied filter must not count as a
        # narrowing, or the count line describes a cut that never happened.
        assert context["filter_bar_scope_total"] is None

    def test_a_real_value_is_reported_as_a_narrowing(self, app, client):
        context = self._context(app, client, "/properties?profile_id=all&sea_dist=800")
        assert context["pagination"].total == 2
        assert context["filter_bar_scope_total"] == len(SEA_BLOCKS)


class TestTheFilterTravels:
    def test_the_sort_headers_carry_it(self, client):
        """base_args, not the named example: a link that drops the filter
        widens the set in silence (#435's shape)."""
        body = client.get("/properties?profile_id=all&sea_dist=800").get_data(
            as_text=True
        )
        sort_link = re.search(r'href="(/properties\?[^"]*sort=price[^"]*)"', body)
        assert sort_link, "the page drew no price sort header"
        assert "sea_dist=800" in unescape(sort_link.group(1))

    def test_the_export_link_carries_it(self, client):
        body = client.get("/properties?profile_id=all&sea_dist=800").get_data(
            as_text=True
        )
        export = re.search(r'href="(/properties/export\.csv[^"]*)"', body)
        assert export, "the page drew no Export CSV link"
        assert "sea_dist=800" in unescape(export.group(1))

    def test_the_export_applies_it(self, client):
        resp = client.get("/properties/export.csv?profile_id=all&sea_dist=800")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert _shown(body) == {"precise_near", "centroid_near"}

    def test_the_map_applies_it(self, client):
        body = client.get("/map?profile_id=all&sea_dist=800").get_data(as_text=True)
        match = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.DOTALL)
        assert match, "could not find the marker payload on the map page"
        titles = set(re.findall(r"Row-(\w+)", unescape(match.group(1))))
        assert titles == {"precise_near", "centroid_near"}


class TestRouteFromGijon:
    """Every located row offers a one-click Google Maps route from Gijón.

    Owner request 2026-08-24, shipped with this filter because the set it is
    for is the one being driven to. The origin is a constant
    (`utils/maps_urls.GIJON_CENTER`), the destination is the row's own pin,
    and a row with no coordinate offers no route -- same contract as every
    other maps link here (`maps_directions_url` returns None over a half-built
    URL).
    """

    def test_the_list_offers_the_route_for_located_rows(self, client):
        from utils.maps_urls import GIJON_CENTER

        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        origin = f"{GIJON_CENTER[0]:.6f}%2C{GIJON_CENTER[1]:.6f}"
        routes = re.findall(r'href="(https://www\.google\.com/maps/dir/\?[^"]+)"', body)
        from_gijon = [href for href in routes if origin in href]
        # Every fixture row carries the same coordinate, so every row offers
        # the same route -- and it points at the row's pin, not at Gijón.
        assert from_gijon, "no row offered a route from Gijón"
        assert all("destination=43.5" in unescape(href) for href in from_gijon)

    def test_the_helper_refuses_half_a_route(self):
        from utils.maps_urls import maps_route_from_gijon_url

        assert maps_route_from_gijon_url(None, -6.5) is None
        assert maps_route_from_gijon_url(43.5, None) is None
        url = maps_route_from_gijon_url(43.5, -6.5)
        assert url is not None and url.startswith("https://www.google.com/maps/dir/")


class TestTheControl:
    def test_the_select_offers_the_cuts_and_keeps_its_selection(self, client):
        body = client.get("/properties?profile_id=all&sea_dist=800").get_data(
            as_text=True
        )
        select = re.search(r'<select[^>]*name="sea_dist".*?</select>', body, re.DOTALL)
        assert select, "the filter bar has no sea-distance select"
        assert 'value="800" selected' in select.group(0)
        for value in ("400", "1600"):
            assert f'value="{value}"' in select.group(0)
