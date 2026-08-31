"""Two shapes of real milanuncios alert that the reader used to refuse whole.

Measured on production 2026-08-31. Two alert emails were consumed producing
zero rows, and the UID watermark moved permanently past both, so the ads they
carried are gone until a parser fix and a full sync:

* UID 489469 carried four ads (99,000 EUR in las Vegas, 90,000 in Colunga,
  110,000 in Siero, 36,000 in Llanera). Its card anchors point at
  `sgt.milanuncios.com/uni/ls/click`, and the tracker pattern was anchored on
  `/ls/click`, so not one anchor was recognised.
* UID 489549 carried an ad with no photograph. A card was recognised only by
  the `images*.milanuncios.com` photo it wrapped; a photo-less ad renders a
  `cdn.braze.eu` placeholder instead, so the email read as cardless. That is
  the structural half: every photo-less ad drops, and a cheap private-seller
  plot is exactly the shape that arrives without photographs.

Neither email survived -- the container was rebuilt and its logs rotated
before they could be committed as fixtures. So the photo-less case is
reproduced by taking a real committed digest and swapping only the image host,
which is the one attribute the two differ by, and the `/uni/` case is asserted
against the pattern directly. Both are stated here rather than implied,
because a fixture that steps around the defect is this repository's own
recurring way of shipping a test that cannot fail.
"""

import pathlib

import pytest

from services.milanuncios_source import _TRACKER, card_tracker_urls

DATA = pathlib.Path(__file__).parent / "data"
SOLARES = DATA / "milanuncios_alert_solares.html"
CHALETS = DATA / "milanuncios_alert_chalets.html"


@pytest.fixture(scope="module")
def solares() -> str:
    return SOLARES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chalets() -> str:
    return CHALETS.read_text(encoding="utf-8")


def test_the_real_digests_are_read_exactly_as_before(solares, chalets):
    """The widened rules must not move a card that is already found."""
    assert len(card_tracker_urls(solares)) == 3
    assert len(card_tracker_urls(chalets)) == 2


def test_an_ad_without_a_photograph_is_still_a_card(solares):
    photo_less = solares.replace("images-re.milanuncios.com", "cdn.braze.eu")
    assert photo_less != solares, "fixture no longer carries the photo host"

    # Before the fix this returned [] and the whole email was consumed.
    assert len(card_tracker_urls(photo_less)) == 3


def test_the_management_buttons_are_still_left_alone(solares):
    """The footer and the alert controls sit on the same tracker host.

    `..._solares.html` holds 20 tracker anchors; only 3 are cards. A rule that
    took the rest would resolve them, and each resolve is a request.
    """
    cards = card_tracker_urls(solares)

    assert len(cards) == 3
    assert len(set(cards)) == 3


@pytest.mark.parametrize(
    "href,expected",
    [
        ("http://sgt.milanuncios.com/ls/click?upn=u001.x", True),
        # The shape that lost four ads.
        ("http://sgt.milanuncios.com/uni/ls/click?upn=u001.x", True),
        ("https://sgt.milanuncios.com/a/b/ls/click?upn=u001.x", True),
        # The host is the guarantee and stays anchored.
        ("http://evil.example.com/ls/click?upn=u001.x", False),
        ("http://sgt.milanuncios.example.com/ls/click?upn=u001.x", False),
        ("http://sgt.milanuncios.com/ls/click", False),
    ],
)
def test_the_tracker_pattern_reads_every_prefix_and_only_this_host(href, expected):
    assert bool(_TRACKER.match(href)) is expected


def test_a_card_is_taken_once_even_if_the_template_marks_it_twice(solares):
    """The photo anchor and its "Ver más fotos" twin resolve to one ad.

    Each resolve is a request, so a template that ever carried both marks on
    one href must not double the traffic.
    """
    doubled = solares.replace(
        'title="ver el resultado de la búsqueda"',
        'title="ver el resultado de la búsqueda" data-x="1"',
    )

    assert len(card_tracker_urls(doubled)) == 3
