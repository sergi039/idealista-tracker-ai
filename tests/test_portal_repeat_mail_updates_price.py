"""A repeated yaencontre alert updates the stored price (#631).

Yaencontre's email card carries the asking price and the portal answers
DataDome, so the repeat mail is the only signal a changed price will ever
get. The idealista branch already did this; the portal door skipped a known
listing before comparing prices. Both now go through
`PropertyIMAPService._apply_price_update`.

Fotocasa and milanuncios are deliberately not covered: their price comes
from a page fetch and their dedup runs before any fetch.
"""

from types import SimpleNamespace

import pytest

from models import Property, SyncHistory
from services.property_imap_service import PropertyIMAPService
from tests import test_portal_alert_ingestion as _base
from tests.test_portal_alert_ingestion import (
    DATA,
    _FakeIMAPClient,
    _service,
    _wrap_email,
)
from app import db
from config import Config

# Fixtures are the sibling module's own, re-registered under their names.
app = _base.app
uid_file = _base.uid_file
imap = _base.imap

LISTING = "yaencontre:112395195"
SUBJECT = "Tenemos más inmuebles para tu búsqueda en Boiro (personalizada)"
SENDER = "yaencontre <no-reply@envios.yaencontre.com>"


def _alert(imap) -> None:
    imap.payloads = {
        1: _wrap_email(
            SUBJECT, (DATA / "yaencontre_alert_boiro.html").read_text(), SENDER
        )
    }


def _rerun(imap):
    _FakeIMAPClient.payloads = dict(imap.payloads)
    service = _service()
    service.last_seen_uid = 0
    created = service.run_ingestion(sync_type="test")
    return service, created


class TestYaencontreRepeatMail:
    def test_a_changed_card_price_updates_the_known_row(self, app, uid_file, imap):
        _alert(imap)
        with app.app_context():
            assert _service().run_ingestion(sync_type="test") == 10
            prop = Property.query.filter_by(source_email_id=LISTING).one()
            assert float(prop.price) == 180000.0
            # The portal price moved since we stored the row.
            prop.price = 200000
            db.session.commit()

            _, created = _rerun(imap)

            assert created == 0
            assert Property.query.count() == 10
            prop = Property.query.filter_by(source_email_id=LISTING).one()
            assert float(prop.price) == 180000.0
            assert float(prop.previous_price) == 200000.0
            assert float(prop.price_change_amount) == -20000.0
            assert float(prop.price_change_percentage) == pytest.approx(-10.0)
            assert prop.price_changed_date is not None
            assert prop.email_date is not None
            sync = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
            assert sync.price_updated_count == 1

    def test_an_unchanged_price_changes_nothing(self, app, uid_file, imap):
        _alert(imap)
        with app.app_context():
            _service().run_ingestion(sync_type="test")
            _rerun(imap)
            prop = Property.query.filter_by(source_email_id=LISTING).one()
            assert prop.previous_price is None
            assert prop.price_changed_date is None
            sync = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
            assert sync.price_updated_count == 0

    def test_a_card_without_a_price_keeps_the_stored_one(
        self, app, uid_file, imap, monkeypatch
    ):
        _alert(imap)
        with app.app_context():
            _service().run_ingestion(sync_type="test")
            from services import yaencontre_source

            real = yaencontre_source.cards_in_email
            monkeypatch.setattr(
                yaencontre_source,
                "cards_in_email",
                lambda body: [
                    type(card)(**{**card.__dict__, "price": None})
                    for card in real(body)
                ],
            )
            _rerun(imap)
            prop = Property.query.filter_by(source_email_id=LISTING).one()
            assert float(prop.price) == 180000.0
            assert prop.previous_price is None

    def test_a_price_update_rescored_when_auto_scoring_is_on(
        self, app, uid_file, imap, monkeypatch
    ):
        _alert(imap)
        monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", True)
        calls = []

        class _Scorer:
            def calculate_for_property(self, prop, commit=True):
                calls.append((prop.source_email_id, commit))

        monkeypatch.setattr(
            "services.property_scoring_service.PropertyScoringService", _Scorer
        )
        with app.app_context():
            _service().run_ingestion(sync_type="test")
            calls.clear()
            prop = Property.query.filter_by(source_email_id=LISTING).one()
            prop.price = 200000
            db.session.commit()
            _rerun(imap)
            assert (LISTING, False) in calls


class TestApplyPriceUpdate:
    @pytest.mark.parametrize("bad", [None, 0, -5.0, float("nan"), float("inf")])
    def test_a_missing_or_unusable_price_changes_nothing(self, bad):
        prop = SimpleNamespace(price=100000, previous_price=None)
        assert PropertyIMAPService._apply_price_update(prop, bad) is False
        assert prop.price == 100000
        assert prop.previous_price is None

    def test_an_equal_price_changes_nothing(self):
        prop = SimpleNamespace(price=100000, previous_price=None)
        assert PropertyIMAPService._apply_price_update(prop, 100000.0) is False
        assert prop.previous_price is None

    def test_a_missing_stored_price_is_backfilled_with_the_hint(self):
        prop = SimpleNamespace(price=None)
        assert (
            PropertyIMAPService._apply_price_update(
                prop, 90000.0, previous_price_hint=100000.0
            )
            is True
        )
        assert prop.price == 90000.0
        assert prop.previous_price == 100000.0
        assert prop.price_change_amount == -10000.0
