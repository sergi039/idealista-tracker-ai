"""Mail is stored before the slow, free enrichment of any row starts.

Measured on production 2026-11-10: every new listing cost about 95 s of paced
Overpass round trips inside the mail loop, so a batch of alert emails took
hours and every later email waited unsaved behind the first one's amenities.
`run_ingestion` now stores the whole batch, moves the cursor and writes the
run's history row first; the free pass follows for the rows it created.

Pinned here:

* when the free pass first runs, every row of the batch already exists and
  the history row is already closed;
* every created row still gets its free pass, exactly once;
* a row enriched outside a run gets the free pass inline, as before;
* a run that failed half-way still enriches the rows it had committed.
"""

from datetime import datetime, timezone

import pytest

from app import create_app, db
from config import Config
from models import Property, SyncHistory
from services.property_enrichment_service import PropertyEnrichmentService
from services.property_imap_service import PropertyIMAPService
from tests import setup_test_environment
from utils.cache import cache

INTERNAL_DATE = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def app(monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
    monkeypatch.setattr(Config, "AUTO_GEOCODING", False)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", True)

    with app.app_context():
        cache.clear()
        db.create_all()
        yield app
        db.session.rollback()
        db.drop_all()
        cache.clear()


@pytest.fixture
def free_pass(monkeypatch):
    """Replace the free pass with a recorder of what the database held."""
    calls = []

    def record(self, prop, *, commit, use_ai):
        history = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
        calls.append(
            {
                "property_id": prop.id,
                "rows": Property.query.count(),
                "run_closed": bool(history and history.completed_at),
                "use_ai": use_ai,
            }
        )

    monkeypatch.setattr(PropertyEnrichmentService, "enrich_free_sources", record)
    return calls


def _listing_email(idealista_id):
    return {
        "type": "listing",
        "source_email_id": f"imap_defer_{idealista_id}",
        "email_received_at": INTERNAL_DATE,
        "email_subject": "New home in your search: Navia",
        "email_sender": "Idealista <noresponder@idealista.com>",
        "title": "Casa rural en Navia",
        "url": f"https://www.idealista.com/inmueble/{idealista_id}/",
        "deal_type": "sale",
        "price": 250000.0,
        "area": 120,
        "area_type": "built",
        "municipality": "Navia",
        "search_profile_id": None,
        "property_category": "house",
        "property_subtype": None,
        "description": "Casa con finca y huerta en Navia",
        "attributes": None,
        "idealista_property_id": idealista_id,
    }


def _service(monkeypatch, emails):
    service = PropertyIMAPService()
    monkeypatch.setattr(service, "get_idealista_emails", lambda: emails)
    return service


def test_the_batch_is_stored_before_any_free_pass_runs(app, monkeypatch, free_pass):
    service = _service(monkeypatch, [_listing_email(n) for n in (991, 992, 993)])

    assert service.run_ingestion() == 3

    assert free_pass, "the free pass never ran"
    assert free_pass[0]["rows"] == 3
    assert free_pass[0]["run_closed"] is True


def test_every_created_row_gets_its_free_pass_once(app, monkeypatch, free_pass):
    service = _service(monkeypatch, [_listing_email(n) for n in (991, 992, 993)])

    service.run_ingestion()

    created = sorted(p.id for p in Property.query.all())
    assert sorted(c["property_id"] for c in free_pass) == created
    assert {c["use_ai"] for c in free_pass} == {False}
    assert service._deferred_enrichment is None


def test_a_row_enriched_outside_a_run_gets_the_free_pass_inline(
    app, monkeypatch, free_pass
):
    prop = Property()
    prop.source_email_id = "manual:defer-1"
    prop.title = "Casa rural en Navia"
    prop.url = "https://www.idealista.com/inmueble/994/"
    db.session.add(prop)
    db.session.commit()

    PropertyIMAPService()._enrich_new_property(prop)

    assert [c["property_id"] for c in free_pass] == [prop.id]


def test_a_failed_run_still_enriches_what_it_committed(app, monkeypatch, free_pass):
    class BreaksAfterOne(list):
        """A batch whose read gives one email and then fails mid-loop."""

        def __iter__(self):
            yield _listing_email(991)
            raise RuntimeError("the mailbox went away")

    service = _service(monkeypatch, BreaksAfterOne([None, None]))

    assert service.run_ingestion() == 0

    history = SyncHistory.query.order_by(SyncHistory.id.desc()).first()
    assert history.status == "failed"
    assert Property.query.count() == 1
    assert len(free_pass) == 1
    assert free_pass[0]["run_closed"] is True
