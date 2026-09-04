"""The portal's own photographs, captured on the way past (#498 follow-up).

The owner asked for a system that learns from their comments about photographs.
It could not: measured on production 2026-09-04, of 1893 rows exactly one row's
`enrichment` and four rows' `attributes` so much as mentioned an image, while
`templates/property_detail.html` asserted "No photos" on EVERY listing page
unconditionally -- an absence rendered as a measurement (#98), on the one datum
the owner was being asked to judge by.

The URLs were already in memory and were being dropped. What this file pins,
each against the REAL committed fixture rather than a hand-written dict, because
the whole defect was a payload key nobody read:

* fotocasa's nine, by value, with the agency logo -- same host, `/images/client/`
  instead of `/images/ads/` -- refused;
* milanuncios' eight, by value;
* yaencontre's card photograph, on all ten cards, with the 13 mail-chrome images
  and the tracking pixel refused. That portal matters most here: 550 production
  rows, only 26 with any description, so a picture is the only thing that can be
  judged -- and its email carries one for every card;
* a URL carrying a credential is refused outright, never stripped: that tracking
  pixel's query is `apikey=...`, and a captured URL is persisted and rendered in
  an href;
* the two facts the badge could not tell apart -- the portal published none
  (an empty list, a measurement) against nobody captured any (no key at all);
* and the reader is fail-closed against a block written straight into the
  database, which is a supported workflow here.
"""

import json
import re

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import (
    fotocasa_import,
    fotocasa_source,
    milanuncios_source,
    portal_photos,
    yaencontre_source,
)
from tests import setup_test_environment

FOTOCASA_FIXTURE = "tests/data/fotocasa_listing_190280914.html"
MILANUNCIOS_FIXTURE = "tests/data/milanuncios_listing_612329827.html"
YAENCONTRE_FIXTURE = "tests/data/yaencontre_alert_boiro.html"

# Read off the committed fixtures, by hand, once.
FOTOCASA_FIRST = (
    "https://static.fotocasa.es/images/ads/"
    "1a882a2d-f7b9-4e35-9567-11b6936a58b1?rule=original"
)
FOTOCASA_AGENCY_LOGO_PATH = "/images/client/"
MILANUNCIOS_FIRST = (
    "https://images.milanuncios.com/api/v1/ma-ad-media-pro/images/"
    "b22fc08c-e4cf-4f81-af36-54b9a0a3c764"
)
YAENCONTRE_FIRST = (
    "https://media.yaencontre.com/img/photo/w630/75866/75866-57436479-1515006322.jpg"
)


def _fixture(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile(app):
    row = SearchProfile(name="Galicia · costa", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _prop(profile, slug, **kwargs):
    row = Property(
        source_email_id=slug,
        title=kwargs.pop("title", "Casa en Malpica"),
        municipality=kwargs.pop("municipality", "Malpica de Bergantiños"),
        search_profile_id=profile.id,
        **kwargs,
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestCaptureFromTheRealPayloads:
    def test_fotocasa_nine_photos_and_never_the_agency_logo(self):
        listing = fotocasa_source.parse_listing(
            _fixture(FOTOCASA_FIXTURE),
            "https://www.fotocasa.es/es/comprar/casa/aviles/x/190280914/d",
        )

        assert listing.ok
        assert len(listing.photos) == 9
        assert listing.photos[0] == {"url": FOTOCASA_FIRST, "type": "image"}
        # Same host, different path segment: the agency's logo, not the plot.
        assert not any(
            FOTOCASA_AGENCY_LOGO_PATH in photo["url"] for photo in listing.photos
        )

    def test_milanuncios_eight_photos(self):
        row = milanuncios_source.parse_listing(
            _fixture(MILANUNCIOS_FIXTURE),
            "https://www.milanuncios.com/venta-de-casas/x-612329827.htm",
        )

        assert row["status"] == "new"
        assert len(row["photos"]) == 8
        assert row["photos"][0] == {"url": MILANUNCIOS_FIRST}

    def test_yaencontre_every_card_carries_one_and_no_chrome(self):
        cards = yaencontre_source.cards_in_email(_fixture(YAENCONTRE_FIXTURE))

        assert len(cards) == 10
        assert all(card.photos for card in cards), (
            "this email is the ONLY source for these rows and every card has a photo"
        )
        assert cards[0].photos == [{"url": YAENCONTRE_FIRST}]
        captured = [photo["url"] for card in cards for photo in card.photos]
        assert not any("static-mail.yaencontre.com" in url for url in captured)
        assert not any("apicondor.yaencontre.com" in url for url in captured)


class TestWhatMayBeCaptured:
    @pytest.mark.parametrize(
        "url, why",
        [
            ("https://media.example.com/p.jpg?apikey=SECRET", "apikey"),
            ("https://media.example.com/p.jpg?token=SECRET", "token"),
            ("https://media.example.com/p.jpg?signature=abc", "signature"),
        ],
    )
    def test_a_url_carrying_a_credential_is_refused_not_stripped(self, url, why):
        assert portal_photos.normalise_photo_url(url) is None, why

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:image/png;base64,AAAA",
            "/relative/photo.jpg",
            "",
            None,
            42,
        ],
    )
    def test_only_an_http_url_with_a_host_may_be_stored(self, url):
        assert portal_photos.normalise_photo_url(url) is None

    def test_an_ordinary_photo_url_passes(self):
        url = "https://media.example.com/img/photo/w630/1/2-3.jpg"
        assert portal_photos.normalise_photo_url(url) == url


class TestTheTwoFactsTheBadgeCouldNotTellApart:
    def test_a_read_payload_that_named_none_is_a_measurement(self, app, profile):
        row = _prop(profile, "none-published", enrichment={"import": {"photos": []}})

        assert portal_photos.read_photos(row) == {
            "state": "none_published",
            "photos": [],
            "count": 0,
        }

    def test_no_key_at_all_means_nobody_looked(self, app, profile):
        row = _prop(profile, "not-captured", enrichment={"import": {"source": "x"}})

        assert portal_photos.read_photos(row)["state"] == "not_captured"

    def test_captured_reads_back_by_value(self, app, profile):
        row = _prop(
            profile,
            "captured",
            enrichment={
                "import": {"photos": [{"url": FOTOCASA_FIRST, "type": "image"}]}
            },
        )

        reading = portal_photos.read_photos(row)

        assert reading["count"] == 1
        assert reading["state"] == "captured"
        assert reading["photos"] == [{"url": FOTOCASA_FIRST, "type": "image"}]

    @pytest.mark.parametrize(
        "stored, why",
        [
            ({"photos": "nine"}, "a string is not a list of photographs"),
            ({"photos": [{"url": "javascript:alert(1)"}]}, "never reaches an href"),
            ({"photos": [{"url": None}]}, "a null url"),
            ({"photos": ["https://media.example.com/x.jpg"]}, "bare strings"),
        ],
    )
    def test_a_block_written_by_hand_cannot_force_a_claim(
        self, app, profile, stored, why
    ):
        """Direct SQL is a supported workflow here, so the reader is the guard."""
        row = _prop(
            profile, f"handwritten-{abs(hash(why))}", enrichment={"import": stored}
        )

        reading = portal_photos.read_photos(row)

        assert reading["photos"] == [], why
        # Refused entries are NOT the portal saying it published none.
        assert reading["state"] == "not_captured", why


class TestTheWriter:
    def test_build_property_carries_the_photos_into_the_import_block(
        self, app, profile
    ):
        listing = fotocasa_source.parse_listing(
            _fixture(FOTOCASA_FIXTURE),
            "https://www.fotocasa.es/es/comprar/casa/aviles/x/190280914/d",
        )
        row = fotocasa_import.preview_row(listing)

        # preview_row is the layer a background job persists; a field that
        # stops here is dropped as surely as one never read.
        assert len(row["photos"]) == 9
        assert json.loads(json.dumps(row["photos"])) == row["photos"]

        prop = fotocasa_import.build_property(
            row, source="fotocasa", method="paste", profile_id=profile.id
        )

        assert len(prop.enrichment["import"]["photos"]) == 9
        assert prop.enrichment["import"]["photos"][0]["url"] == FOTOCASA_FIRST

    def test_a_row_with_no_photos_key_leaves_the_block_without_one(self, app, profile):
        prop = fotocasa_import.build_property(
            {"url": "https://www.fotocasa.es/x/1/d", "listing_id": 1, "title": "x"},
            source="fotocasa",
            method="paste",
            profile_id=profile.id,
        )

        assert "photos" not in prop.enrichment["import"]


class TestThePage:
    @pytest.mark.parametrize(
        "enrichment, expect, forbid",
        [
            (
                {"import": {"photos": [{"url": FOTOCASA_FIRST}]}},
                "1 photos",
                "No photos",
            ),
            ({"import": {"photos": []}}, "No photos", "not captured"),
            ({"import": {"source": "idealista"}}, "not captured", "No photos"),
            (None, "not captured", "No photos"),
        ],
    )
    def test_the_badge_says_which_of_the_three_it_is(
        self, app, client, profile, enrichment, expect, forbid
    ):
        row = _prop(
            profile, f"page-{abs(hash(expect + forbid))}", enrichment=enrichment
        )

        response = client.get(f"/properties/{row.id}")

        # A template error becomes a redirect and a re-render with a flash
        # (routes/main_routes.py), which would also "not contain" a string --
        # so assert the page rendered before asserting what it says.
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Casa en Malpica" in body, "the page did not render"
        assert expect.lower() in body.lower()
        assert not re.search(rf">\s*{re.escape(forbid)}\s*<", body, re.IGNORECASE)
