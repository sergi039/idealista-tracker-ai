"""Which site a listing is on: one reading, four surfaces, one badge.

`properties.url` is the only record of the source, and before this it was read
nowhere -- so the page could not say a row was fotocasa, the filter could not
select them, and `services/listing_verification.py` told every reader the row
had been checked "against Idealista" whatever site it was on.

The tests that matter here are the ones about *narrowness*. A source filter
built on `url ILIKE '%fotocasa%'` passes the obvious cases and quietly matches
an idealista listing whose tracking tail happens to name a competitor, so the
clause is anchored on the host and these pin that it is.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils.listing_source import (
    FOTOCASA,
    IDEALISTA,
    OTHER,
    UNKNOWN,
    source_filter_clause,
    source_label,
    source_of_url,
)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Plots",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        db.session.add_all(
            [
                Property(
                    source_email_id="idealista-1",
                    title="Plot in Cudillero",
                    search_profile_id=profile.id,
                    url="https://www.idealista.com/en/inmueble/91523456/?utm_source=alerts-id",
                ),
                Property(
                    source_email_id="fotocasa:190280914",
                    title="Land for sale in Llaranes, Avilés",
                    search_profile_id=profile.id,
                    url="https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d",
                ),
                Property(
                    source_email_id="agency-1",
                    title="Plot from an agency",
                    search_profile_id=profile.id,
                    url="https://inmobiliaria-example.es/detalle-inmuebles.php?id=2546",
                ),
                Property(
                    source_email_id="no-url",
                    title="A row with no link at all",
                    search_profile_id=profile.id,
                    url=None,
                ),
            ]
        )
        db.session.commit()
        yield app
        db.drop_all()


class TestReading:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://www.idealista.com/en/inmueble/1/", IDEALISTA),
            ("https://idealista.com/es/inmueble/1/", IDEALISTA),
            ("https://www.fotocasa.es/en/buy/land/x/y/1/d", FOTOCASA),
            ("fotocasa.es/en/buy/land/x/y/1/d", FOTOCASA),
            ("https://inmobiliaria-example.es/x", OTHER),
            (None, UNKNOWN),
            ("", UNKNOWN),
            ("   ", UNKNOWN),
        ],
    )
    def test_the_host_decides(self, url, expected):
        assert source_of_url(url) == expected

    def test_a_competitor_named_in_the_query_is_not_the_source(self):
        """The whole point of parsing the host instead of matching a substring."""
        assert (
            source_of_url("https://www.idealista.com/en/inmueble/1/?from=fotocasa.es")
            == IDEALISTA
        )
        assert (
            source_of_url("https://example.com/redirect?to=www.idealista.com/x")
            == OTHER
        )

    def test_a_badge_never_shows_a_bare_slug(self):
        for source in (IDEALISTA, FOTOCASA, OTHER, UNKNOWN, "nonsense", None):
            assert source_label(source)[0].isupper()


class TestClause:
    def _ids(self, source):
        query = Property.query
        clause = source_filter_clause(Property, source)
        if clause is not None:
            query = query.filter(clause)
        return {row.source_email_id for row in query.all()}

    def test_each_source_selects_its_own(self, app):
        with app.app_context():
            assert self._ids(IDEALISTA) == {"idealista-1"}
            assert self._ids(FOTOCASA) == {"fotocasa:190280914"}
            assert self._ids(OTHER) == {"agency-1"}
            assert self._ids(UNKNOWN) == {"no-url"}

    def test_the_four_partition_the_table(self, app):
        """Every row belongs to exactly one source, so nothing can hide."""
        with app.app_context():
            total = Property.query.count()
            selected = [
                self._ids(source) for source in (IDEALISTA, FOTOCASA, OTHER, UNKNOWN)
            ]
            union = set().union(*selected)
            assert len(union) == total
            assert sum(len(group) for group in selected) == total

    def test_an_unset_or_unknown_filter_selects_everything(self, app):
        with app.app_context():
            total = Property.query.count()
            for value in ("", None, "  ", "zillow"):
                assert source_filter_clause(Property, value) is None
                assert len(self._ids(value)) == total

    def test_a_tracking_tail_naming_another_site_does_not_move_a_row(self, app):
        """The substring version of this clause fails exactly here."""
        with app.app_context():
            row = Property.query.filter_by(source_email_id="idealista-1").one()
            row.url = (
                "https://www.idealista.com/en/inmueble/1/?utm_campaign=vs-fotocasa.es/x"
            )
            db.session.commit()

            assert "idealista-1" in self._ids(IDEALISTA)
            assert "idealista-1" not in self._ids(FOTOCASA)


class TestSurfaces:
    def test_the_listing_page_filters_by_source(self, app):
        with app.test_client() as client:
            response = client.get("/properties?source=fotocasa")
            assert response.status_code == 200
            body = response.get_data(as_text=True)
            assert "Llaranes" in body
            assert "Plot in Cudillero" not in body

    def test_the_csv_export_filters_by_the_same_clause(self, app):
        with app.test_client() as client:
            response = client.get("/properties/export.csv?source=fotocasa")
            assert response.status_code == 200
            body = response.get_data(as_text=True)
            assert "Llaranes" in body
            assert "Plot in Cudillero" not in body

    def test_the_json_api_filters_by_the_same_clause(self, app):
        with app.test_client() as client:
            response = client.get("/api/properties?source=fotocasa")
            assert response.status_code == 200
            payload = response.get_json()
            titles = [
                item.get("title")
                for item in (payload.get("properties") or payload.get("items") or [])
            ]
            assert any("Llaranes" in (title or "") for title in titles)
            assert not any("Cudillero" in (title or "") for title in titles)

    def test_the_badge_marks_the_exception_not_every_row(self, app):
        """675 of 732 rows are idealista; badging those marks nothing.

        Counted on the badge's own icon rather than on the word: "Idealista"
        appears thirteen times in a rendered page -- the title, the navbar,
        tooltips -- so asserting on the label would pass whatever the badge
        did.
        """
        with app.test_client() as client:
            body = client.get("/properties").get_data(as_text=True)

            # Three rows are not idealista: fotocasa, the agency, and the one
            # with no link. Exactly those three carry a source badge.
            assert body.count("fas fa-link me-1") == 3
            assert "Fotocasa" in body
            assert "Other site" in body
            assert "No link" in body

    def test_only_the_shown_rows_are_badged(self, app):
        """Filtering to idealista leaves no source badge on the page at all."""
        with app.test_client() as client:
            body = client.get("/properties?source=idealista").get_data(as_text=True)

            assert "Plot in Cudillero" in body
            assert body.count("fas fa-link me-1") == 0


# Every shape a stored URL can take that has ever mattered here, plus the ones
# an adversarial review found. The point of the list is that it is fed to
# *both* readings below and they must agree on every entry: the badge on the
# row comes from `source_of_url` and the filter under it comes from
# `source_filter_clause`, and this file exists because those two disagreeing is
# the defect the module was written to remove.
AGREEMENT_CORPUS = [
    "https://www.idealista.com/en/inmueble/91523456/",
    "https://www.idealista.com/en/inmueble/91523456/?utm_source=alerts-id",
    "http://idealista.com/es/inmueble/1/",
    "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d",
    "https://m.fotocasa.es/es/comprar/terreno/a/b/1/d",
    "https://inmobiliaria-example.es/detalle-inmuebles.php?id=2546",
    # A host with no path at all. urlsplit reads the hostname; a LIKE pattern
    # requiring a trailing slash does not.
    "https://www.fotocasa.es",
    "https://www.idealista.com",
    # A query glued straight to the host, and a fragment.
    "https://www.fotocasa.es?ref=1",
    "https://www.fotocasa.es#top",
    # Another site's URL carried inside a query parameter. The unanchored
    # clause matched these; `source_of_url` never did.
    "https://example.com/x?ref=www.idealista.com/foo",
    "https://redirect.example.com/away?to=http://idealista.com/foo",
    "https://example.com/x?to=https://www.fotocasa.es/en/buy/land/a/b/1/d",
    # A lookalike host that must not be read as the real one.
    "https://www.idealista.com.evil.example/x",
    "https://notfotocasa.es/x",
    "https://fotocasa.es.evil.example/y",
]


class TestTheTwoReadingsAgree:
    """The SQL clause and the Python classifier, on the same corpus.

    Written after an adversarial review found them diverging two ways at once:
    the clause searched for `//idealista.com/` anywhere in the column, so a
    query parameter carrying another site's link matched it; and it required a
    slash after the host, so a path-less URL did not match the clause its own
    badge claimed. The single test that had guarded this used
    `?utm_campaign=vs-fotocasa.es/x`, whose hyphen -- not a dot -- meant it
    passed while the defect was live. A corpus, fed to both, cannot dodge it
    that way.
    """

    def test_every_url_lands_in_the_same_bucket_both_ways(self, app):
        with app.app_context():
            for index, url in enumerate(AGREEMENT_CORPUS):
                db.session.add(
                    Property(source_email_id=f"corpus-{index}", url=url, title=url)
                )
            db.session.commit()

            expected = {url: source_of_url(url) for url in AGREEMENT_CORPUS}

            for source in (IDEALISTA, FOTOCASA, OTHER, UNKNOWN):
                clause = source_filter_clause(Property, source)
                selected = {
                    row.url
                    for row in Property.query.filter(clause).all()
                    if row.source_email_id.startswith("corpus-")
                }
                should_be = {u for u, s in expected.items() if s == source}
                assert selected == should_be, (
                    f"source={source}: SQL and Python disagree on "
                    f"{selected ^ should_be}"
                )

    def test_a_host_named_in_a_query_is_not_the_source(self, app):
        """The exact false positive the unanchored clause had."""
        assert source_of_url("https://example.com/x?to=http://idealista.com/y") == OTHER
        with app.app_context():
            db.session.add(
                Property(
                    source_email_id="query-carrier",
                    url="https://example.com/x?to=http://idealista.com/y",
                )
            )
            db.session.commit()

            idealista_rows = {
                row.source_email_id
                for row in Property.query.filter(
                    source_filter_clause(Property, IDEALISTA)
                ).all()
            }
            assert "query-carrier" not in idealista_rows

    def test_a_lookalike_host_is_not_the_source(self, app):
        for url in (
            "https://www.idealista.com.evil.example/x",
            "https://fotocasa.es.evil.example/y",
            "https://notfotocasa.es/x",
        ):
            assert source_of_url(url) == OTHER
