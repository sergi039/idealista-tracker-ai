"""The free pass is bounded and its last unlocked writer is not (#434, #352).

Two defects an adversarial review of #453 reproduced on 2026-08-20, both of
them older than the branch that surfaced them.

**Ingestion had no clock.** `enrich_property` opens a `lookup_budget` for the
whole run, but `services/property_imap_service.py` calls `enrich_free_sources`
directly and had none: with all three Overpass instances connecting and then
saying nothing, one listing cost **640 s** -- a 120 s coastline read, eight
60 s requests and eight 5 s gate waits. Each walk has its own 210 s ceiling
and three independent walks do not compose into a run budget.

**And the amenity writer could erase what another process had just measured.**
It read `enrichment`, spent seconds in Overpass, then committed the whole
column from the copy it loaded before the call. Reproduced against #437's new
block: session B stored a `hazards` measurement, session A finished its
amenity count, and the row came back with `infrastructure_extended` and no
`hazards`. Three of the four advisory writers had taken the row under
`FOR UPDATE` since #339; this one made that a three-quarters guarantee, and
CLAUDE.md described the gap rather than closing it.
"""

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.enrichment_service import EnrichmentService  # noqa: E402
from utils.http import lookup_deadline  # noqa: E402


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _property(**kw):
    row = Property(
        source_email_id=kw.pop("source_email_id", "clock"),
        title="Plot",
        location_lat=43.5,
        location_lon=-6.0,
        **kw,
    )
    db.session.add(row)
    db.session.commit()
    return row


class TestTheFreePassOpensItsOwnBudget:
    """The clock is opened where the pass is, not by whoever calls it."""

    def test_every_step_of_an_ingest_runs_under_a_deadline(self, app, monkeypatch):
        from services import property_enrichment_service as module

        seen = []

        service = module.PropertyEnrichmentService()
        monkeypatch.setattr(
            service.enrichment_service,
            "enrich_osm_amenities",
            lambda prop, *, commit=True: seen.append(("amenities", lookup_deadline())),
        )
        monkeypatch.setattr(
            service.quality_of_life_service,
            "enrich",
            lambda prop, commit=False: seen.append(("qol", lookup_deadline())),
        )
        monkeypatch.setattr(
            service.hazard_service,
            "enrich",
            lambda prop, commit=False: seen.append(("hazards", lookup_deadline())),
        )
        monkeypatch.setattr(
            service,
            "sea_view_calculator",
            lambda prop, commit=False, use_ai=True: seen.append(
                ("sea_view", lookup_deadline())
            ),
        )

        prop = _property()
        # The ingestion call, verbatim: no run around it.
        assert lookup_deadline() is None
        service.enrich_free_sources(prop, commit=True, use_ai=False)

        assert [name for name, _ in seen] == [
            "amenities",
            "qol",
            "hazards",
            "sea_view",
        ], seen
        for name, deadline in seen:
            assert deadline is not None, f"{name} ran with no deadline"
        # And it does not leak past the pass.
        assert lookup_deadline() is None

    def test_a_nested_pass_cannot_extend_the_run_that_contains_it(
        self, app, monkeypatch
    ):
        """`enrich_property` opens the run's budget and this pass opens its own
        inside it. The inner one must take the earlier deadline, or a run could
        buy itself more time simply by reaching its advisory steps."""
        from services import property_enrichment_service as module
        from utils.http import lookup_budget

        seen = []
        service = module.PropertyEnrichmentService()
        for owner, attr, name in (
            (service.enrichment_service, "enrich_osm_amenities", "amenities"),
            (service.quality_of_life_service, "enrich", "qol"),
            (service.hazard_service, "enrich", "hazards"),
        ):
            monkeypatch.setattr(
                owner,
                attr,
                lambda *a, _n=name, **kw: seen.append((_n, lookup_deadline())),
            )
        monkeypatch.setattr(
            service, "sea_view_calculator", lambda prop, commit=False, use_ai=True: None
        )

        prop = _property(source_email_id="nested")
        with lookup_budget(1.0) as outer:
            service.enrich_free_sources(prop, commit=True, use_ai=False)
        assert seen
        for name, deadline in seen:
            assert deadline is not None
            assert deadline <= outer + 0.01, (
                f"{name} was given more time than the run allows"
            )


class TestTheAmenityWriterHoldsItsRow:
    """The last unlocked writer of `enrichment` (#352)."""

    def test_it_does_not_erase_a_block_committed_during_its_lookup(
        self, app, monkeypatch
    ):
        """The reproduction, verbatim: another process commits while this
        writer is in Overpass, and the block it wrote must still be there."""
        service = EnrichmentService()
        prop = _property(
            source_email_id="race", enrichment={"existing": {"kept": True}}
        )
        prop_id = prop.id

        def _fetch(lat, lon):
            # Another process commits a measurement while we are "in Overpass".
            with Session(bind=db.engine) as other:
                row = other.get(Property, prop_id)
                enrichment = dict(row.enrichment or {})
                enrichment["hazards"] = {
                    "status": "ok",
                    "items": [{"name": "Cementos"}],
                }
                row.enrichment = enrichment
                flag_modified(row, "enrichment")
                other.commit()
            from services.enrichment_service import OsmAmenityReading

            return OsmAmenityReading(
                counts={"cafe": 1}, measured_at="2026-08-20T00:00:00Z"
            )

        monkeypatch.setattr(service, "_fetch_osm_amenities", _fetch)
        service.enrich_osm_amenities(prop, commit=True)

        db.session.expire_all()
        stored = db.session.get(Property, prop_id).enrichment or {}
        assert "hazards" in stored, sorted(stored.keys())
        assert stored["hazards"]["items"][0]["name"] == "Cementos"
        # ...and its own write landed as well.
        assert "infrastructure_extended" in stored, sorted(stored.keys())
        assert stored["existing"] == {"kept": True}

    def test_it_refuses_a_caller_that_cannot_commit_before_it_asks_overpass(
        self, app, monkeypatch
    ):
        """`services/enrichment_write.py`'s contract: validate before the
        measurement, so a write that could not persist costs a raise rather
        than a round trip."""
        from services.enrichment_write import EnrichmentWriteContractError

        service = EnrichmentService()
        prop = _property(source_email_id="dirty")
        asked = []
        monkeypatch.setattr(
            service, "_fetch_osm_amenities", lambda lat, lon: asked.append(1)
        )

        # Something else is pending in this session.
        db.session.add(Property(source_email_id="pending", title="Other"))
        with pytest.raises(EnrichmentWriteContractError):
            service.enrich_osm_amenities(prop, commit=True)
        assert asked == [], "Overpass was asked for a write that could not persist"
        db.session.rollback()

    def test_commit_false_still_takes_no_lock_and_makes_no_promise(
        self, app, monkeypatch
    ):
        """The mode `enrich_property`'s decisive pass uses. It must keep
        working on a session the caller owns."""
        service = EnrichmentService()
        prop = _property(source_email_id="shared")
        from services.enrichment_service import OsmAmenityReading

        monkeypatch.setattr(
            service,
            "_fetch_osm_amenities",
            lambda lat, lon: OsmAmenityReading(counts={"bar": 2}, measured_at="x"),
        )
        db.session.add(Property(source_email_id="also-pending", title="Other"))
        service.enrich_osm_amenities(prop, commit=False)
        assert "infrastructure_extended" in (prop.enrichment or {})
        db.session.rollback()


class TestOneUnreadableElementIsCounted:
    """A coordinate no `float()` can take is an unreadable element, not a
    crash that leaves the row reading "not scanned yet"."""

    def test_an_overflowing_latitude_is_not_an_exception(self):
        from services.hazard_service import _coordinate

        assert _coordinate(10**400, 90.0) is None
        assert _coordinate("nonsense", 90.0) is None
        assert _coordinate(43.5, 90.0) == 43.5
