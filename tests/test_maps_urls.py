"""The one Maps URL builder every surface goes through (proposal D2/D24).

The old templates concatenated free-text place names into
`https://www.google.com/maps/dir/{lat},{lon}/{name}` — unencoded spaces,
and a name Google may resolve to the wrong town entirely. These tests pin
the replacement: official `api=1` forms, everything encoded, place ids only
when they look like place ids, and no link at all when either endpoint is
missing.
"""

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from utils.maps_urls import maps_directions_url, maps_place_url

ORIGIN = (43.551663, -6.831426)
DEST = (43.5436, -6.72)
PLACE_ID = "ChIJd8BlQ2BZwokRAFUEcm_qrcA"


def _params(url):
    return parse_qs(urlparse(url).query)


class TestDirectionsUrl:
    def test_official_form_with_place_id(self):
        url = maps_directions_url(*ORIGIN, *DEST, place_id=PLACE_ID)
        assert url.startswith("https://www.google.com/maps/dir/?")
        params = _params(url)
        assert params["api"] == ["1"]
        assert params["origin"] == ["43.551663,-6.831426"]
        assert params["destination"] == ["43.543600,-6.720000"]
        assert params["destination_place_id"] == [PLACE_ID]
        assert params["travelmode"] == ["driving"]

    def test_no_place_id_param_when_absent(self):
        params = _params(maps_directions_url(*ORIGIN, *DEST))
        assert "destination_place_id" not in params

    def test_malformed_place_id_dropped_not_encoded(self):
        # A junk id would make Google pin the wrong thing; the coordinate
        # query alone is honest.
        for junk in ("", "   ", "two words", "short", 123, {"id": "x"}):
            params = _params(maps_directions_url(*ORIGIN, *DEST, place_id=junk))
            assert "destination_place_id" not in params

    def test_missing_either_endpoint_means_no_link(self):
        assert maps_directions_url(None, ORIGIN[1], *DEST) is None
        assert maps_directions_url(*ORIGIN, None, DEST[1]) is None
        assert maps_directions_url(*ORIGIN, DEST[0], None) is None

    def test_non_numeric_coordinates_rejected(self):
        # Strings and booleans mean the caller read the wrong field.
        assert maps_directions_url("43.55", -6.83, *DEST) is None
        assert maps_directions_url(True, -6.83, *DEST) is None
        assert maps_directions_url(float("nan"), -6.83, *DEST) is None
        assert maps_directions_url(float("inf"), -6.83, *DEST) is None

    def test_decimal_coordinates_accepted(self):
        url = maps_directions_url(Decimal("43.551663"), Decimal("-6.831426"), *DEST)
        assert _params(url)["origin"] == ["43.551663,-6.831426"]

    def test_unknown_travelmode_falls_back_to_driving(self):
        params = _params(maps_directions_url(*ORIGIN, *DEST, travelmode="rocket"))
        assert params["travelmode"] == ["driving"]

    def test_walking_mode_passes_through(self):
        params = _params(maps_directions_url(*ORIGIN, *DEST, travelmode="walking"))
        assert params["travelmode"] == ["walking"]


class TestPlaceUrl:
    def test_official_search_form(self):
        url = maps_place_url(*DEST, place_id=PLACE_ID)
        assert url.startswith("https://www.google.com/maps/search/?")
        params = _params(url)
        assert params["api"] == ["1"]
        assert params["query"] == ["43.543600,-6.720000"]
        assert params["query_place_id"] == [PLACE_ID]

    def test_no_coordinate_no_link(self):
        assert maps_place_url(None, DEST[1]) is None
        assert maps_place_url(DEST[0], "x") is None

    def test_malformed_place_id_dropped(self):
        assert "query_place_id" not in _params(maps_place_url(*DEST, place_id="a b"))
