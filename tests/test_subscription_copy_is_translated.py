"""The subscription copy speaks the language the rest of the page speaks (#409).

Every disclosure around the subscription controls used to be written in
English in the template, under headings that *were* translated -- so the
Spanish UI rendered "Suscripciones" over "2 hidden subscriptions (2 listings)
not shown", and `/profiles` was English top to bottom while the navbar leading
to it was not.

Two things are pinned here.

**Both languages, on the pages that carry the copy.** A test that only checks
the Spanish string would pass on a template that hardcoded the Spanish one, so
each assertion has an English half.

**The counted phrases.** `tn()` picks `_one` or `_other`, which is the whole
reason it exists: Spanish inflects the adjective along with the noun --
"1 suscripción oculta" against "2 suscripciones ocultas" -- so a single string
with a number substituted into it is wrong in one of the two cases whatever it
says.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from tests import setup_test_environment
from utils.i18n import TRANSLATIONS, tn


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _in(client, lang):
    with client.session_transaction() as session:
        session["language"] = lang


@pytest.fixture
def subscriptions(app):
    """One visible subscription, two hidden ones, and an unassigned listing."""
    with app.app_context():
        live = SearchProfile(name="Land at Norte", is_active=True)
        hidden_a = SearchProfile(name="Solares Norte", is_active=True, is_hidden=True)
        hidden_b = SearchProfile(name="Quesada", is_active=False, is_hidden=True)
        db.session.add_all([live, hidden_a, hidden_b])
        db.session.commit()

        for slug, profile_id in (
            ("live", live.id),
            ("hiddena", hidden_a.id),
            ("hiddenb", hidden_b.id),
            ("orphan", None),
        ):
            db.session.add(
                Property(
                    source_email_id=f"i18n_{slug}",
                    title=f"{slug}UniqueTitle",
                    search_profile_id=profile_id,
                    listing_status="active",
                    location_lat=43.56,
                    location_lon=-6.14,
                )
            )
        db.session.commit()
        return {"live": live.id, "hidden_a": hidden_a.id, "hidden_b": hidden_b.id}


class TestTheCountedPhrases:
    @pytest.mark.parametrize(
        "lang,count,expected",
        [
            ("en", 1, "1 hidden subscription"),
            ("en", 2, "2 hidden subscriptions"),
            ("es", 1, "1 suscripción oculta"),
            ("es", 2, "2 suscripciones ocultas"),
        ],
    )
    def test_one_and_other(self, lang, count, expected):
        assert tn("hidden_subscriptions_count", count, lang=lang) == expected

    @pytest.mark.parametrize(
        "lang,count,expected",
        [
            ("en", 1, "1 listing"),
            ("en", 3, "3 listings"),
            ("es", 1, "1 anuncio"),
            ("es", 3, "3 anuncios"),
        ],
    )
    def test_listings(self, lang, count, expected):
        assert tn("listings_count", count, lang=lang) == expected

    def test_zero_takes_the_other_form(self):
        """English and Spanish both say "0 listings", not "0 listing"."""
        assert tn("listings_count", 0, lang="en") == "0 listings"
        assert tn("listings_count", 0, lang="es") == "0 anuncios"

    def test_a_missing_pair_falls_back_rather_than_raising(self):
        assert tn("no_such_counted_key", 2, lang="es") == "no_such_counted_key_other"


class TestThePropertiesPage:
    def test_the_hidden_note_is_translated(self, client, subscriptions):
        _in(client, "es")
        body = client.get("/properties").get_data(as_text=True)
        assert "Sin mostrar" in body
        assert "2 suscripciones ocultas" in body
        assert "2 anuncios" in body
        assert "hidden subscription" not in body, "no English left in that line"

        _in(client, "en")
        body = client.get("/properties").get_data(as_text=True)
        assert "2 hidden subscriptions" in body
        assert "2 listings" in body

    def test_the_unassigned_disclosure_is_translated(self, client, subscriptions):
        _in(client, "es")
        body = client.get("/properties").get_data(as_text=True)
        assert "1 anuncio sin suscripción sin mostrar" in body
        assert "mostrarlos" in body
        assert "with no subscription" not in body

        _in(client, "en")
        body = client.get("/properties").get_data(as_text=True)
        assert "1 listing with no subscription not shown" in body
        assert "show them" in body

    def test_the_no_subscription_option_is_translated(self, client, subscriptions):
        _in(client, "es")
        body = client.get("/properties").get_data(as_text=True)
        assert "Sin suscripción" in body
        assert "No subscription" not in body

    def test_a_selected_hidden_subscription_is_badged_in_spanish(
        self, client, subscriptions
    ):
        _in(client, "es")
        body = client.get(
            "/properties?profile_id={}".format(subscriptions["hidden_a"])
        ).get_data(as_text=True)
        assert "Oculta" in body
        assert ">Hidden<" not in body


class TestTheMap:
    def test_its_note_is_translated_too(self, client, subscriptions):
        _in(client, "es")
        body = client.get("/map").get_data(as_text=True)
        assert "Sin mostrar" in body
        assert "suscripciones ocultas" in body
        assert "hidden subscription" not in body


class TestTheProfilesPage:
    def test_the_whole_page_is_translated(self, client, subscriptions):
        _in(client, "es")
        body = client.get("/profiles").get_data(as_text=True)

        for spanish in (
            "Perfiles de búsqueda",
            "Nuevo perfil",
            "Nombre",
            "Predeterminado",
            "Visible",
            "Actualizado",
            "Ocultar",
            "Editar",
            "Sin suscripción",
            "Ocultar afecta a la pantalla",
        ):
            assert spanish in body, f"missing: {spanish}"

        for english in (
            "Search Profiles",
            "New profile",
            "No profiles yet",
            "Hidden is about the screen",
            ">Hide<",
        ):
            assert english not in body, f"still English: {english}"

    def test_the_show_button_is_translated(self, client, subscriptions):
        _in(client, "es")
        body = client.get("/profiles").get_data(as_text=True)
        assert "Mostrar" in body, "a hidden row offers Show"

    def test_english_still_reads_as_before(self, client, subscriptions):
        _in(client, "en")
        body = client.get("/profiles").get_data(as_text=True)
        for english in (
            "Search Profiles",
            "Visible",
            "Hide",
            "Edit",
            "No subscription",
        ):
            assert english in body, f"missing: {english}"


class TestTheFlashMessages:
    def test_hiding_and_the_refusal_speak_spanish(self, app, client, subscriptions):
        _in(client, "es")
        body = client.post(
            "/profiles/{}/visibility".format(subscriptions["live"]),
            data={"hidden": "on"},
            follow_redirects=True,
        ).get_data(as_text=True)
        assert "queda oculta en las vistas de anuncios" in body
        assert "is hidden from the property views" not in body

    def test_the_catch_all_refusal_is_translated(self, app, client):
        with app.app_context():
            catch_all = SearchProfile(name="Default", is_active=True, is_default=True)
            db.session.add(catch_all)
            db.session.commit()
            catch_all_id = catch_all.id

        _in(client, "es")
        body = client.post(
            f"/profiles/{catch_all_id}/visibility",
            data={"hidden": "on"},
            follow_redirects=True,
        ).get_data(as_text=True)
        assert "no se puede ocultar" in body
        assert "cannot be hidden" not in body


class TestNoKeyIsMissingFromEitherLanguage:
    def test_every_english_key_has_a_spanish_one(self):
        """A key present in one language only renders English to a Spanish
        reader, silently -- `t()` falls back rather than failing, which is
        right at runtime and useless as a signal."""
        missing = sorted(set(TRANSLATIONS["en"]) - set(TRANSLATIONS["es"]))
        assert missing == [], f"no Spanish for: {missing}"

    def test_every_counted_key_has_both_forms(self):
        for lang, table in TRANSLATIONS.items():
            ones = {k[: -len("_one")] for k in table if k.endswith("_one")}
            others = {k[: -len("_other")] for k in table if k.endswith("_other")}
            assert ones == others, f"{lang}: unpaired counted keys {ones ^ others}"
