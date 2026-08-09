"""A saved search is identified by its own search URL, not by its label.

The subject line carries a *label*: the mail server folds it (#101), Idealista
rewords it, the owner renames it. Every alert email also links to the search
page, and that link encodes the subscription's filters, so it is the thing that
actually distinguishes one saved search from another (#102).

Verified over the last 60 emails in the mailbox: grouping by path + shape gives
exactly two groups, each mapping to exactly one unfolded subject name.

The canonical form is pinned literally in this module rather than produced by
calling the code under test - otherwise the assertions would only prove the
function agrees with itself.
"""

import hashlib
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from config import Config
from models import Land, Property, SearchProfile
from services import search_profile_service
from services.land_to_property_migration_service import LandToPropertyMigrationService
from services.property_imap_service import PropertyIMAPService
from services.search_profile_service import SearchProfileService
from services.search_subscription_identity import (
    SEARCH_KEY_PREFIX,
    canonicalize_search_url,
    extract_search_identity,
    search_key_for_url,
)
from tests import setup_test_environment

# The two real saved searches, as they appear in the mailbox.
TERRENOS_SHAPE = "((u}ygG~adt@gqdAquaBmnZ_yvC))"
VIVIENDAS_SHAPE = "((wc_hGxqmu@dCsdE~pAquH))"

TERRENOS_FILTERS = (
    "con-precio-hasta_150000,metros-cuadrados-mas-de_100,"
    "metros-cuadrados-menos-de_5000,terrenos-urbanizables,publicado_ultimo-mes"
)
VIVIENDAS_FILTERS = (
    "con-precio-hasta_150000,precio-desde_60000,chalets,de-dos-dormitorios,"
    "de-tres-dormitorios,publicado_ultimo-mes"
)

TERRENOS_URL = (
    f"https://www.idealista.com/en/areas/venta-terrenos/{TERRENOS_FILTERS}/"
    f"?shape={TERRENOS_SHAPE}"
)
VIVIENDAS_URL = (
    f"https://www.idealista.com/en/areas/venta-viviendas/{VIVIENDAS_FILTERS}/"
    f"?shape={VIVIENDAS_SHAPE}"
)

# The same subscription as Idealista writes it in a different email: Spanish
# UI segment, no `www`, plain http, percent-encoded shape, per-email `utm_*`
# noise in a different order, a fragment, and no trailing slash. Every one of
# those differences is cosmetic.
TERRENOS_URL_VARIANT = (
    "http://idealista.com/es/areas/venta-terrenos/"
    f"{TERRENOS_FILTERS}"
    "?utm_notification_id=99887766&"
    "shape=%28%28u%7DygG%7Eadt%40gqdAquaBmnZ_yvC%29%29&"
    "utm_source=email&utm_recipient_id=42#results"
)
# ... and the same link as it is actually written inside an href attribute.
# The stored diagnostic URL is the unescaped one: `&amp;` is HTML escaping,
# not part of the address.
TERRENOS_URL_VARIANT_IN_HTML = TERRENOS_URL_VARIANT.replace("&", "&amp;")

TERRENOS_CANONICAL = (
    f"idealista.com/areas/venta-terrenos/{TERRENOS_FILTERS}"
    "?shape=%28%28u%7DygG~adt%40gqdAquaBmnZ_yvC%29%29"
)
TERRENOS_KEY = (
    SEARCH_KEY_PREFIX + hashlib.sha256(TERRENOS_CANONICAL.encode("utf-8")).hexdigest()
)

LISTING_URL_ONE = "https://www.idealista.com/en/inmueble/112229931/"
LISTING_URL_TWO = "https://www.idealista.com/en/inmueble/112229932/"

# One saved search, two labels. Idealista rewords a saved-search name, and a
# folded subject (#101) truncates it; either way the label moves and the
# subscription does not.
SUBJECT_FULL = (
    "New country house in your search: houses at your custom search area norte!"
)
SUBJECT_TRUNCATED = "New country house in your search: houses at your custom!"
SEARCH_NAME = "houses at your custom search area norte"


def _alert_email(subject: str, listing_url: str, search_url: str) -> bytes:
    """A single-part HTML alert, the shape the real mailbox delivers."""
    return (
        "From: idealista <noresponder@idealista.com>\r\n"
        f"Subject: {subject}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/html; charset="utf-8"\r\n'
        "\r\n"
        "<html><body>"
        f'<a href="{listing_url}">150,000 EUR</a>'
        "<p>200 m2</p>"
        f'<a href="{search_url}">See all listings</a>'
        "</body></html>\r\n"
    ).encode("utf-8")


# Delivered in UID order, so the *last* alert carries the full label and the
# cosmetically different copy of the same search URL.
RAW_EMAILS = {
    1: _alert_email(SUBJECT_TRUNCATED, LISTING_URL_ONE, TERRENOS_URL),
    2: _alert_email(SUBJECT_FULL, LISTING_URL_TWO, TERRENOS_URL_VARIANT_IN_HTML),
}


class _FakeIMAPClient:
    """Serves raw emails, so the test exercises the real parsing path."""

    def __init__(self, host, port=None, ssl=None, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        return True

    def select_folder(self, name, readonly=True):
        return None

    def search(self, args):
        return sorted(RAW_EMAILS)

    def fetch(self, uids, parts):
        return {
            uid: {b"RFC822": RAW_EMAILS[uid], b"INTERNALDATE": None} for uid in uids
        }


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client_app(app):
    """The same app plus a test client, for the profile-editor routes."""
    app.config["WTF_CSRF_ENABLED"] = False
    return app, app.test_client()


# --------------------------------------------------------------------------
# Canonicalization: only provably cosmetic differences may collapse.
# --------------------------------------------------------------------------


def test_canonical_form_is_the_documented_string():
    assert canonicalize_search_url(TERRENOS_URL) == TERRENOS_CANONICAL
    assert search_key_for_url(TERRENOS_URL) == TERRENOS_KEY


@pytest.mark.parametrize(
    "variant",
    [
        TERRENOS_URL_VARIANT_IN_HTML,
        TERRENOS_URL.replace("https://", "http://"),
        TERRENOS_URL.replace("www.idealista.com", "WWW.Idealista.COM"),
        TERRENOS_URL.replace("www.idealista.com", "idealista.com"),
        TERRENOS_URL.replace("/en/", "/es/"),
        TERRENOS_URL.replace(f"{TERRENOS_FILTERS}/?", f"{TERRENOS_FILTERS}?"),
        TERRENOS_URL + "#top",
        TERRENOS_URL + "&utm_notification_id=1&utm_recipient_id=2",
    ],
    ids=[
        "everything-at-once",
        "scheme",
        "host-case",
        "www",
        "language-segment",
        "trailing-slash",
        "fragment",
        "utm-params",
    ],
)
def test_cosmetic_variants_share_one_key(variant):
    assert search_key_for_url(variant) == TERRENOS_KEY


@pytest.mark.parametrize(
    "variant",
    [
        TERRENOS_URL.replace(TERRENOS_SHAPE, "((u}ygG~adt@gqdAquaBmnZ_yvD))"),
        VIVIENDAS_URL,
        # The path is opaque: its comma-separated filters are not sorted, and
        # a reordered path is a different search until Idealista proves it is
        # not.
        TERRENOS_URL.replace(
            "con-precio-hasta_150000,metros-cuadrados-mas-de_100",
            "metros-cuadrados-mas-de_100,con-precio-hasta_150000",
        ),
    ],
    ids=["different-shape", "different-search", "reordered-path"],
)
def test_material_differences_produce_different_keys(variant):
    key = search_key_for_url(variant)

    assert key is not None
    assert key != TERRENOS_KEY


@pytest.mark.parametrize(
    "url",
    [
        TERRENOS_URL.split("?")[0],
        TERRENOS_URL.split("?")[0] + "?utm_source=email&utm_notification_id=7",
        TERRENOS_URL.split("?")[0] + "?shape=",
    ],
    ids=["truncated-before-shape", "only-tracking-params", "empty-shape"],
)
def test_an_areas_url_without_a_polygon_is_not_an_identity(url):
    """`shape` is what tells two custom-area searches apart.

    A URL that lost its query - wrapped mid-line in a text/plain part, or
    truncated - would otherwise mint a key from the path alone, collapsing
    every subscription that happens to share those filters.
    """
    assert canonicalize_search_url(url) is None
    assert search_key_for_url(url) is None


def test_a_truncated_link_does_not_invent_an_identity_or_a_twin(app):
    """The full URL must still land on the profile the label already made."""
    with app.app_context():
        truncated = TERRENOS_URL.split("?")[0]

        by_label = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{truncated}">x</a>'
        )

        assert by_label is not None
        assert by_label.source_search_key is None, (
            "a shapeless URL minted an identity out of the path"
        )

        identified = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert identified is not None
        assert identified.id == by_label.id, (
            "the real URL created a second profile for the same subscription"
        )
        assert identified.source_search_key == TERRENOS_KEY
        assert SearchProfile.query.count() == 1


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://www.idealista.com/en/inmueble/112229931/",
        "https://www.idealista.com/en/venta-viviendas/alicante/",
        "https://example.com/en/areas/venta-terrenos/x/?shape=((a))",
        "javascript:alert(1)",
        "mailto:noresponder@idealista.com",
    ],
    ids=["empty", "listing", "no-areas", "foreign-host", "javascript", "mailto"],
)
def test_non_search_urls_have_no_key(url):
    assert canonicalize_search_url(url) is None
    assert search_key_for_url(url) is None


# --------------------------------------------------------------------------
# Extraction from the email body.
# --------------------------------------------------------------------------


def test_identity_is_extracted_from_the_body_and_ignores_listing_links():
    body = _alert_email(SUBJECT_FULL, LISTING_URL_ONE, TERRENOS_URL).decode("utf-8")

    found = extract_search_identity(body)

    assert found.is_ambiguous is False
    assert found.identity is not None
    assert found.identity.key == TERRENOS_KEY
    assert found.identity.url == TERRENOS_URL


def test_repeated_links_to_the_same_search_are_one_identity():
    body = (
        f'<a href="{TERRENOS_URL}">See all listings</a>'
        f'<a href="{TERRENOS_URL_VARIANT_IN_HTML}">Modify your search</a>'
    )

    found = extract_search_identity(body)

    assert found.identity is not None
    assert found.identity.key == TERRENOS_KEY


def test_an_email_with_no_search_link_is_absent_not_ambiguous():
    """The two outcomes drive different behaviour, so they must differ here."""
    found = extract_search_identity(f'<a href="{LISTING_URL_ONE}">150,000 EUR</a>')

    assert found.identity is None
    assert found.is_ambiguous is False


def test_two_different_searches_in_one_email_are_logged_and_refused(caplog):
    """Never guess: picking "the first link" would bind the wrong identity."""
    body = f'<a href="{TERRENOS_URL}">A</a><a href="{VIVIENDAS_URL}">B</a>'

    with caplog.at_level(logging.WARNING):
        found = extract_search_identity(body)

    assert found.identity is None
    assert found.is_ambiguous is True
    assert set(found.conflicting) == {TERRENOS_KEY, search_key_for_url(VIVIENDAS_URL)}
    assert any("search links" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# resolve_profile: the search key outranks the label.
# --------------------------------------------------------------------------


def test_one_search_url_with_two_labels_resolves_to_one_profile(app):
    with app.app_context():
        first = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">See all listings</a>'
        )
        second = SearchProfileService.resolve_profile(
            SUBJECT_TRUNCATED,
            f'<a href="{TERRENOS_URL_VARIANT_IN_HTML}">See all listings</a>',
        )

        assert first is not None and second is not None
        assert first.id == second.id
        assert first.source_search_key == TERRENOS_KEY
        assert first.source_search_url == TERRENOS_URL_VARIANT
        assert SearchProfile.query.count() == 1


def test_same_label_with_a_different_shape_creates_a_second_profile(app):
    """Two subscriptions may legitimately carry the same human label."""
    with app.app_context():
        other_shape = TERRENOS_URL.replace(TERRENOS_SHAPE, "((zzz))")

        first = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )
        second = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{other_shape}">x</a>'
        )

        assert first is not None and second is not None
        assert first.id != second.id
        assert first.name == second.name == SEARCH_NAME
        assert {p.source_search_key for p in SearchProfile.query.all()} == {
            TERRENOS_KEY,
            search_key_for_url(other_shape),
        }


def test_an_existing_keyless_profile_is_adopted_rather_than_duplicated(app):
    with app.app_context():
        existing = SearchProfile(
            name=SEARCH_NAME,
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(existing)
        db.session.commit()
        existing_id = existing.id

        resolved = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert resolved is not None
        assert resolved.id == existing_id
        assert resolved.source_search_key == TERRENOS_KEY
        assert SearchProfile.query.count() == 1


def test_the_default_profile_is_never_bound_to_one_subscription(app):
    """The catch-all must keep catching; binding it would hijack every email."""
    with app.app_context():
        default = SearchProfile(
            name=SEARCH_NAME,
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(default)
        db.session.commit()

        resolved = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert resolved is not None
        assert resolved.is_default is False
        assert db.session.get(SearchProfile, default.id).source_search_key is None


def test_an_email_without_a_search_url_keeps_the_old_resolution(app):
    with app.app_context():
        by_name = SearchProfileService.resolve_profile(
            "New detached house in your search: Search Junio!",
            "See all listings for 'Search Junio'",
        )

        assert by_name is not None
        assert by_name.name == "Junio"
        assert by_name.source_search_key is None
        # That label came out of an email too, so a later email carrying the
        # subscription's URL is allowed to correct it.
        assert by_name.is_auto_created is True


def test_an_email_without_a_search_url_says_so_instead_of_matching_silently(
    app, caplog
):
    """The label path is the precondition for the #116 split; make it visible.

    A URL-less alert creates or finds a *keyless* profile. `UNIQUE
    (source_search_key)` cannot see that row and `UNIQUE (name) WHERE
    source_search_key IS NULL` cannot see a keyed one - deliberately, because
    two real subscriptions may share a label - so an alert for the same label
    that does carry its URL inserts a second profile and the subscription's
    listings split across both. Neither index may be widened without forbidding
    the supported case, so the one thing available is that the precondition
    stops being silent: the label and the profile it landed on are named.
    """
    with app.app_context():
        with caplog.at_level(logging.WARNING):
            resolved = SearchProfileService.resolve_profile(
                "New detached house in your search: Search Junio!",
                "See all listings for 'Search Junio'",
            )

        assert resolved is not None
        assert resolved.source_search_key is None

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        matched_by_label = [
            message
            for message in warnings
            if "no saved-search URL" in message and "#116" in message
        ]
        assert matched_by_label, (
            "an email resolved by its label alone left no trace in the log"
        )
        assert any("Junio" in message for message in matched_by_label), (
            "the warning does not name the label it matched on"
        )
        assert any(str(resolved.id) in message for message in matched_by_label), (
            "the warning does not name the profile the email landed in"
        )


def test_an_email_that_carries_its_search_url_stays_quiet(app, caplog):
    """The warning must mean something: an identified email must not raise it."""
    with app.app_context():
        with caplog.at_level(logging.WARNING):
            resolved = SearchProfileService.resolve_profile(
                SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
            )

        assert resolved is not None
        assert resolved.source_search_key == TERRENOS_KEY
        assert not [
            record
            for record in caplog.records
            if "no saved-search URL" in record.getMessage()
        ]


def test_an_unlabelled_email_with_a_url_never_lands_in_default(app):
    with app.app_context():
        resolved = SearchProfileService.resolve_profile(
            "Nueva vivienda", f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert resolved is not None
        assert resolved.is_default is False
        assert resolved.source_search_key == TERRENOS_KEY
        assert "venta-terrenos" in resolved.name


def test_only_an_auto_created_label_may_be_rewritten(app):
    with app.app_context():
        owner_named = SearchProfile(
            name="Plots I actually want",
            is_active=True,
            is_default=False,
            is_auto_created=False,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(owner_named)
        db.session.commit()

        resolved = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert resolved is not None
        assert resolved.id == owner_named.id
        assert resolved.name == "Plots I actually want"


def test_an_auto_created_label_follows_the_email(app):
    with app.app_context():
        first = SearchProfileService.resolve_profile(
            SUBJECT_TRUNCATED, f'<a href="{TERRENOS_URL}">x</a>'
        )
        assert first is not None
        assert first.is_auto_created is True

        second = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert second is not None
        assert second.id == first.id
        assert second.name == SEARCH_NAME


def test_an_identity_conflict_is_logged_and_nothing_is_merged(app, caplog):
    """URL says profile A, label says profile B: report it, change neither."""
    with app.app_context():
        keyed = SearchProfile(
            name="Terrenos norte",
            is_active=True,
            is_auto_created=True,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        by_label = SearchProfile(
            name=SEARCH_NAME,
            is_active=True,
            is_auto_created=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([keyed, by_label])
        db.session.commit()
        keyed_id, label_id = keyed.id, by_label.id

        with caplog.at_level(logging.WARNING):
            resolved = SearchProfileService.resolve_profile(
                SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
            )

        assert resolved is not None
        assert resolved.id == keyed_id
        assert db.session.get(SearchProfile, keyed_id).name == "Terrenos norte"
        assert db.session.get(SearchProfile, label_id).name == SEARCH_NAME
        assert SearchProfile.query.count() == 2
        assert any("conflict" in record.message.lower() for record in caplog.records)


def test_merge_refuses_profiles_that_hold_different_search_keys(app):
    """Same label, different subscription: merging would destroy one of them."""
    with app.app_context():
        for key in (TERRENOS_KEY, search_key_for_url(VIVIENDAS_URL)):
            db.session.add(
                SearchProfile(
                    name="Same label",
                    is_active=True,
                    is_auto_created=True,
                    source_search_key=key,
                    travel_targets={"presets": {}, "custom": []},
                )
            )
        db.session.commit()

        report = SearchProfileService.merge_duplicate_profiles()

        assert report["merged_groups"] == 0
        assert report["profiles_deleted"] == 0
        assert report["conflicts"]
        assert SearchProfile.query.count() == 2


# --------------------------------------------------------------------------
# Concurrency and conflict handling. Two ingestions overlap routinely: the
# scheduled run and a manual one, across four gunicorn threads.
# --------------------------------------------------------------------------


def test_adopting_a_profile_that_was_claimed_meanwhile_does_not_steal_it(
    app, monkeypatch
):
    """The candidate list is a snapshot; the row may be claimed before we write.

    Two subscriptions can share a label, so two overlapping ingestions can
    select the *same* keyless row and write different keys into it. An
    unconditional UPDATE lets the second one silently re-point the profile -
    and hand its existing properties to the wrong subscription.

    The interleaving is forced deterministically: the competing write happens
    while the candidate list is still being filtered, which is exactly the
    window between the SELECT and the UPDATE.
    """
    with app.app_context():
        candidate = SearchProfile(
            # Same label, different spelling, so the seam below can tell the
            # candidate's name apart from the label read out of the email.
            name=SEARCH_NAME.title(),
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(candidate)
        db.session.commit()
        candidate_id = candidate.id

        other_key = search_key_for_url(VIVIENDAS_URL)
        original = search_profile_service._canonical_profile_name

        def canonical_with_a_racing_writer(value):
            if value == SEARCH_NAME.title():
                # "The other ingestion" claims the row first and commits.
                db.session.execute(
                    text(
                        "UPDATE search_profiles SET source_search_key = :key "
                        "WHERE id = :id"
                    ),
                    {"key": other_key, "id": candidate_id},
                )
                db.session.commit()
            return original(value)

        monkeypatch.setattr(
            search_profile_service,
            "_canonical_profile_name",
            canonical_with_a_racing_writer,
        )

        resolved = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        claimed = db.session.get(SearchProfile, candidate_id)
        assert claimed.source_search_key == other_key, (
            "the row was re-pointed at another subscription's search"
        )
        assert resolved is not None
        assert resolved.id != candidate_id
        assert resolved.source_search_key == TERRENOS_KEY


def test_two_keyless_profiles_cannot_share_a_label(app):
    """Dropping the UNIQUE on `name` must not drop check-then-insert safety.

    Until #102 the UNIQUE constraint was what stopped two overlapping
    ingestions both passing the "does a profile with this name exist?" check
    and both inserting. Profiles that hold *different* search keys must still
    be free to share a label; keyless ones must not.
    """
    with app.app_context():
        db.session.add(
            SearchProfile(
                name="Same label",
                is_active=True,
                travel_targets={"presets": {}, "custom": []},
            )
        )
        db.session.commit()

        db.session.add(
            SearchProfile(
                name="Same label",
                is_active=True,
                travel_targets={"presets": {}, "custom": []},
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # ... but two identified subscriptions may legitimately share it.
        for key in (TERRENOS_KEY, search_key_for_url(VIVIENDAS_URL)):
            db.session.add(
                SearchProfile(
                    name="Same label",
                    is_active=True,
                    source_search_key=key,
                    travel_targets={"presets": {}, "custom": []},
                )
            )
        db.session.commit()

        assert SearchProfile.query.filter_by(name="Same label").count() == 3


def test_an_ambiguous_email_stops_resolution_instead_of_falling_back(app):
    """ "Several searches" is not "no search".

    Falling through to the label would land the listing in whichever
    same-named subscription happens to exist - the guess the extractor just
    refused to make.
    """
    with app.app_context():
        existing = SearchProfile(
            name=SEARCH_NAME,
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(existing)
        db.session.commit()

        resolved = SearchProfileService.resolve_profile(
            SUBJECT_FULL,
            f'<a href="{TERRENOS_URL}">A</a><a href="{VIVIENDAS_URL}">B</a>',
        )

        assert resolved is None, "an ambiguous email must not pick a profile"
        assert SearchProfile.query.count() == 1
        assert db.session.get(SearchProfile, existing.id).source_search_key is None


def test_merge_never_pins_a_search_key_to_the_catch_all(app):
    """The default profile must not end up representing one subscription.

    A keyed profile and the default can normalize to the same canonical label
    ("Same label" vs "Same label!"). The default sorts first, so carrying the
    key onto the primary would make one row both the fallback for everything
    unmatched and the identity of one saved search - the invariant
    `_adopt_keyless_profile` protects, broken from the other end.
    """
    with app.app_context():
        catch_all = SearchProfile(
            name="Same label!",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        keyed = SearchProfile(
            name="Same label",
            is_active=True,
            is_auto_created=True,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([catch_all, keyed])
        db.session.commit()
        catch_all_id, keyed_id = catch_all.id, keyed.id

        report = SearchProfileService.merge_duplicate_profiles()

        assert db.session.get(SearchProfile, catch_all_id).source_search_key is None, (
            "the catch-all was pinned to one subscription"
        )
        assert db.session.get(SearchProfile, keyed_id) is not None, (
            "the subscription's identity was deleted"
        )
        assert db.session.get(SearchProfile, keyed_id).is_default is False
        assert report["merged_groups"] == 0
        assert report["profiles_deleted"] == 0
        assert report["conflicts"]


def test_the_database_refuses_to_make_an_identified_profile_the_catch_all(app):
    """The invariant is enforced by the schema, not by each reader.

    Five separate entry points had to be patched to keep a search key off the
    catch-all - merge, the label fallback, `get_default_profile()`, the profile
    editor. A CHECK constraint closes the class instead of the next door: any
    route written later, and any hand-written UPDATE, fails at the database.
    """
    with app.app_context():
        subscription = SearchProfile(
            name="Terrenos norte",
            is_active=True,
            is_default=False,
            source_search_key=TERRENOS_KEY,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(subscription)
        db.session.commit()

        subscription.is_default = True
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        # ... and from the other direction: the catch-all cannot acquire one.
        catch_all = SearchProfile(
            name="Catch all",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(catch_all)
        db.session.commit()

        catch_all.source_search_key = search_key_for_url(VIVIENDAS_URL)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_the_profile_editor_refuses_to_promote_an_identified_profile(client_app):
    """The owner gets an explanation, not a 500 from the constraint."""
    app, client = client_app
    with app.app_context():
        catch_all = SearchProfile(
            name="Catch all",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        subscription = SearchProfile(
            name="Terrenos norte",
            is_active=True,
            is_default=False,
            source_search_key=TERRENOS_KEY,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([catch_all, subscription])
        db.session.commit()
        catch_all_id, subscription_id = catch_all.id, subscription.id

    response = client.post(
        f"/profiles/{subscription_id}/edit",
        data={
            "action": "save_profile_settings",
            "is_active": "on",
            "is_default": "on",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(SearchProfile, subscription_id).is_default is False, (
            "an identified saved search was promoted to the catch-all"
        )
        assert db.session.get(SearchProfile, catch_all_id).is_default is True
        assert SearchProfileService.get_default_profile(create=False).id == catch_all_id

    # The form should not offer the action in the first place.
    page = client.get(f"/profiles/{subscription_id}/edit")
    assert page.status_code == 200
    checkbox = page.get_data(as_text=True).split('name="is_default"')[1].split(">")[0]
    assert "disabled" in checkbox


def test_the_profile_editor_still_promotes_an_unidentified_profile(client_app):
    """The guard must not break the ordinary case it is wrapped around."""
    app, client = client_app
    with app.app_context():
        old = SearchProfile(
            name="Catch all",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        fresh = SearchProfile(
            name="New catch all",
            is_active=True,
            is_default=False,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([old, fresh])
        db.session.commit()
        old_id, fresh_id = old.id, fresh.id

    response = client.post(
        f"/profiles/{fresh_id}/edit",
        data={"action": "save_profile_settings", "is_default": "on"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(SearchProfile, fresh_id).is_default is True
        assert db.session.get(SearchProfile, old_id).is_default is False


def test_the_catch_all_is_never_a_subscription_that_is_merely_named_default(app):
    """A saved search may legitimately be labelled "Default" now.

    Labels are no longer unique, so a keyed profile can carry that name without
    being the fallback. Flagging it would hand one subscription every email
    that matches nothing.
    """
    with app.app_context():
        subscription = SearchProfile(
            name=search_profile_service.DEFAULT_PROFILE_NAME,
            is_active=True,
            is_default=False,
            is_auto_created=True,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(subscription)
        db.session.commit()
        subscription_id = subscription.id

        catch_all = SearchProfileService.get_default_profile(create=True)

        assert catch_all is not None
        assert catch_all.id != subscription_id, (
            "a subscription was promoted to the catch-all"
        )
        assert catch_all.source_search_key is None
        assert db.session.get(SearchProfile, subscription_id).is_default is False


def test_an_identified_email_that_cannot_be_resolved_stays_unassigned(app, monkeypatch):
    """Falling back to the label would hand it to a *different* subscription.

    Labels are no longer unique among identified profiles, so
    `get_or_create_profile_by_name()` can return a profile carrying somebody
    else's search key. An email whose own search URL was read successfully must
    never be resolved by its label.

    The failure is injected at the leaf that can genuinely fail in production -
    the insert - so the whole real resolution path runs first.
    """
    with app.app_context():
        foreign = SearchProfile(
            name=SEARCH_NAME,
            is_active=True,
            is_auto_created=True,
            source_search_key=search_key_for_url(VIVIENDAS_URL),
            source_search_url=VIVIENDAS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(foreign)
        db.session.commit()

        monkeypatch.setattr(
            SearchProfileService,
            "_create_profile_for_identity",
            staticmethod(lambda identity, search_name: None),
        )

        resolved = SearchProfileService.resolve_profile(
            SUBJECT_FULL, f'<a href="{TERRENOS_URL}">x</a>'
        )

        assert resolved is None, (
            "an identified email fell back to a label owned by another search"
        )
        assert SearchProfile.query.count() == 1
        assert db.session.get(SearchProfile, foreign.id).source_search_key == (
            search_key_for_url(VIVIENDAS_URL)
        )


def test_a_label_shared_by_two_subscriptions_resolves_to_neither(app):
    """A URL-less email cannot choose between two searches with one label.

    That pairing only became representable in #102, so this is a hazard the
    change itself introduced into the label fallback.
    """
    with app.app_context():
        for key in (TERRENOS_KEY, search_key_for_url(VIVIENDAS_URL)):
            db.session.add(
                SearchProfile(
                    name=SEARCH_NAME,
                    is_active=True,
                    is_auto_created=True,
                    source_search_key=key,
                    travel_targets={"presets": {}, "custom": []},
                )
            )
        db.session.commit()

        by_label = SearchProfileService.get_or_create_profile_by_name(SEARCH_NAME)

        assert by_label is None, "the label picked one of two subscriptions"
        assert SearchProfile.query.count() == 2, "and it must not add a third"


def test_the_legacy_land_archive_never_joins_an_identified_subscription(app):
    """168 migrated plots must not be poured into somebody's live search.

    The migration looks its profile up by name, and names stopped being unique
    in #102 - so a saved search labelled "Legacy Lands" would swallow the whole
    legacy archive.
    """
    with app.app_context():
        subscription = SearchProfile(
            name=LandToPropertyMigrationService.DEFAULT_PROFILE_NAME,
            is_active=True,
            is_auto_created=True,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(subscription)
        db.session.commit()
        subscription_id = subscription.id

        db.session.add(
            Land(
                source_email_id="legacy_1",
                url="https://www.idealista.com/en/inmueble/900001/",
                title="Legacy plot",
            )
        )
        db.session.commit()

        report = LandToPropertyMigrationService().migrate(dry_run=False)

        assert report["properties_created"] == 1
        migrated = Property.query.one()
        assert migrated.search_profile_id != subscription_id, (
            "the legacy archive was migrated into a live subscription"
        )
        archive = db.session.get(SearchProfile, migrated.search_profile_id)
        assert archive.source_search_key is None
        assert archive.name == LandToPropertyMigrationService.DEFAULT_PROFILE_NAME


def test_a_lost_insert_race_re_reads_the_unidentified_winner(app, monkeypatch):
    """The recovery read must not hand the email to a keyed namesake.

    Two URL-less ingestions can create the same keyless profile at once. The
    loser falls into the recovery read, and an unconditional lookup by name can
    return a *keyed* profile that appeared alongside it.

    The interleaving is forced deterministically: the competitors are committed
    while the losing row is still being constructed, i.e. after its checks and
    before its INSERT.
    """
    with app.app_context():
        original = search_profile_service.default_travel_targets_config

        def commit_the_competitors():
            # The keyed row goes in first, so an unordered `.first()` returns it.
            db.session.execute(
                text(
                    "INSERT INTO search_profiles (name, is_active, is_default, "
                    "source_search_key, created_at, updated_at) VALUES "
                    "(:name, 1, 0, :key, :now, :now)"
                ),
                {
                    "name": SEARCH_NAME,
                    "key": TERRENOS_KEY,
                    "now": datetime(2026, 8, 8, tzinfo=timezone.utc),
                },
            )
            db.session.execute(
                text(
                    "INSERT INTO search_profiles (name, is_active, is_default, "
                    "created_at, updated_at) VALUES (:name, 1, 0, :now, :now)"
                ),
                {
                    "name": SEARCH_NAME,
                    "now": datetime(2026, 8, 8, tzinfo=timezone.utc),
                },
            )
            db.session.commit()
            monkeypatch.setattr(
                search_profile_service, "default_travel_targets_config", original
            )
            return original()

        monkeypatch.setattr(
            search_profile_service,
            "default_travel_targets_config",
            commit_the_competitors,
        )

        resolved = SearchProfileService.get_or_create_profile_by_name(SEARCH_NAME)

        assert resolved is not None
        assert resolved.source_search_key is None, (
            "the losing insert was handed a profile owned by another search"
        )
        assert SearchProfile.query.count() == 2


def test_merging_onto_a_keyless_primary_keeps_the_only_search_key(app):
    """The surviving row must inherit the identity, not drop it.

    The primary is chosen by property count, so the keyless row usually wins -
    and deleting the keyed one used to delete the subscription's identity with
    it, unrecoverably (nothing records which search a stored row came from).
    """
    with app.app_context():
        keyless = SearchProfile(
            name="Terrenos norte",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        keyed = SearchProfile(
            name="Terrenos Norte",
            is_active=True,
            is_auto_created=True,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([keyless, keyed])
        db.session.commit()
        keyless_id = keyless.id

        # Property count decides the primary, so the keyless row wins.
        db.session.add(
            Property(
                source_email_id="imap_1",
                url="https://www.idealista.com/en/inmueble/1/",
                search_profile_id=keyless_id,
            )
        )
        db.session.commit()

        report = SearchProfileService.merge_duplicate_profiles()

        assert report["merged_groups"] == 1
        survivor = db.session.get(SearchProfile, keyless_id)
        assert survivor is not None, "the keyless row with the listings survives"
        assert survivor.source_search_key == TERRENOS_KEY, (
            "the merge deleted the subscription's identity"
        )
        assert survivor.source_search_url == TERRENOS_URL


def test_the_merge_asks_the_database_to_hold_the_group_it_decides_on():
    """Pin that the lock is actually requested.

    SQLite ignores `FOR UPDATE`, so this suite cannot demonstrate the locking
    itself - only that the statement asks for it. The semantics come from
    Postgres, the same honest limitation as the repair service's lock.
    """
    from sqlalchemy.dialects import postgresql

    from services.search_profile_service import lock_profiles_statement

    sql = str(lock_profiles_statement([9, 7, 8]).compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in sql
    assert "search_profiles" in sql


def test_merge_decides_on_the_rows_it_locked_not_on_the_snapshot(app, monkeypatch):
    """A key acquired after the grouping read must not be merged away (#116).

    `merge_duplicate_profiles()` grouped the profiles with a plain SELECT and
    computed its refusal from that snapshot. A `_claim_keyless_profile()`
    landing in between turns a group that looked safe - one key, so mergeable -
    into one holding two, and the merge then deletes a row that has just
    acquired an identity. Nothing records which saved search a stored listing
    came from, so that is unrecoverable.

    The interleaving is forced deterministically at the moment the group is
    locked: the competing write is *not* committed, so it cannot expire the
    snapshot's objects on its way past - the merge can only see it by reading
    the rows again under the lock, which is the fix. SQLite cannot show the
    lock blocking a second connection; that half is Postgres semantics.
    """
    with app.app_context():
        keyless = SearchProfile(
            name="Terrenos norte",
            is_active=True,
            travel_targets={"presets": {}, "custom": []},
        )
        keyed = SearchProfile(
            name="Terrenos Norte",
            is_active=True,
            is_auto_created=True,
            source_search_key=TERRENOS_KEY,
            source_search_url=TERRENOS_URL,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add_all([keyless, keyed])
        db.session.commit()
        keyless_id, keyed_id = keyless.id, keyed.id

        other_key = search_key_for_url(VIVIENDAS_URL)
        original = search_profile_service.lock_profiles_statement
        raced = False

        def claim_the_keyless_row_first(profile_ids):
            nonlocal raced
            if not raced:
                raced = True
                # "The other ingestion" binds a second subscription to the row
                # the merge is about to treat as a spare duplicate.
                db.session.execute(
                    text(
                        "UPDATE search_profiles SET source_search_key = :key "
                        "WHERE id = :id"
                    ),
                    {"key": other_key, "id": keyless_id},
                )
            return original(profile_ids)

        monkeypatch.setattr(
            search_profile_service,
            "lock_profiles_statement",
            claim_the_keyless_row_first,
        )

        report = SearchProfileService.merge_duplicate_profiles()

        assert raced, "the seam never fired; the merge does not lock its group"
        assert report["merged_groups"] == 0
        assert report["profiles_deleted"] == 0
        assert report["conflicts"], (
            "a group holding two search keys must be reported, not merged"
        )

        db.session.expire_all()
        assert db.session.get(SearchProfile, keyed_id) is not None, (
            "the merge deleted a profile that still carried its own identity"
        )
        assert (
            db.session.get(SearchProfile, keyless_id).source_search_key == other_key
        ), "the identity claimed mid-merge was overwritten"


# --------------------------------------------------------------------------
# The ingestion boundary: the URL has to survive the whole IMAP pipeline.
# --------------------------------------------------------------------------


def test_the_search_url_reaches_profile_resolution_through_ingestion(app):
    """Two alerts, one saved search, two different labels: one profile."""
    with app.app_context():
        Config.AUTO_TRAVEL_ENRICHMENT = False
        Config.AUTO_PROPERTY_SCORING = False

        with patch("services.property_imap_service.IMAPClient", _FakeIMAPClient):
            service = PropertyIMAPService()
            service.user = "user@example.com"
            service.password = "dummy"
            service.host = "imap.gmail.com"
            service.folder = "Idealista"
            service.last_seen_uid = 0
            service.run_ingestion(sync_type="test")

        first = Property.query.filter_by(idealista_property_id=112229931).first()
        second = Property.query.filter_by(idealista_property_id=112229932).first()
        assert first is not None and second is not None, "both alerts should ingest"

        assert first.search_profile_id == second.search_profile_id, (
            "the same saved search under two labels must not fragment"
        )

        profile = db.session.get(SearchProfile, first.search_profile_id)
        assert profile is not None
        assert profile.source_search_key == TERRENOS_KEY, (
            "the search URL never reached resolve_profile"
        )
        assert profile.source_search_url == TERRENOS_URL_VARIANT
        assert profile.name == SEARCH_NAME
        assert SearchProfile.query.count() == 1
