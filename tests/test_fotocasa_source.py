"""What a real fotocasa page yields, pinned against the real payload.

The fixture is not hand-written. `tests/data/fotocasa_listing_190280914.html`
carries the verbatim 40 KB `__initial_props__` block served by fotocasa.es for
https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d on 2026-08-17,
wrapped in a minimal page. A parser test whose fixture was written by the same
person as the parser tests that person's idea of the format, which is the one
thing already known to be right.
"""

import pathlib

import pytest

from services.fotocasa_source import (
    REFUSAL_BLOCKED,
    REFUSAL_NOT_FOTOCASA,
    REFUSAL_NO_PAYLOAD,
    REFUSAL_UNREADABLE,
    is_fotocasa_url,
    listing_id_from_url,
    normalize_url,
    parse_listing,
    split_urls,
)

URL = "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d"
FIXTURE = pathlib.Path(__file__).parent / "data" / "fotocasa_listing_190280914.html"


@pytest.fixture(scope="module")
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_reads_every_field_the_scorer_needs(page):
    listing = parse_listing(page, URL)

    assert listing.ok, listing.refusal
    assert listing.listing_id == 190280914
    assert listing.price == 68000.0
    assert listing.area == 1945.0
    assert listing.area_type == "plot"
    assert listing.deal_type == "sale"
    assert listing.latitude == pytest.approx(43.570805)
    assert listing.longitude == pytest.approx(-5.8932443)
    assert listing.description and len(listing.description) == 825


def test_municipality_is_the_municipality_not_the_district(page):
    """The two address blocks disagree, and only one of them is right.

    `realEstate.address.municipality` is "Avilés" (with `cityId: 33004`, which
    is Avilés's INE code); `realEstateAdDetailEntityV2.address.municipality` is
    "Llaranes", the district. `utils/municipality_grouping.py` groups four
    listing surfaces on this string and `/municipalities` joins it to INE, so
    reading the wrong block invents a municipality no join can resolve.
    """
    listing = parse_listing(page, URL)

    assert listing.municipality == "Avilés"
    assert listing.district == "Llaranes"
    assert listing.province == "Asturias"
    assert listing.postal_code == "33490"


def test_zero_is_absent_not_a_measurement(page):
    """This plot carries `rooms: 0, bathrooms: 0` -- fotocasa's blank.

    Storing those as real counts would put a fabricated fact where the page
    said nothing, which is #98 with a number in the place of a blank.
    """
    listing = parse_listing(page, URL)

    assert listing.attributes == {}


def test_the_coordinate_is_never_called_precise(page):
    """`precise` unlocks a paid travel run; the portal says `isExact: false`."""
    listing = parse_listing(page, URL)

    assert listing.portal_accuracy["is_exact"] is False
    assert listing.portal_accuracy["coordinates_accuracy"] == 0
    assert listing.portal_accuracy["record_accuracy"] is False


def test_title_carries_the_place_so_the_geocoder_has_something_to_read(page):
    """`propertyTitle` is the bare "Land for sale"; `seoTitle` names the place.

    `PropertyLocationService._build_geocoding_queries` takes the text after
    "in", so the generic one would hand the geocoder an empty query.
    """
    listing = parse_listing(page, URL)

    assert listing.title == "Land for sale in Llaranes, Avilés"
    assert " in " in listing.title


def test_a_hidden_price_is_absent_not_free():
    page = (
        '<script type="application/json" id="__initial_props__">'
        '{"realEstate": {"id": 1, "price": 68000, "showPrice": false}}'
        "</script>"
    )
    listing = parse_listing(page, URL)

    assert listing.ok
    assert listing.price is None


def test_a_rental_is_not_recorded_as_a_sale():
    page = (
        '<script type="application/json" id="__initial_props__">'
        '{"realEstate": {"id": 1, "price": 700, "transactionTypeId": 3}}'
        "</script>"
    )
    assert parse_listing(page, URL).deal_type == "rent"


@pytest.mark.parametrize(
    "html, expected",
    [
        ("", REFUSAL_NO_PAYLOAD),
        ("<html><title>SENTIMOS LA INTERRUPCIÓN</title></html>", REFUSAL_BLOCKED),
        (
            '<script type="application/json" id="__initial_props__">{oops</script>',
            REFUSAL_UNREADABLE,
        ),
        (
            '<script type="application/json" id="__initial_props__">{"a":1}</script>',
            REFUSAL_UNREADABLE,
        ),
    ],
)
def test_a_page_that_says_nothing_refuses_by_name(html, expected):
    listing = parse_listing(html, URL)

    assert listing.refusal == expected
    assert not listing.ok
    # Every field stays empty: a refusal must not leave a half-read row that
    # the preview would show as a listing.
    assert listing.price is None and listing.area is None
    assert listing.municipality is None


class TestUrls:
    def test_a_search_results_page_names_no_listing(self):
        """robots.txt disallows walking results; nothing here may accept one."""
        results = "https://www.fotocasa.es/en/buy/lands/asturias-province/all-zones/l"

        assert is_fotocasa_url(results)
        assert listing_id_from_url(results) is None

    def test_idealista_is_not_fotocasa(self):
        assert not is_fotocasa_url("https://www.idealista.com/en/inmueble/91523456/")
        assert (
            listing_id_from_url("https://www.idealista.com/en/inmueble/91523456/")
            is None
        )

    def test_the_id_survives_language_and_tracking(self):
        for url in (
            URL,
            "www.fotocasa.es/es/comprar/terreno/aviles/llaranes/190280914/d?utm_source=x",
            "fotocasa.es/de/kaufen/grundstuck/aviles/llaranes/190280914/d/",
        ):
            assert listing_id_from_url(url) == 190280914

    def test_normalize_drops_the_query(self):
        assert normalize_url(URL + "?utm_source=alert&x=1") == URL

    def test_two_languages_of_one_listing_are_one_link(self):
        """Keyed on the id, not the path: fetching it twice pays the gate twice."""
        pasted = (
            "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d\n"
            "https://www.fotocasa.es/es/comprar/terreno/aviles/llaranes/190280914/d\n"
            "https://www.fotocasa.es/en/buy/land/gozon/x/190210058/d"
        )

        assert len(split_urls(pasted)) == 2

    def test_a_non_listing_link_is_refused_before_any_request(self, monkeypatch):
        """No socket is opened for something that cannot be a listing."""
        from services import fotocasa_source

        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("a request was made for a non-listing URL")

        monkeypatch.setattr(fotocasa_source, "request_with_retries", explode)

        listing = fotocasa_source.fetch_listing("https://www.fotocasa.es/en/buy/x/l")

        assert listing.refusal == REFUSAL_NOT_FOTOCASA
