"""`hide_removed` is one rule, read by the page and by its own CSV export.

Both routes used to decide it from a hand-written list of filter parameter
names -- "is any filter present? then this came from the filter form, so an
absent `hide_removed` means the box is unticked". There were two such lists and
they had drifted apart from each other and from the filters the routes really
apply. `/properties`' was missing `source` and `advertiser`; the export's was
missing those *and* `measured`, which the export route did not implement at all.

Measured against production on 2026-08-20, read-only:

* `/properties?measured=full` answered "72 properties found" and the Export CSV
  button on that same page returned **471** rows -- it dropped `measured`, and
  lost `hide_removed` on the way too, so it also carried the withdrawn listing
  the page had excluded.
* the page's own list was inert, and naming the two missing parameters would
  have been a regression rather than a fix: every in-page link carries `sort`
  and `order`, which were already listed, so no link diverged -- while a
  hand-typed `?advertiser=owner` would have flipped from hiding the withdrawn
  listings to showing them.

So the list is gone rather than corrected. `utils/listing_status_scope.py` asks
where the request came from instead of what it filters: the filter form emits
`mode` and `view_type` as hidden inputs, and `base_args` puts both on every link
the page draws, while a bare `/properties`, the cross-page links from
`/profiles`, `/map` and `/profiles/<id>/edit`, and a hand-typed query string
carry neither. That marker cannot go stale when a filter is added, and it fixes
the three cross-page links, which used to read as a submitted form with the box
unticked and show the withdrawn listings the bare page hides.

These tests follow the page's own links rather than reading them, for the reason
`tests/test_filters_survive_page_and_sort_links.py` gives: an assertion that
`measured=full` appears somewhere in the body passes on a page whose Export CSV
button leads elsewhere, which is the defect.
"""

from __future__ import annotations

import csv
import io
import re
from html import unescape
from urllib.parse import parse_qsl, urlparse

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

# Keys of `current_filters` the Export CSV link deliberately does not carry,
# and why. Everything else narrows the result set, so a key missing from that
# link is a filter the CSV silently drops -- which is what `measured` was.
NOT_EXPORTED = {
    "mode": "how the rows are scored on screen, not which rows they are",
    "active_mode": "derived from the applied sort, never sent",
    "view_type": "cards vs table; a CSV has neither",
    "page": "the export is the whole set, not one page of it",
    "per_page": "same -- the export is not paginated",
}

# `current_filters` names the sort under a different key from the parameter.
EXPORT_PARAM_NAMES = {"sort_by": "sort"}

LIVE = 20
GONE = 7


def _count(body: str) -> int:
    match = re.search(r"<strong>(\d+) properties found</strong>", body)
    assert match, "the page printed no result count -- did it render at all?"
    return int(match.group(1))


def _export_href(body: str) -> str:
    match = re.search(r'href="(/properties/export\.csv[^"]*)"', body)
    assert match, "the page drew no Export CSV link"
    return unescape(match.group(1))


def _hide_removed_toggle(body: str) -> str:
    """The Hide-removed switch. Favorites carries `aria-pressed` too."""
    match = re.search(
        r'href="(/properties\?[^"]*)"(?:(?!</a>).)*?Hide removed', body, re.S
    )
    assert match, "the page drew no Hide-removed switch"
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
    """Live listings and withdrawn ones, half of each fully measured.

    Every one is a private-owner advert on idealista.com, so `advertiser=owner`
    and `source=idealista` really select rather than matching nothing -- a
    default-vs-form test over an empty result set passes either way.

    `measured=full` reads `scoring.coverage.share` (#379), so the rows carry a
    real share rather than a column a test could set to anything.
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

        for index in range(LIVE + GONE):
            withdrawn = index >= LIVE
            db.session.add(
                Property(
                    source_email_id=f"scope_{index}",
                    title=f"{'Withdrawn' if withdrawn else 'Live'} plot {index}",
                    municipality="Castrillon",
                    property_category="land",
                    property_subtype="plot",
                    price=40000 + index,
                    # The seller is read off the alert link's `utm_campaign`
                    # (services/advertiser.py), so `advertiser=owner` is
                    # exercised through its real reading rather than a column.
                    url=(
                        f"https://www.idealista.com/en/inmueble/9{index:05d}/"
                        "?utm_campaign=express_newAd_sale_particular"
                    ),
                    search_profile_id=profile.id,
                    listing_status="removed" if withdrawn else "active",
                    scoring=(
                        {"coverage": {"share": 1.0}}
                        if index % 2 == 0
                        else {"coverage": {"share": 0.4}}
                    ),
                )
            )
        db.session.commit()
        yield profile.id


def _rows(client, href: str) -> int:
    """CSV records, header excluded -- a field may hold a newline."""
    body = client.get(href).get_data(as_text=True)
    return max(len(list(csv.reader(io.StringIO(body)))) - 1, 0)


class TestTheDefaultDependsOnWhereTheRequestCameFrom:
    """ON by default; OFF only when the form said so by leaving it out."""

    def test_a_bare_page_hides_the_withdrawn_listings(self, client, listings):
        assert _count(client.get("/properties").get_data(as_text=True)) == LIVE

    def test_the_form_with_the_box_unticked_shows_them(self, client, listings):
        # What an Apply looks like: the two hidden markers, no `hide_removed`.
        body = client.get(
            "/properties?mode=combined&view_type=list&sort=created_at&order=desc"
        ).get_data(as_text=True)
        assert _count(body) == LIVE + GONE

    def test_the_form_with_the_box_ticked_hides_them(self, client, listings):
        body = client.get(
            "/properties?mode=combined&view_type=list&hide_removed=on"
        ).get_data(as_text=True)
        assert _count(body) == LIVE

    def test_a_cross_page_link_gets_the_default(self, client, listings):
        """/profiles, /map and /profiles/<id>/edit link in with `profile_id`.

        That used to read as a submitted form with the box unticked, so opening
        a subscription from `/profiles` showed the withdrawn listings the bare
        page hides -- two routes into one view, disagreeing silently.
        """
        body = client.get(f"/properties?profile_id={listings}").get_data(as_text=True)
        assert _count(body) == LIVE

    @pytest.mark.parametrize(
        "query",
        [
            "advertiser=owner",
            "source=idealista",
            "municipality=Castrillon",
            "category=land",
        ],
    )
    def test_a_hand_typed_filter_gets_the_default(self, client, listings, query):
        """Naming a filter is not asking to see the withdrawn listings.

        This is what adding `source` and `advertiser` to the old list would have
        broken: they would have made a hand-typed filter read as an unticked
        checkbox.
        """
        body = client.get(f"/properties?{query}").get_data(as_text=True)
        assert _count(body) == LIVE

    def test_an_explicit_off_is_obeyed(self, client, listings):
        """`hide_removed=off` is the spelling `drilldown_args` already sends."""
        body = client.get(
            f"/properties?municipality=Castrillon&profile_id={listings}&hide_removed=off"
        ).get_data(as_text=True)
        assert _count(body) == LIVE + GONE

    def test_the_switch_round_trips(self, client, listings):
        """Press it, and press it again: the page has to come back."""
        body = client.get("/properties").get_data(as_text=True)
        assert _count(body) == LIVE

        pressed = client.get(_hide_removed_toggle(body)).get_data(as_text=True)
        assert _count(pressed) == LIVE + GONE

        again = client.get(_hide_removed_toggle(pressed)).get_data(as_text=True)
        assert _count(again) == LIVE


class TestTheExportAgreesWithThePage:
    """A CSV taken from a page describes the set that page is showing."""

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "measured=full",
            "municipality=Castrillon",
            "category=land",
            "mode=combined&view_type=list&sort=created_at&order=desc",
            "mode=combined&view_type=list&hide_removed=on",
        ],
    )
    def test_the_export_link_returns_what_the_page_counted(
        self, client, listings, query
    ):
        body = client.get(f"/properties?{query}").get_data(as_text=True)
        assert _rows(client, _export_href(body)) == _count(body)

    def test_measured_reaches_the_export_route(self, client, listings):
        """The export did not implement `measured` at all.

        Production, 2026-08-20: the page found 72 and its export returned 471.
        """
        page = client.get("/properties?measured=full").get_data(as_text=True)
        counted = _count(page)
        assert 0 < counted < LIVE + GONE, "the fixture must actually narrow here"
        assert _rows(client, _export_href(page)) == counted

    def test_the_export_carries_the_status_scope_in_both_directions(
        self, client, listings
    ):
        """Not only `on`: a page showing the withdrawn listings exports them."""
        shown = client.get(
            "/properties?mode=combined&view_type=list&sort=created_at&order=desc"
        ).get_data(as_text=True)
        assert _count(shown) == LIVE + GONE
        href = _export_href(shown)
        assert "hide_removed=off" in href, href
        assert _rows(client, href) == LIVE + GONE

    def test_every_filter_the_page_applies_is_on_the_export_link(
        self, client, listings, app
    ):
        """Close the class, not the one example `measured` happened to be.

        Renders with every filter set to something non-empty, reads
        `current_filters` out of the template context, and requires each key to
        reach the export link or be named in `NOT_EXPORTED` with a reason.
        """
        from flask import template_rendered

        captured = {}

        def record(sender, template, context, **extra):
            captured["filters"] = context.get("current_filters")

        template_rendered.connect(record, app)
        try:
            body = client.get(
                f"/properties?profile_id={listings}"
                "&category=land&subtype=plot&municipality=Castrillon"
                "&source=idealista&advertiser=owner&search=plot"
                "&inv_metr=GOOD&sea_view=likely&measured=full"
                "&favorites=on&hide_removed=on"
                "&mode=combined&view_type=list&sort=price&order=asc"
            ).get_data(as_text=True)
        finally:
            template_rendered.disconnect(record, app)

        filters = captured.get("filters")
        # The route turns a template error into a flash and a second render
        # whose `current_filters` holds three keys -- which would pass every
        # assertion below vacuously.
        assert filters and "category" in filters, "the page did not really render"

        exported = dict(parse_qsl(urlparse(_export_href(body)).query))
        missing = [
            key
            for key, value in filters.items()
            if value not in ("", None, False, [])
            and key not in NOT_EXPORTED
            and EXPORT_PARAM_NAMES.get(key, key) not in exported
        ]
        assert not missing, (
            f"the Export CSV link drops {missing}; add them to the link in "
            f"templates/properties.html, or to NOT_EXPORTED with the reason"
        )


class TestTheRuleHasOneHome:
    def test_neither_route_keeps_its_own_list_of_filter_names(self):
        """The two lists are what drifted; a third would drift the same way."""
        from pathlib import Path

        source = Path("routes/main_routes.py").read_text()
        assert "form_submitted" not in source

    def test_both_routes_read_the_shared_rule(self, client, listings, monkeypatch):
        """Wiring, not just the unit: a green module over a dead hook is #309.

        Forcing the shared reading has to move the page *and* the export.
        """
        monkeypatch.setattr(
            "routes.main_routes.resolve_hide_removed", lambda args: False
        )
        body = client.get("/properties").get_data(as_text=True)
        assert _count(body) == LIVE + GONE
        assert _rows(client, _export_href(body)) == LIVE + GONE

        monkeypatch.setattr(
            "routes.main_routes.resolve_hide_removed", lambda args: True
        )
        body = client.get("/properties?mode=combined&view_type=list").get_data(
            as_text=True
        )
        assert _count(body) == LIVE
        assert _rows(client, _export_href(body)) == LIVE
