"""`/properties/<id>` says which site the listing is on, and it is the row's own.

The page hardcoded "Idealista" in nine user-facing strings. That was true of
every row when they were written; measured 2026-08-31 it is false of 391 of the
1526 rows with a URL -- 253 yaencontre, 96 fotocasa, 32 pisos.com, 10
milanuncios. So the page offered to "Check status on Idealista" for a fotocasa
listing and told the owner "Nobody has read this listing off Idealista yet"
about a yaencontre one: the string `utils/listing_source.py` was written to
remove from the list, one page over.

Two things these tests are careful about, both lessons this repository already
paid for:

* **Assert the page rendered.** `routes/main_routes.py` turns a template error
  into a flash and a redirect, so "the wrong word is absent" is also what a
  broken template looks like. Every assertion here is made against a 200 whose
  body carries the listing.
* **The name comes from the one reading.** The badge, the source filter and its
  counts all read `utils/listing_source.py`; a page deriving its own would be
  free to disagree with the badge beside it.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment

IDEALISTA = "https://www.idealista.com/en/inmueble/91523456/"
YAENCONTRE = "https://www.yaencontre.com/venta/casa/inmueble-45358-112353204"
FOTOCASA = "https://www.fotocasa.es/es/comprar/vivienda/vigo/teis/190540646/d"
MILANUNCIOS = "https://www.milanuncios.com/venta-de-chalets/casa-612329827.htm"
AGENCY = "https://inmobiliariamalga.com/ficha/1234"


@pytest.fixture
def client():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Galicia costa",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        app.config["TEST_PROFILE_ID"] = profile.id
        with app.test_client() as test_client:
            yield test_client
        db.drop_all()


def _page(client, url):
    prop = Property(
        source_email_id=f"portal:{abs(hash(url))}",
        url=url,
        title="Casa adosada en venta en calle Rosa, Vigo",
        municipality="Vigo",
        price=205000,
        area=205,
    )
    db.session.add(prop)
    db.session.commit()
    response = client.get(f"/properties/{prop.id}")
    assert response.status_code == 200, "the template did not render"
    body = response.get_data(as_text=True)
    assert "Casa adosada en venta en calle Rosa" in body, "not this listing's page"
    return body


@pytest.mark.parametrize(
    "url,site",
    [
        (IDEALISTA, "Idealista"),
        (YAENCONTRE, "yaencontre"),
        (FOTOCASA, "Fotocasa"),
        (MILANUNCIOS, "Milanuncios"),
        (AGENCY, "Other site"),
    ],
)
def test_the_status_control_names_the_site_it_would_ask(client, url, site):
    body = _page(client, url)
    assert f"Check status on {site}" in body
    assert f"Nobody has read this listing off {site} yet" in body


def test_a_row_on_another_portal_never_says_idealista(client):
    """The whole point: the word appears only where it is true."""
    body = _page(client, YAENCONTRE)
    for claim in (
        "Check status on Idealista",
        "Nobody has read this listing off Idealista yet",
        "no longer available on Idealista",
    ):
        assert claim not in body


def test_the_runtime_messages_read_the_same_name(client):
    """The status check writes its sentences in JS, from the server's reading.

    Re-deriving the host in JavaScript would be a second reading of a URL, and
    the one nobody updates when a portal is added.
    """
    body = _page(client, FOTOCASA)
    assert 'window.LISTING_SITE = "Fotocasa";' in body
    assert "${window.LISTING_SITE} has refused the last" in body
    assert "Idealista is not consulted" not in body


def test_a_comparable_is_not_labelled_with_this_row_s_portal(client):
    """A comparable may be on another site and its payload carries no source."""
    body = _page(client, IDEALISTA)
    assert "></i>Open listing" in body
