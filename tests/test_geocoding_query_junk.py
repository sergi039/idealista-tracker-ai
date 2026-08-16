"""The geocoding query carries the address and nothing else (#342, item 3).

Two artefacts of the portal's titles were being handed to Google as if they
were part of an address.

**"n/a" is Idealista's missing street number.** 42 of 401 production titles
carry one, and it rode into the query as a component:
`Tiñana, n/a, Viella-Granda-Meres, Siero, Spain`.

**A preposition in a sentence is not a separator.** `_LOCATION_FROM_TITLE_RE`
takes everything after the first "in", which is right for the portal's
`<type> in <location>` form and wrong for a description:

    FINCA 529 An excellent investment opportunity is presented in a farm for
    sale, loca

from which it extracted `a farm for sale, loca` and asked Google to geocode
it. Three production rows are that shape, and all three landed on the centroid
of Spain.

Measured over all 401 titles on 2026-08-16: the marker sits at word 1, 2 or 3
in 392 of them, at word 8 in exactly those three, and nowhere in between. The
threshold is placed in that gap rather than on a guessed boundary.

What this does **not** claim: that a cleaner query changes Google's answer for
any particular row. That cannot be checked without spending the owner's
geocoding quota. #331 and #348 are what stop a bad answer being stored; this
stops a bad question being asked, and saves the paid call that asking it costs.
"""

import pytest

from models import Property
from services.property_location_service import _build_geocoding_queries


def _prop(title, municipality=None):
    return Property(
        source_email_id="query-junk", title=title, municipality=municipality
    )


class TestThePlaceholderIsDropped:
    def test_n_a_does_not_reach_the_query(self):
        queries = _build_geocoding_queries(
            _prop("Land in Tiñana, n/a, Viella-Granda-Meres, Siero 90,000 €", "Siero")
        )
        assert queries[0] == "Tiñana, Viella-Granda-Meres, Siero, Spain"
        assert not any("n/a" in q.lower() for q in queries)

    def test_it_is_dropped_from_a_title_with_no_marker_too(self):
        """`parcela 37, ...` carries no "in" and uses the whole title."""
        queries = _build_geocoding_queries(
            _prop("parcela 37, n/a, San Claudio-Trubia-Las Caldas, Oviedo 49,000 €")
        )
        assert queries[0] == (
            "parcela 37, San Claudio-Trubia-Las Caldas, Oviedo, Spain"
        )

    def test_a_real_street_number_survives(self):
        """Only the placeholder goes; "9" is an address."""
        queries = _build_geocoding_queries(
            _prop("Flat / apartment in calle Dean D. Antonio Sala, 9, Centro, Alicante")
        )
        assert queries[0] == "calle Dean D. Antonio Sala, 9, Centro, Alicante, Spain"

    def test_s_n_survives(self):
        """`s/n` is sin número -- a real convention, not a placeholder."""
        queries = _build_geocoding_queries(_prop("Land in calle Mayor, s/n, Carreño"))
        assert "s/n" in queries[0]


class TestProseIsNotAnAddress:
    def test_a_buried_marker_does_not_become_the_location(self):
        title = (
            "FINCA 529 An excellent investment opportunity is presented in a farm "
            "for sale, loca"
        )
        queries = _build_geocoding_queries(_prop(title, "Lugar Otero"))
        assert "a farm for sale, loca, Spain" not in queries
        assert queries[0].startswith("FINCA 529")

    @pytest.mark.parametrize(
        "title,expected",
        [
            # The three legitimate prefix lengths seen in production.
            ("Land in Tiñana, Siero", "Tiñana, Siero, Spain"),
            ("Land plot in Barrio la Zampudia", "Barrio la Zampudia, Spain"),
            (
                "Flat / apartment in Centro, San Juan de Alicante",
                "Centro, San Juan de Alicante, Spain",
            ),
        ],
    )
    def test_the_portal_format_is_untouched(self, title, expected):
        assert _build_geocoding_queries(_prop(title))[0] == expected

    def test_the_municipality_is_still_offered_as_a_second_candidate(self):
        """The fallthrough this whole loop depends on must survive."""
        queries = _build_geocoding_queries(
            _prop("Land in Tiñana, Viella-Granda-Meres, Siero", "Siero")
        )
        assert queries == ["Tiñana, Viella-Granda-Meres, Siero, Spain", "Siero, Spain"]
