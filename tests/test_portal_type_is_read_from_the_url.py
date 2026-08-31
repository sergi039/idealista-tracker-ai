"""A portal's own type word outranks the marketing copy around it.

Two defects measured on production on 2026-08-31, both of them a parser
believing a payload field over the portal's own statement:

* fotocasa put 10 of 15 `/comprar/terreno/` rows into `area_type='built'`,
  because those payloads carry `buildingSubtype: "Residential"` -- fotocasa's
  word for *residential land* -- and the reader never looked at the path. The
  worst of them, property 1336, was a 21,472 m² field stored as a house, which
  is a house passing the owner's "at least 150 m² of house" filter.
* Three of those rows were classified `land` and still kept `area_type='built'`,
  because the portal doors build a `Property` without ever going through
  `apply_classification`, whose land->plot reconciliation would have caught it.

The fixture is the real 40 KB fotocasa payload already committed for
`tests/test_fotocasa_source.py`. The one production shape it does not itself
carry -- a plot whose `buildingSubtype` reads `Residential` -- is produced by
flipping that single field, which is exactly what the live rows differ by:
`surface`, `surfaceLand` and `groundSurface` all carry the same parcel figure
on a real terreno page, so the flipped payload is the live one, field for
field.
"""

import pathlib

import pytest

from app import create_app, db
from models import SearchProfile
from services.fotocasa_source import parse_listing, url_says_plot
from tests import setup_test_environment

FIXTURE = pathlib.Path(__file__).parent / "data" / "fotocasa_listing_190280914.html"

# The live URL shape of the misfiled rows (property 1336 is this path).
PLOT_URL = "https://www.fotocasa.es/es/comprar/terreno/gozon/bocines/190280914/d"
HOUSE_URL = "https://www.fotocasa.es/es/comprar/vivienda/naron/feal/190540646/d"


@pytest.fixture(scope="module")
def land_page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def residential_land_page(land_page: str) -> str:
    """The production shape: a plot the payload calls `Residential`."""
    flipped = land_page.replace(
        '"buildingSubtype":"Land"', '"buildingSubtype":"Residential"'
    )
    assert flipped != land_page, "fixture no longer carries the field being flipped"
    return flipped


def test_a_plot_the_payload_calls_residential_is_still_a_plot(residential_land_page):
    listing = parse_listing(residential_land_page, PLOT_URL)

    assert listing.ok, listing.refusal
    # Before the fix this was "built", and 1945 m² of parcel counted as floor.
    assert listing.area_type == "plot"
    assert listing.area == 1945.0


def test_the_portal_word_is_kept_verbatim_and_not_corrected(residential_land_page):
    """The URL decides the measurement; it must not rewrite the provenance.

    `enrichment.import.building_type` is what fotocasa said. Writing a
    conclusion into it would be the mistake this repository files under
    STATUS-002, one column over.
    """
    listing = parse_listing(residential_land_page, PLOT_URL)

    assert listing.building_type == "Residential"


def test_a_plot_that_says_so_in_the_payload_is_unchanged(land_page):
    listing = parse_listing(land_page, PLOT_URL)

    assert listing.area_type == "plot"
    assert listing.building_type == "Land"


def test_a_dwelling_url_is_left_to_the_payload_and_the_rules(land_page):
    """`vivienda` is fotocasa's catch-all for everything with a roof.

    Reading it as housing would overrule the profile's own rules on rows
    nobody has shown to be wrong -- 9 of the 20 live `/comprar/vivienda/`
    rows are classified `land` from their own text, and this fix does not
    disturb them.
    """
    assert url_says_plot(HOUSE_URL) is False


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.fotocasa.es/es/comprar/terreno/gozon/x/1/d", True),
        # The committed fixture's own canonical URL is the English form; a
        # list that only knew Spanish would put this locale back in the hole.
        ("https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d", True),
        ("https://www.fotocasa.es/ca/comprar/terreny/x/y/1/d", True),
        ("https://www.fotocasa.es/es/comprar/vivienda/naron/x/1/d", False),
        # Another portal's terreno URL is not fotocasa's to read.
        ("https://www.yaencontre.com/venta/terreno/inmueble-1-2", False),
        ("https://www.fotocasa.es/", False),
        (None, False),
    ],
)
def test_url_says_plot_boundaries(url, expected):
    assert url_says_plot(url) is expected


@pytest.fixture
def app():
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
        yield app
        db.drop_all()


# Property 1336's own text, verbatim from production. The title says
# "Residencial", the payload says "Residential", and the advert sells the idea
# of building -- so every text the classifier reads argues for a house, and
# only the URL says otherwise.
LIVE_TITLE = "Residencial en venta en Lugar Susacasa, Bocines - Nembro - Cardo, Gozón"
LIVE_BUILDING_TYPE = "Residential"
LIVE_DESCRIPTION = (
    "¿Imaginas construir la casa de tus sueños donde el verde de la montaña "
    "se cruza con el azul del mar? Finca edificable."
)


def test_a_plot_advertised_as_a_place_to_build_a_house_is_classified_as_land(app):
    """The classifier reads the portal's own word before the sales copy.

    Without it, `\\b(casa|chalet|vivienda|...)\\b` matches "la casa de tus
    sueños" in the description and the row becomes `housing/house` -- which is
    how a 21,472 m² field came to hold the top of the owner's ranking.
    """
    from services.fotocasa_import import classify_row

    row = {
        "title": LIVE_TITLE,
        "building_type": LIVE_BUILDING_TYPE,
        "description": LIVE_DESCRIPTION,
        "url": PLOT_URL,
    }

    category, subtype = classify_row(row, app.config["TEST_PROFILE_ID"])

    assert (category, subtype) == ("land", "plot")


def test_the_same_text_on_a_dwelling_url_is_left_alone(app):
    """The fix must not classify by URL in the other direction.

    `vivienda` carries no plot word, so this row is decided by its text
    exactly as it was before -- the description's "casa" wins, and that is the
    pre-existing behaviour, not something this change chose.
    """
    from services.fotocasa_import import classify_row

    row = {
        "title": LIVE_TITLE,
        "building_type": LIVE_BUILDING_TYPE,
        "description": LIVE_DESCRIPTION,
        "url": HOUSE_URL,
    }

    category, _ = classify_row(row, app.config["TEST_PROFILE_ID"])

    assert category == "housing"


def test_the_builder_reconciles_the_row_it_writes(app):
    """The wiring, not the rule -- a green unit test over a dead hook is this
    repository's own recurring defect (#309).

    The URL here says `vivienda`, so nothing in this row is decided by the
    path: the title alone classifies it `land`, the payload hands over
    `area_type='built'`, and only the builder's reconciliation can make the
    two agree. Rows 1305 and 1320 are live examples of it not happening.
    """
    from services.fotocasa_import import build_property

    prop = build_property(
        {
            "url": HOUSE_URL,
            "listing_id": 190540646,
            "title": "Terreno en venta en Lugar Susacasa, Gozón",
            "price": 54000,
            "area": 2227,
            "area_type": "built",
        },
        profile_id=app.config["TEST_PROFILE_ID"],
    )

    assert prop.property_category == "land"
    assert prop.area_type == "plot"


def test_land_can_never_keep_a_built_area():
    """The reconciliation the portal doors used to skip.

    Live rows 1305 and 1320 were `property_category='land'` holding
    `area_type='built'`, so a parcel was counted as floor space on a row the
    classifier had already called land.
    """
    from models import Property
    from services.property_classification_service import PropertyClassificationService

    prop = Property()
    prop.property_category = "land"
    prop.area = 2227.0
    prop.area_type = "built"

    changed = PropertyClassificationService.reconcile_area_type(prop)

    assert changed is True
    assert prop.area_type == "plot"
