"""Actual #584 failures: translated 'vendido', missing ids, bare CDN URLs."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property
from services import portal_photos
from services.listing_status_service import ListingStatusService
from tests import setup_test_environment
from utils import audit_property_listings as audit
from utils.backfill_idealista_photos import fill

FOTO = "https://www.fotocasa.es/es/comprar/terreno/aviles/x/190280914/d"
MIL = "https://www.milanuncios.com/venta-de-casas/x-612329827.htm"
PHOTO = "https://img4.idealista.com/blur/500_375_mq/0/id.pro.es.image.master/a6/e0/7d/1391040892.jpg"
BARE = "https://images.milanuncios.com/api/v1/ma-ad-media-pro/images/650e68ab-aa09-4842-a458-4bdbe3356b7f"
MAIL = f'<a href="https://www.idealista.com/inmueble/109962514/?utm_source=mail"><img src="{PHOTO}"></a>'


def observe(url, body, final=None, code=200):
    service = ListingStatusService()
    response = SimpleNamespace(status_code=code, text=body, url=final or url)
    with patch(
        "services.listing_status_service.request_with_retries", return_value=response
    ):
        result = service.observe(url)
    return result, service


@pytest.mark.parametrize(
    "url,fixture",
    [
        (FOTO, "fotocasa_listing_190280914.html"),
        (MIL, "milanuncios_listing_612329827.html"),
    ],
)
def test_live_payload_translation_is_not_sold(url, fixture):
    body = (
        Path("tests/data", fixture).read_text()
        + '<script>"reason":"Ya está alquilado o vendido"</script>'
    )
    result, service = observe(url, body)
    assert result.status == "active"
    assert service.captured_photos["items"]


def test_missing_fotocasa_redirect_is_removed_not_sold():
    result, _ = observe(
        FOTO,
        "Ya está alquilado o vendido",
        "https://www.fotocasa.es/es/comprar/terrenos/asturias/l?propertyNotFound",
    )
    assert result.status == "removed"


@pytest.mark.parametrize(
    "final",
    [
        "https://www.fotocasa.es/es/comprar/terrenos/asturias/l",
        "https://evil.example/l?propertyNotFound",
        "https://www.fotocasa.es/es/comprar/terreno/x/1902809140/d",
    ],
)
def test_unrelated_page_with_sold_words_is_unresolved(final):
    result, _ = observe(FOTO, "vendido", final)
    assert result.status == "error"


def test_payload_for_another_listing_is_not_confirmation():
    result, _ = observe(
        FOTO.replace("190280914", "190280915"),
        Path("tests/data/fotocasa_listing_190280914.html").read_text(),
    )
    assert result.status == "error"


def test_milanuncios_search_redirect_is_not_active():
    result, _ = observe(
        MIL, "Resultados", "https://www.milanuncios.com/venta-de-casas/"
    )
    assert result.status == "error"


def test_captcha_404_is_not_a_removal():
    result, _ = observe(FOTO, "https://captcha-delivery.com/", code=404)
    assert result.status == "error"


def test_reader_repairs_existing_bare_milanuncios_urls():
    p = SimpleNamespace(
        enrichment={"import": {"photos": {"items": [{"url": BARE}], "published": 1}}}
    )
    assert portal_photos.read_photos(p)["photos"][0]["url"] == BARE + "?rule=hw396_70"
    assert (
        portal_photos.normalise_photo_url(BARE + "?rule=hw396_70")
        == BARE + "?rule=hw396_70"
    )
    assert portal_photos.normalise_photo_url(
        BARE.replace("images.milanuncios.com", "images.milanuncios.com.evil.test")
    ) == BARE.replace("images.milanuncios.com", "images.milanuncios.com.evil.test")


def test_idealista_photos_are_bound_to_each_listing_not_the_mail():
    markup = '<img src="https://st3.idealista.com/static/logo.png">' + MAIL
    markup += f'<a href="https://www.idealista.com/inmueble/222/"><img src="{PHOTO}?token=redacted"></a>'
    markup += f'<a href="https://evil.test/inmueble/333/"><img src="{PHOTO}"></a>'
    markup += f'<img src="{PHOTO}">'
    captures = portal_photos.from_idealista_email(markup)
    assert captures == {109962514: {"items": [{"url": PHOTO}], "published": 1}}


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def row(key, **kwargs):
    p = Property(
        source_email_id=key, url=FOTO, idealista_property_id=109962514, **kwargs
    )
    db.session.add(p)
    db.session.commit()
    return p


def entry(p, **kwargs):
    return {
        "id": p.id,
        "baseline": audit.baseline(p),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "removed",
        "refusal": None,
        "removed_date": None,
        "photos": None,
        **kwargs,
    }


def test_apply_only_answers_with_fresh_unchanged_baseline(app):
    p = row("a")
    e = entry(p)
    assert audit.apply_entry(e) == "applied"
    db.session.refresh(p)
    assert p.listing_status == "removed"
    assert p.listing_status_source == "check"
    assert audit.apply_entry(e) == "conflict"


@pytest.mark.parametrize("kind", ["refused", "expired", "changed", "manual"])
def test_unanswered_stale_or_changed_rows_are_not_rewritten(app, kind):
    p = row(kind)
    e = entry(p)
    if kind == "refused":
        e.update(status="error", refusal="blocked")
    elif kind == "expired":
        e["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    else:
        p.listing_status_source = "manual" if kind == "manual" else "email"
        db.session.commit()
        if kind == "manual":
            e["baseline"] = audit.baseline(p)
    before = audit.baseline(p)
    assert audit.apply_entry(e) != "applied"
    db.session.refresh(p)
    assert audit.baseline(p) == before


def test_mail_backfill_fills_all_profile_copies_without_creating_or_overwriting(app):
    a = row("one", enrichment={"location": {"source": "manual"}})
    b = row("two")
    c = row(
        "existing", enrichment={"import": {"photos": {"items": [], "published": 0}}}
    )
    captures = portal_photos.from_idealista_email(MAIL)
    saved = deepcopy(c.enrichment)
    assert fill(captures) == {"would_fill": 2, "already": 1}
    assert portal_photos.read_photos(a)["count"] == 0
    assert fill(captures, apply=True) == {"filled": 2, "already": 1}
    for p in [a, b]:
        db.session.refresh(p)
        assert portal_photos.read_photos(p)["photos"] == [{"url": PHOTO}]
    assert a.enrichment["location"] == {"source": "manual"}
    assert c.enrichment == saved
    assert Property.query.count() == 3
    assert fill(captures, apply=True) == {"already": 3}


def test_new_idealista_ingestion_persists_the_captured_email_photo(app, monkeypatch):
    from services.property_imap_service import PropertyIMAPService

    service = PropertyIMAPService()
    monkeypatch.setattr(
        service,
        "get_idealista_emails",
        lambda max_results=None: [
            {
                "type": "listing",
                "source_email_id": "imap_584",
                "url": "https://www.idealista.com/inmueble/109962514/",
                "idealista_property_id": 109962514,
                "title": "Casa",
                "price": 180000,
                "photos": portal_photos.from_idealista_email(MAIL)[109962514],
            }
        ],
    )
    monkeypatch.setattr(service, "_enrich_new_property", lambda prop: None)
    assert service.run_ingestion(sync_type="test") == 1
    p = Property.query.one()
    assert portal_photos.read_photos(p)["photos"] == [{"url": PHOTO}]


def test_audit_photo_fill_preserves_other_blocks_and_measured_empty(app):
    photos = {"items": [{"url": PHOTO}], "published": 1}
    p = row("with-location", enrichment={"location": {"source": "manual"}})
    assert audit.apply_entry(entry(p, status="active", photos=photos)) == "applied"
    db.session.refresh(p)
    assert p.enrichment["location"] == {"source": "manual"}
    assert portal_photos.read_photos(p)["photos"] == [{"url": PHOTO}]
    q = row("empty", enrichment={"import": {"photos": {"items": [], "published": 0}}})
    assert audit.apply_entry(entry(q, status="active", photos=photos)) == "applied"
    db.session.refresh(q)
    assert portal_photos.read_photos(q)["state"] == "none_published"


def test_apply_rechecks_baseline_after_taking_row_lock(app, monkeypatch):
    from contextlib import contextmanager

    p = row("raced")
    e = entry(p)
    real = audit.locked_write

    @contextmanager
    def racing(row, *, locked, commit):
        assert locked and commit
        # An intervening owner's status edit becomes visible on refresh.
        row.listing_status_source = "manual"
        db.session.commit()
        with real(row, locked=locked, commit=commit):
            yield

    monkeypatch.setattr(audit, "locked_write", racing)
    assert audit.apply_entry(e) == "conflict"
    db.session.refresh(p)
    assert p.listing_status == "active"
    assert p.listing_status_source == "manual"
