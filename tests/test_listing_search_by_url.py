"""A pasted listing URL finds the listing (search box, all four surfaces).

The box read `title`, `description` and `municipality` only, so pasting the
link from the alert email -- the most natural way to look one listing up --
answered "0 properties found" for a row the table was holding. Measured
2026-08-17 against the live database: `https://www.idealista.com/en/inmueble/
91523456/` found nothing while property 351 carried exactly that listing id.

Three things are pinned here, and each fails differently if the clause in
utils/listing_search.py is weakened:

* the id path, which has to survive the `?utm_...` tail the stored URL carries
  and a different language segment in the pasted one;
* the URL path, for the 57 rows (of 730, same date) that are fotocasa or an
  agency's own site and have no Idealista id at all;
* the *narrowness* of the URL path -- an ordinary word must not start matching
  against `url`, or "terreno" would pull in every fotocasa link by its path.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils.listing_search import extract_listing_id, url_fragment


# The stored URL, as ingestion writes it: the email's own language segment and
# the ten tracking parameters idealista appends.
STORED_IDEALISTA_URL = (
    "https://www.idealista.com/en/inmueble/91523456/"
    "?utm_medium=email&utm_campaign=express_newAd_sale_particular"
    "&utm_source=alerts-id&utm_notification_id=133bcb49-5422-4dcf-8e1d"
)
STORED_FOTOCASA_URL = (
    "https://www.fotocasa.es/es/comprar/terreno/carreno/carreno/184639834/d"
)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
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
        db.session.add_all(
            [
                Property(
                    source_email_id="salamir",
                    title="Land plot in Salamir",
                    municipality="Cudillero",
                    search_profile_id=profile.id,
                    listing_status="active",
                    idealista_property_id=91523456,
                    url=STORED_IDEALISTA_URL,
                    location_lat=43.56,
                    location_lon=-6.21,
                ),
                Property(
                    source_email_id="fotocasa-carreno",
                    title="Plot in Carreno",
                    municipality="Carreno",
                    search_profile_id=profile.id,
                    listing_status="active",
                    # No listing id: this row is not from idealista at all.
                    url=STORED_FOTOCASA_URL,
                    location_lat=43.58,
                    location_lon=-5.71,
                ),
                Property(
                    source_email_id="other-listing",
                    title="House in Gijon",
                    municipality="Gijon",
                    search_profile_id=profile.id,
                    listing_status="active",
                    idealista_property_id=99999999,
                    url="https://www.idealista.com/es/inmueble/99999999/",
                    location_lat=43.53,
                    location_lon=-5.66,
                ),
            ]
        )
        db.session.commit()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


class TestExtractListingId:
    def test_reads_the_id_out_of_a_pasted_url(self):
        assert (
            extract_listing_id("https://www.idealista.com/en/inmueble/91523456/")
            == 91523456
        )

    def test_survives_the_tracking_tail_and_the_language_segment(self):
        assert extract_listing_id(STORED_IDEALISTA_URL) == 91523456
        assert extract_listing_id("idealista.com/es/inmueble/91523456/") == 91523456

    def test_reads_a_bare_id(self):
        assert extract_listing_id(" 91523456 ") == 91523456

    def test_a_number_too_large_for_the_column_is_not_an_id(self):
        # `idealista_property_id` is a bigint. Measured against this
        # deployment's PostgreSQL: the untyped literal psycopg2 sends returns
        # no rows, but the same value bound to a `bigint` parameter fails with
        # "bigint out of range". Only this guard makes the two agree -- and
        # SQLite, which the suite runs on, has no such range at all, so the
        # page-level test below cannot catch this one.
        assert extract_listing_id("9" * 25) is None

    def test_ordinary_text_names_no_listing(self):
        assert extract_listing_id("Salamir") is None
        assert extract_listing_id("") is None
        assert extract_listing_id(None) is None


class TestUrlFragment:
    def test_drops_scheme_www_tracking_and_trailing_slash(self):
        assert (
            url_fragment(STORED_IDEALISTA_URL) == "idealista.com/en/inmueble/91523456"
        )

    def test_keeps_a_query_parameter_that_identifies_the_listing(self):
        # A real row here: the agency carries the id in the query string.
        assert (
            url_fragment("https://inmobiliariarivero.es/detalle-inmuebles.php?id=2546")
            == "inmobiliariarivero.es/detalle-inmuebles.php?id=2546"
        )

    def test_a_word_is_not_a_url(self):
        assert url_fragment("terreno") is None
        assert url_fragment("Muros de Nalon") is None


class TestPastedUrlFindsTheListing:
    def test_properties_page_finds_it(self, client):
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/en/inmueble/91523456/",
            },
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Land plot in Salamir" in body
        assert "House in Gijon" not in body

    def test_a_bare_listing_id_finds_it(self, client):
        response = client.get(
            "/properties", query_string={"profile_id": "all", "search": "91523456"}
        )
        assert response.status_code == 200
        assert "Land plot in Salamir" in response.get_data(as_text=True)

    def test_a_link_copied_in_another_language_finds_it(self, client):
        # The row stores /en/; the owner may be reading the Spanish site.
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/es/inmueble/91523456/",
            },
        )
        assert response.status_code == 200
        assert "Land plot in Salamir" in response.get_data(as_text=True)

    def test_a_non_idealista_url_finds_its_row(self, client):
        response = client.get(
            "/properties",
            query_string={"profile_id": "all", "search": STORED_FOTOCASA_URL},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Plot in Carreno" in body
        assert "Land plot in Salamir" not in body

    def test_the_json_api_finds_it(self, client):
        response = client.get(
            "/api/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/en/inmueble/91523456/",
            },
        )
        assert response.status_code == 200
        payload = response.get_json()
        titles = [row["title"] for row in payload["properties"]]
        assert titles == ["Land plot in Salamir"]

    def test_the_csv_export_finds_it(self, client):
        response = client.get(
            "/properties/export.csv",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/en/inmueble/91523456/",
            },
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Land plot in Salamir" in body
        assert "House in Gijon" not in body

    def test_the_map_finds_it(self, client):
        response = client.get(
            "/map",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/en/inmueble/91523456/",
            },
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Land plot in Salamir" in body
        assert "House in Gijon" not in body


class TestTheTextSearchIsUnchanged:
    def test_a_word_still_matches_the_text_columns(self, client):
        response = client.get(
            "/properties", query_string={"profile_id": "all", "search": "Salamir"}
        )
        assert response.status_code == 200
        assert "Land plot in Salamir" in response.get_data(as_text=True)

    def test_a_word_does_not_start_matching_urls(self, client):
        # "terreno" appears in the fotocasa row's URL path and nowhere in its
        # text columns. Matching every query against `url` would widen every
        # ordinary search on this table.
        response = client.get(
            "/properties", query_string={"profile_id": "all", "search": "terreno"}
        )
        assert response.status_code == 200
        assert "Plot in Carreno" not in response.get_data(as_text=True)

    def test_a_wildcard_in_a_pasted_url_matches_literally(self, client):
        # `_` is a single-character wildcard in LIKE; a URL that does not
        # exist must not match one that does.
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.fotocasa.es/es/comprar/terreno/carreno/carreno/18463983_/d",
            },
        )
        assert response.status_code == 200
        assert "Plot in Carreno" not in response.get_data(as_text=True)

    def test_an_absurd_number_is_answered_not_crashed(self, client):
        response = client.get(
            "/properties", query_string={"profile_id": "all", "search": "9" * 25}
        )
        assert response.status_code == 200


class TestAnEmptyResultSaysWhatItLookedFor:
    """ "0 properties found" must not mean two different things silently.

    A pasted link is read as the listing it names rather than as text to
    match, so a zero can mean "no such listing here" or "understood
    differently from how you typed it". The line says which; it is derived
    from the same reading the filter was built from, so it cannot describe a
    search that did not happen.

    Every test here asserts the page rendered normally as well as what it
    said. `routes/main_routes.py` turns a template error into a flashed
    message and a second render with no rows, and that fallback also shows
    "0 properties found" with no disclosure line -- so a test that only looks
    for the line's absence passes just as happily when the page broke. That
    is not hypothetical: it is what the first version of this class did, and
    a mutation that made the line render for every query (where the URL is
    None and `truncate` raises) stayed green through it.
    """

    @staticmethod
    def _rendered_body(response):
        """The page body, once it is established the page really rendered."""
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "An error occurred while loading properties" not in body
        return body

    def test_a_missing_listing_id_is_named(self, client):
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/en/inmueble/12345678/",
            },
        )
        body = self._rendered_body(response)
        assert 'id="search-read-as"' in body
        assert "12345678" in body

    def test_a_missing_link_is_named(self, client):
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.fotocasa.es/es/comprar/terreno/gozon/gozon/1/d",
            },
        )
        body = self._rendered_body(response)
        assert 'id="search-read-as"' in body
        assert "fotocasa.es/es/comprar/terreno/gozon/gozon/1/d" in body

    def test_a_found_listing_says_nothing(self, client):
        # The rows on screen explain the query; a line about how it was read
        # would be noise.
        response = client.get(
            "/properties",
            query_string={
                "profile_id": "all",
                "search": "https://www.idealista.com/en/inmueble/91523456/",
            },
        )
        body = self._rendered_body(response)
        assert "Land plot in Salamir" in body
        assert 'id="search-read-as"' not in body

    def test_an_ordinary_word_with_no_answer_says_nothing(self, client):
        # Nothing was read differently from how it was typed, so there is
        # nothing to disclose -- an empty result is the whole answer.
        response = client.get(
            "/properties",
            query_string={"profile_id": "all", "search": "Villaviciosa"},
        )
        body = self._rendered_body(response)
        assert "0 properties found" in body
        assert 'id="search-read-as"' not in body
