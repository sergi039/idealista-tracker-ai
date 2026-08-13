"""Fixtures for the display-side description cleanup (proposal D13).

Every SAMPLE below is the opening of a real stored description (looked up in
the owner's database on 2026-08-13, truncated the way the parser stored it).
The contract: recognized boilerplate goes, the listing's own words stay, the
raw text is never modified in place, and an unrecognized opening passes
through byte-for-byte — a cleanup that guesses is worse than none.
"""

from utils.description_display import clean_description_for_display

NEW_LISTING = (
    "Hello Sergioalicante, 1 new listing that matches your search criteria "
    "68,000 € 1,930 m² urban Building plot in Molleda Corvera de Asturias"
)
NEW_LISTING_HOUSE = (
    "Hello Sergioalicante, 1 new listing that matches your search criteria "
    "89,000 € 234 €/m² 4 bed. 380 m2 Stone house located in a quiet area"
)
PRICE_DROP = (
    "Hello Sergioalicante, The price of this listing has dropped from "
    "104,000€ to 94,000€ 104,000€ ↓10% 94,000 € 783 €/m² 3 bed. 120 m2"
)
PRICE_DROP_WITH_TEXT = (
    "Hello Sergioalicante, The price of this listing has dropped from "
    "260,000€ to 220,000€ 260,000€ ↓15% 220,000 € 128 m² 3 bed Looking for "
    "a home near the coast"
)


class TestRecognizedBoilerplate:
    def test_new_listing_alert_keeps_only_the_listing_text(self):
        result = clean_description_for_display(NEW_LISTING)
        assert result["stripped"] is True
        # Numeric tokens go; the words stay, even lowercase "urban".
        assert result["text"] == "urban Building plot in Molleda Corvera de Asturias"

    def test_price_and_bed_tokens_are_eaten_as_a_run(self):
        result = clean_description_for_display(NEW_LISTING_HOUSE)
        assert result["text"] == "Stone house located in a quiet area"

    def test_price_drop_alert_with_trailing_text(self):
        result = clean_description_for_display(PRICE_DROP_WITH_TEXT)
        assert result["stripped"] is True
        assert result["text"] == "Looking for a home near the coast"

    def test_all_boilerplate_keeps_the_original_not_blank(self):
        # This alert is nothing but salutation + figures; blanking the card
        # would hide that the email said anything at all.
        result = clean_description_for_display(PRICE_DROP)
        assert result["stripped"] is False
        assert result["text"] == PRICE_DROP


class TestUnrecognizedTextPassesThrough:
    def test_plain_description_untouched(self):
        text = "Beautiful stone house with 1,020 m² plot near the sea."
        result = clean_description_for_display(text)
        assert result == {"text": text, "stripped": False}

    def test_leading_figures_without_an_alert_opening_stay(self):
        # Diff-review finding (2026-08-13): the token loop must be licensed
        # by a recognized opener, or a listing's own leading figure is eaten.
        for text in (
            "320 m2 plot with amazing views",
            "99,000 € negotiable. Sale of country estate",
            "20 minutes from Oviedo, this farm has everything",
        ):
            assert clean_description_for_display(text) == {
                "text": text,
                "stripped": False,
            }

    def test_tokens_never_bite_into_words(self):
        # "3 bed" must not match inside "3 bedroom", "20 m" not inside
        # "20 minutes" — the (?=\s|$) boundary pins it (diff review).
        text = (
            "Hello Sergioalicante, 1 new listing that matches your search "
            "criteria 89,000 € 234 €/m² 4 bed. 380 m2 3 bedroom stone house "
            "with garden"
        )
        result = clean_description_for_display(text)
        assert result["stripped"] is True
        assert result["text"] == "3 bedroom stone house with garden"

    def test_greeting_alone_is_not_boilerplate(self):
        # "Hello" without the alert's `<name>,` shape stays.
        text = "Hello and welcome to this unique property in Navia."
        result = clean_description_for_display(text)
        assert result["stripped"] is False
        assert result["text"] == text

    def test_none_and_empty_are_explicit(self):
        assert clean_description_for_display(None) == {"text": "", "stripped": False}
        assert clean_description_for_display("   ") == {
            "text": "   ",
            "stripped": False,
        }
