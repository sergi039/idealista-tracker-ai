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
from html import escape as html_escape
from pathlib import Path

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
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
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
        assert listing.photos["published"] == 9
        assert len(listing.photos["items"]) == 9
        assert listing.photos["items"][0] == {"url": FOTOCASA_FIRST, "type": "image"}

    def test_the_agency_logo_is_refused_by_path(self):
        """A guard that has NOT been observed to fire on real data, tested
        directly because of that. Measured on the committed fixture: the logo
        sits at `publisher.logo` / `agency.logo` / `promotionLogo` and zero of
        the nine `multimedia` entries carry `/images/client/` — so the earlier
        version of this file, which asserted the logo's absence from the parsed
        result, passed whether or not the filter existed. Removing the filter
        left all 26 tests green. Fotocasa serves both from ONE host, so the
        guard stays; it is worth exactly what a test that can fail is worth."""
        estate = {
            "multimedia": [
                {"src": FOTOCASA_FIRST, "type": "image"},
                {
                    "src": "https://static.fotocasa.es/images/client/9202765912278/"
                    "632581-20201218101205.jpg?rule=original",
                    "type": "image",
                },
            ]
        }

        capture = portal_photos.from_fotocasa_payload(estate, None)

        assert [photo["url"] for photo in capture["items"]] == [FOTOCASA_FIRST]
        assert not any(
            FOTOCASA_AGENCY_LOGO_PATH in photo["url"] for photo in capture["items"]
        )
        # The payload NAMED two: one captured, one refused. A row like this
        # must never read as "the portal published none".
        assert capture["published"] == 2

    def test_milanuncios_eight_photos(self):
        row = milanuncios_source.parse_listing(
            _fixture(MILANUNCIOS_FIXTURE),
            "https://www.milanuncios.com/venta-de-casas/x-612329827.htm",
        )

        assert row["status"] == "new"
        assert row["photos"]["published"] == 8
        assert len(row["photos"]["items"]) == 8
        assert row["photos"]["items"][0] == {"url": MILANUNCIOS_FIRST}

    def test_yaencontre_every_card_carries_one_and_no_chrome(self):
        cards = yaencontre_source.cards_in_email(_fixture(YAENCONTRE_FIXTURE))

        assert len(cards) == 10
        assert all(card.photos["items"] for card in cards), (
            "this email is the ONLY source for these rows and every card has a photo"
        )
        assert cards[0].photos == {"items": [{"url": YAENCONTRE_FIRST}], "published": 1}
        captured = [photo["url"] for card in cards for photo in card.photos["items"]]
        assert not any("static-mail.yaencontre.com" in url for url in captured)
        assert not any("apicondor.yaencontre.com" in url for url in captured)


class TestWhatMayBeCaptured:
    @pytest.mark.parametrize(
        "url, why",
        [
            ("https://media.example.com/p.jpg?apikey=SECRET", "apikey"),
            ("https://media.example.com/p.jpg?token=SECRET", "token"),
            ("https://media.example.com/p.jpg?signature=abc", "signature"),
            ("https://media.example.com/p.jpg?sessionid=abc", "an unlisted name"),
            ("https://user:pass@media.example.com/p.jpg", "userinfo"),
            (
                "https://media.yaencontre.com@evil.example.com/p.jpg",
                "the host is evil.example.com; the trusted name is userinfo",
            ),
            ("https://media.example.com/p.jpg#access_token=abc", "the fragment"),
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

    @pytest.mark.parametrize(
        "url", [FOTOCASA_FIRST, MILANUNCIOS_FIRST, YAENCONTRE_FIRST]
    )
    def test_the_guard_never_refuses_a_real_portal_photograph(self, url):
        """The credential check matches parameter names by SUBSTRING, so it is
        deliberately broad. This is what stops a future tightening from
        quietly capturing nothing: fotocasa's only parameter is
        `rule=original`, and the other two carry none."""
        assert portal_photos.normalise_photo_url(url) == url


class TestTheTwoFactsTheBadgeCouldNotTellApart:
    def test_a_read_payload_that_named_none_is_a_measurement(self, app, profile):
        row = _prop(
            profile,
            "none-published",
            enrichment={"import": {"photos": {"items": [], "published": 0}}},
        )

        assert portal_photos.read_photos(row) == {
            "state": "none_published",
            "photos": [],
            "count": 0,
        }

    def test_refused_entries_are_not_a_portal_with_no_pictures(self, app, profile):
        """The independent review's finding, reproduced then fixed: a payload
        naming eight photographs of which the guard refuses all eight leaves an
        empty list, and an empty list on its own is indistinguishable from a
        portal that published none -- a refusal reported as a measurement,
        which is #98 one layer inside the module written to avoid it."""
        capture = portal_photos.from_milanuncios_ad(
            {"images": ["https://cdn.example.com/x.jpg?signature=dummy"]}
        )
        assert capture == {"items": [], "published": 1}

        row = _prop(profile, "all-refused", enrichment={"import": {"photos": capture}})

        assert portal_photos.read_photos(row)["state"] == "not_captured"

    def test_an_unreadable_count_is_unknown_and_never_none_published(
        self, app, profile
    ):
        """A malformed `published` must not read as "the portal published
        none", and must not throw away photographs that ARE readable. The
        first version refused the whole block, which did both wrongs at once
        and was invisible: a mutation removing it left every test green."""
        empty = _prop(
            profile,
            "bad-count-empty",
            enrichment={"import": {"photos": {"items": [], "published": "nine"}}},
        )
        assert portal_photos.read_photos(empty)["state"] == "not_captured"

        readable = _prop(
            profile,
            "bad-count-readable",
            enrichment={
                "import": {
                    "photos": {
                        "items": [{"url": FOTOCASA_FIRST}],
                        "published": "nine",
                    }
                }
            },
        )
        reading = portal_photos.read_photos(readable)
        assert reading["state"] == "captured"
        assert [photo["url"] for photo in reading["photos"]] == [FOTOCASA_FIRST]

    def test_an_entry_the_payload_named_but_nobody_can_read_still_counts(self):
        """Otherwise a payload of nothing but junk entries reports
        `published: 0` and the row reads as a portal with no pictures."""
        capture = portal_photos.from_fotocasa_payload(
            {"multimedia": ["not-a-dict", 42, None]}, None
        )

        assert capture == {"items": [], "published": 3}

    def test_no_key_at_all_means_nobody_looked(self, app, profile):
        row = _prop(profile, "not-captured", enrichment={"import": {"source": "x"}})

        assert portal_photos.read_photos(row)["state"] == "not_captured"

    def test_captured_reads_back_by_value(self, app, profile):
        row = _prop(
            profile,
            "captured",
            enrichment={
                "import": {
                    "photos": {
                        "items": [{"url": FOTOCASA_FIRST, "type": "image"}],
                        "published": 1,
                    }
                }
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
            (
                {"photos": {"items": [{"url": "javascript:x"}], "published": 1}},
                "refused",
            ),
            ({"photos": {"items": [], "published": "nine"}}, "a non-integer count"),
            ({"photos": {"items": [], "published": -1}}, "a negative count"),
            (
                {"photos": {"items": [{"url": "javascript:x"}], "published": 0}},
                "a count that disagrees with its own refused entry",
            ),
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
        assert len(row["photos"]["items"]) == 9
        assert json.loads(json.dumps(row["photos"])) == row["photos"]

        prop = fotocasa_import.build_property(
            row, source="fotocasa", method="paste", profile_id=profile.id
        )

        stored = prop.enrichment["import"]["photos"]
        assert stored["published"] == 9
        assert len(stored["items"]) == 9
        assert stored["items"][0]["url"] == FOTOCASA_FIRST

    def test_a_row_with_no_photos_key_leaves_the_block_without_one(self, app, profile):
        prop = fotocasa_import.build_property(
            {"url": "https://www.fotocasa.es/x/1/d", "listing_id": 1, "title": "x"},
            source="fotocasa",
            method="paste",
            profile_id=profile.id,
        )

        assert "photos" not in prop.enrichment["import"]


class TestRendering:
    """The photographs on screen. The model never sees them — the subscription
    bridge is text-only — so this IS the feature: 711 rows carry no description
    at all, and for those a picture is the only judgeable thing the page has."""

    def _with_photos(self, profile, slug, urls):
        return _prop(
            profile,
            slug,
            enrichment={
                "import": {
                    "photos": {
                        "items": [{"url": url} for url in urls],
                        "published": len(urls),
                    }
                }
            },
        )

    def test_the_property_page_renders_every_captured_photograph(
        self, app, client, profile
    ):
        row = self._with_photos(
            profile, "strip", [FOTOCASA_FIRST, YAENCONTRE_FIRST, MILANUNCIOS_FIRST]
        )

        body = client.get(f"/properties/{row.id}").get_data(as_text=True)

        assert "Casa en Malpica" in body, "the page did not render"
        for url in (FOTOCASA_FIRST, YAENCONTRE_FIRST, MILANUNCIOS_FIRST):
            assert f'src="{html_escape(url)}"' in body, url
        # Served from a third-party CDN: the portal is not told which listing
        # is being looked at.
        assert body.count('referrerpolicy="no-referrer"') >= 3
        # And NOT lazily, unlike the list. Measured in a real browser: a lazy
        # image in this strip sometimes never starts its request at all
        # (`currentSrc` empty, `complete` false) while the same URL fetched in
        # 316 ms through `new Image()` -- and an image whose request never
        # starts also never fires `onerror`, so the "photo gone" note stayed
        # hidden too. A handful of photographs on a page somebody opened in
        # order to look at them is not worth that.
        strip = body[body.index('class="listing-photos') :]
        assert 'loading="lazy"' not in strip[: strip.index("</div>")]

    def test_every_surface_says_a_photograph_is_gone_rather_than_nothing(
        self, app, client, profile
    ):
        """An adversarial review's finding: the strip said "photo gone" and the
        list and the cards said nothing, so a rotted URL was indistinguishable
        from a listing whose portal published none — the pair this module
        exists to keep apart, thrown away by the templates."""
        self._with_photos(profile, "rot-everywhere", [FOTOCASA_FIRST])

        for url, marker in (
            ("/properties?view_type=list", "listing-photo-missing"),
            ("/properties?view_type=cards", "listing-card-photo-missing"),
        ):
            body = client.get(url).get_data(as_text=True)
            assert "properties found" in body, url
            assert marker in body, url
            assert "photo gone" in body.lower(), url

    def test_the_tooltip_does_not_contradict_the_photographs_under_it(
        self, app, client, profile
    ):
        """It read "They are not shown here" — directly above the strip showing
        them. The badge's meaning changed and its tooltip did not."""
        row = self._with_photos(profile, "tooltip", [FOTOCASA_FIRST])

        body = client.get(f"/properties/{row.id}").get_data(as_text=True)

        assert "Casa en Malpica" in body, "the page did not render"
        assert "not shown here" not in body

    def test_a_rotted_url_reads_as_a_gone_photograph_not_as_none(
        self, app, client, profile
    ):
        row = self._with_photos(profile, "rotted", [FOTOCASA_FIRST])

        body = client.get(f"/properties/{row.id}").get_data(as_text=True)

        assert "onerror=" in body, "a dead URL would leave a broken image"
        assert "photo gone" in body.lower()

    def test_the_list_shows_the_first_photograph_and_links_to_the_row(
        self, app, client, profile
    ):
        row = self._with_photos(profile, "listed", [FOTOCASA_FIRST, YAENCONTRE_FIRST])

        body = client.get("/properties?view_type=list").get_data(as_text=True)

        assert "properties found" in body, "the list did not render"
        assert f'src="{html_escape(FOTOCASA_FIRST)}"' in body
        # The FIRST one only: a table row is one line, not a gallery.
        assert html_escape(YAENCONTRE_FIRST) not in body
        assert f'/properties/{row.id}"' in body

    def test_the_cards_view_shows_it_too(self, app, client, profile):
        self._with_photos(profile, "carded", [FOTOCASA_FIRST])

        body = client.get("/properties?view_type=cards").get_data(as_text=True)

        assert "properties found" in body, "the cards did not render"
        assert "listing-card-photo" in body
        assert f'src="{html_escape(FOTOCASA_FIRST)}"' in body
        # Not lazily, for the strip's reason: a lazy image that never starts
        # its request never fires `onerror` either, and a dead URL then sits
        # as a grey 390x160 block for good. The table's 64x48 thumbnail keeps
        # `lazy` -- 25 of them, and its worst case is a grey square.
        card = body[body.index("listing-card-photo-link") :]
        assert 'loading="lazy"' not in card[: card.index("</a>")]

    def test_a_row_with_no_photograph_renders_no_placeholder(
        self, app, client, profile
    ):
        """Most of the table has simply never been looked at, so an empty cell
        is the honest rendering — a placeholder would assert there are none."""
        _prop(profile, "bare", enrichment={"import": {"source": "idealista"}})

        body = client.get("/properties?view_type=list").get_data(as_text=True)

        assert "properties found" in body
        assert "listing-thumb" not in body

    def test_the_page_still_has_exactly_one_inline_script_element(self):
        """`tests/test_issue_23_xss_and_prompt_injection.py` finds it by
        searching for the literal opening tag, so neither a second element nor
        that tag spelled out in a comment may appear above it — which is why
        the photo strip uses an inline `onerror` and its comment does not
        write the tag out."""
        markup = (TEMPLATES / "property_detail.html").read_text(encoding="utf-8")

        first = markup.index("<script>")
        # Everything before the real element must be free of the literal tag.
        assert "<script" not in markup[:first]


class TestThePage:
    @pytest.mark.parametrize(
        "enrichment, expect, forbid",
        [
            (
                {
                    "import": {
                        "photos": {
                            "items": [{"url": FOTOCASA_FIRST}],
                            "published": 1,
                        }
                    }
                },
                "1 photo",
                "No photos",
            ),
            (
                {"import": {"photos": {"items": [], "published": 0}}},
                "No photos",
                "not captured",
            ),
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
