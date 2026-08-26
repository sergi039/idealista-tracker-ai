"""The buildability filter cuts on the *curated* classification only.

The owner opened the subtype select looking for "where building is allowed"
and found regex-derived listing types instead (developed/plot) -- a
classification of what the advert is, not of what the land permits. The field
that answers his question is `attributes.land_classification`: curated by
hand-run scripts from planning documents and research sheets, preserved by
ingestion (tests/test_ingest_preserves_curated_fields.py), and drawn as a
badge on plot rows.

What must hold:

* Each named bucket matches its vocabulary value exactly; `classified` keeps
  any curated row (IS NOT NULL, so a future vocabulary value is not silently
  dropped from its own bucket).
* An uncurated row matches only "all": absence is not a classification (#98).
* An unknown value applies no filter and reports no narrowing (the
  `filter_bar_active` identity contract).
* The filter survives the page's own links and reaches the CSV export and the
  map (#439/#445) -- the sweeps in test_map_and_list_agree_on_the_filters.py
  and test_export_csv_matches_the_page.py walk it too; the cases here are the
  vocabulary's own.
"""

from __future__ import annotations

import re
from html import unescape

import pytest
from flask import template_rendered

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

CLASSIFICATIONS = {
    "solar_row": "urbano_solar",
    "urbanizable_row": "urbanizable",
    "claimed_row": "residential_claimed",
    "uncurated_row": None,
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
        for key, classification in CLASSIFICATIONS.items():
            db.session.add(
                Property(
                    source_email_id=f"build_{key}",
                    title=f"Row-{key}",
                    municipality="Castrillon",
                    property_category="land",
                    property_subtype="plot",
                    price=40000,
                    location_lat=43.5,
                    location_lon=-6.5,
                    search_profile_id=profile.id,
                    listing_status="active",
                    attributes=(
                        {"land_classification": classification}
                        if classification
                        else None
                    ),
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


class TestTheBuckets:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("solar", {"solar_row"}),
            ("urbanizable", {"urbanizable_row"}),
            ("claimed", {"claimed_row"}),
            ("classified", {"solar_row", "urbanizable_row", "claimed_row"}),
        ],
    )
    def test_each_bucket_matches_its_vocabulary(self, client, value, expected):
        body = client.get(f"/properties?profile_id=all&build={value}").get_data(
            as_text=True
        )
        assert _shown(body) == expected

    def test_an_uncurated_row_matches_only_all(self, client):
        for value in ("solar", "urbanizable", "claimed", "classified"):
            body = client.get(f"/properties?profile_id=all&build={value}").get_data(
                as_text=True
            )
            assert "uncurated_row" not in _shown(body)
        body = client.get("/properties?profile_id=all").get_data(as_text=True)
        assert "uncurated_row" in _shown(body)


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
        context = self._context(app, client, "/properties?profile_id=all&build=banana")
        assert context["pagination"].total == len(CLASSIFICATIONS)
        assert context["filter_bar_scope_total"] is None

    def test_a_real_value_is_reported_as_a_narrowing(self, app, client):
        context = self._context(app, client, "/properties?profile_id=all&build=solar")
        assert context["pagination"].total == 1
        assert context["filter_bar_scope_total"] == len(CLASSIFICATIONS)


class TestTheFilterTravels:
    def test_the_sort_headers_carry_it(self, client):
        body = client.get("/properties?profile_id=all&build=solar").get_data(
            as_text=True
        )
        sort_link = re.search(r'href="(/properties\?[^"]*sort=price[^"]*)"', body)
        assert sort_link, "the page drew no price sort header"
        assert "build=solar" in unescape(sort_link.group(1))

    def test_the_export_link_carries_it_and_the_export_applies_it(self, client):
        body = client.get("/properties?profile_id=all&build=solar").get_data(
            as_text=True
        )
        export = re.search(r'href="(/properties/export\.csv[^"]*)"', body)
        assert export, "the page drew no Export CSV link"
        assert "build=solar" in unescape(export.group(1))

        resp = client.get("/properties/export.csv?profile_id=all&build=solar")
        assert resp.status_code == 200
        assert _shown(resp.get_data(as_text=True)) == {"solar_row"}

    def test_the_map_applies_it(self, client):
        body = client.get("/map?profile_id=all&build=solar").get_data(as_text=True)
        match = re.search(r"const markers\s*=\s*(\[.*?\]);", body, re.DOTALL)
        assert match, "could not find the marker payload on the map page"
        assert set(re.findall(r"Row-(\w+)", unescape(match.group(1)))) == {"solar_row"}


class TestTheControl:
    def test_the_select_offers_the_buckets_and_keeps_its_selection(self, client):
        body = client.get("/properties?profile_id=all&build=solar").get_data(
            as_text=True
        )
        select = re.search(r'<select[^>]*name="build".*?</select>', body, re.DOTALL)
        assert select, "the filter bar has no buildability select"
        assert 'value="solar" selected' in select.group(0)
        for value in ("urbanizable", "claimed", "classified"):
            assert f'value="{value}"' in select.group(0)
