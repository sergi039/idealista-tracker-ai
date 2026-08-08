"""Regression tests for the /properties (and /properties/export.csv, /map)
"all profiles vs. one profile" selector.

Before this fix, `routes/main_routes.py` had no way to distinguish "no
profile_id in the URL" from "the user explicitly asked for every profile":
`request.args.get("profile_id", type=int)` returned None either way, and a
None `selected_profile_id` was *always* replaced by an auto-selected concrete
profile. There was no reachable state -- not even a hand-typed URL -- that
left `Property.query` unfiltered by `search_profile_id`, so a page showing
more than one profile's listings at once was impossible.
"""

import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment


def _extract_href(body, title_text):
    """Pull the href of the <a ...> whose title attribute is `title_text`.

    The templates emit `href="..."` on the line before `title="..."`, so a
    plain substring check like `"profile_id=all" in body` can't tell *which*
    link on the page carries it -- it passes as long as any one link does,
    even if a different link silently lost it. Pinning each link's own href
    is the only way to prove both independently.
    """
    match = re.search(rf'href="([^"]*)"\s*\n?\s*title="{re.escape(title_text)}"', body)
    assert match, f"no link with title={title_text!r} found in the page"
    return match.group(1)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def two_profiles_with_properties(app):
    """Two active profiles, each with one uniquely-titled property, plus
    distinct coordinates so the /map view can be exercised too."""
    with app.app_context():
        profile_a = SearchProfile(
            name="Land at Norte",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        profile_b = SearchProfile(
            name="Homes in Ciudad Quesada",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([profile_a, profile_b])
        db.session.commit()

        prop_a = Property(
            source_email_id="selector_test_a",
            title="AlphaListingUniqueTitle",
            municipality="Alicante",
            search_profile_id=profile_a.id,
            listing_status="active",
            location_lat=38.34,
            location_lon=-0.48,
        )
        prop_b = Property(
            source_email_id="selector_test_b",
            title="BetaListingUniqueTitle",
            municipality="Quesada",
            search_profile_id=profile_b.id,
            listing_status="active",
            location_lat=38.10,
            location_lon=-0.70,
        )
        db.session.add_all([prop_a, prop_b])
        db.session.commit()

        return {
            "profile_a_id": profile_a.id,
            "profile_b_id": profile_b.id,
        }


class TestPropertiesPageProfileSelector:
    def test_all_profiles_returns_listings_from_more_than_one_profile(
        self, client, two_profiles_with_properties
    ):
        resp = client.get("/properties?profile_id=all")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "AlphaListingUniqueTitle" in body
        assert "BetaListingUniqueTitle" in body

    def test_empty_profile_id_is_equivalent_to_all(
        self, client, two_profiles_with_properties
    ):
        resp = client.get("/properties?profile_id=")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "AlphaListingUniqueTitle" in body
        assert "BetaListingUniqueTitle" in body

    def test_specific_profile_returns_only_that_profiles_listings(
        self, client, two_profiles_with_properties
    ):
        profile_a_id = two_profiles_with_properties["profile_a_id"]
        resp = client.get(f"/properties?profile_id={profile_a_id}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "AlphaListingUniqueTitle" in body
        assert "BetaListingUniqueTitle" not in body

        profile_b_id = two_profiles_with_properties["profile_b_id"]
        resp2 = client.get(f"/properties?profile_id={profile_b_id}")
        assert resp2.status_code == 200
        body2 = resp2.get_data(as_text=True)
        assert "BetaListingUniqueTitle" in body2
        assert "AlphaListingUniqueTitle" not in body2

    def test_all_profiles_selection_survives_into_csv_export_and_map_links(
        self, client, two_profiles_with_properties
    ):
        """The Export CSV / Map buttons must forward the explicit "all"
        choice, not silently fall back to a single auto-selected profile.
        Each link's href is checked independently -- a single "profile_id=all
        is somewhere on the page" assertion would still pass if only one of
        the two links carried it."""
        resp = client.get("/properties?profile_id=all")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        csv_href = _extract_href(body, "Download filtered data as CSV")
        assert "profile_id=all" in csv_href

        map_href = _extract_href(body, "View properties on map")
        assert "profile_id=all" in map_href

    def test_no_profile_id_param_preserves_prior_auto_select_behavior(
        self, client, two_profiles_with_properties
    ):
        """A bare /properties request (no profile_id at all, e.g. an old
        bookmarked link) must keep resolving to a single concrete profile,
        not silently become "all"."""
        resp = client.get("/properties")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Exactly one of the two listings shows up, never both.
        assert ("AlphaListingUniqueTitle" in body) != ("BetaListingUniqueTitle" in body)


class TestExportCsvProfileSelector:
    def _rows(self, csv_text):
        import csv
        import io

        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        return rows[0], rows[1:]

    def test_all_profiles_csv_includes_rows_from_more_than_one_profile(
        self, client, two_profiles_with_properties
    ):
        resp = client.get("/properties/export.csv?profile_id=all")
        assert resp.status_code == 200
        header, rows = self._rows(resp.get_data(as_text=True))
        profile_id_col = header.index("Profile ID")
        profile_ids_in_csv = {row[profile_id_col] for row in rows}
        assert str(two_profiles_with_properties["profile_a_id"]) in profile_ids_in_csv
        assert str(two_profiles_with_properties["profile_b_id"]) in profile_ids_in_csv

    def test_specific_profile_csv_includes_only_that_profile(
        self, client, two_profiles_with_properties
    ):
        profile_a_id = two_profiles_with_properties["profile_a_id"]
        resp = client.get(f"/properties/export.csv?profile_id={profile_a_id}")
        assert resp.status_code == 200
        header, rows = self._rows(resp.get_data(as_text=True))
        profile_id_col = header.index("Profile ID")
        profile_ids_in_csv = {row[profile_id_col] for row in rows}
        assert profile_ids_in_csv == {str(profile_a_id)}


class TestMapViewProfileSelector:
    def test_all_profiles_map_includes_markers_from_more_than_one_profile(
        self, client, two_profiles_with_properties
    ):
        resp = client.get("/map?profile_id=all")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "AlphaListingUniqueTitle" in body
        assert "BetaListingUniqueTitle" in body

    def test_specific_profile_map_includes_only_that_profiles_markers(
        self, client, two_profiles_with_properties
    ):
        profile_a_id = two_profiles_with_properties["profile_a_id"]
        resp = client.get(f"/map?profile_id={profile_a_id}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "AlphaListingUniqueTitle" in body
        assert "BetaListingUniqueTitle" not in body
