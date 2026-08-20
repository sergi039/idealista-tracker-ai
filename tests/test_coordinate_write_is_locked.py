"""The geocode writes the coordinate with the row held, not the copy it loaded.

Issue #400 — #339's defect one column over, in the scalars.

`ensure_coordinates` spends minutes on external calls and then writes
`location_lat`, `location_lon` and `location_accuracy` from the row its own
session loaded before them. Measured on the mini, property 733, 2026-08-17:

* ~15:32 an Enrich chain starts with `refresh_coords`, geocodes, and blocks on
  Overpass (which was not opening sockets at all — 60 s connect timeout, four
  retries per call);
* 15:36 the operator concludes the request died, writes the portal's own
  coordinate back with a provenance record, and commits;
* 15:44 Overpass recovers, the chain finishes and commits **its** view. The
  coordinate is Google's again and the provenance block is gone without trace.

The fix is `services/enrichment_write.py`'s rule, which names no column and now
has a scalar caller: validate before the measurement, geocode unlocked, then
lock, re-read, decide and write. The re-reading is the half that matters here
and the half a lock alone would not give — the even-trade comparison rests on
`portal_coordinate(prop)` and the row's own accuracy, both of which the old
code captured before the network call and used after it.

**What is not proved here**, exactly as `tests/test_enrichment_writers_lock.py`
records for the column next door: the suite runs one in-memory SQLite
connection, and SQLAlchemy's SQLite dialect drops `FOR UPDATE` silently rather
than raising — `db.session.refresh(obj, with_for_update=True)` emits a bare
`SELECT`. So no two-process race is staged. What is proved is the half that
actually failed: the stored row changes underneath the session, and the writer
has to see it.
"""

import pytest
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property  # noqa: E402
from services.enrichment_write import EnrichmentWriteContractError  # noqa: E402
from services.property_location_service import PropertyLocationService  # noqa: E402

PIN_LAT = "43.5551000"
PIN_LON = "-5.9333000"


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
def prop(app):
    """A row with a portal pin and nothing located, the shape #393 defends."""
    row = Property(
        source_email_id="lock_400",
        title="Land for sale in Llaranes, Avilés",
        municipality="Avilés",
        location_accuracy="unknown",
        enrichment={
            "import": {
                "coordinate": {"source": "fotocasa", "lat": PIN_LAT, "lon": PIN_LON}
            }
        },
    )
    db.session.add(row)
    db.session.commit()
    return row


class _Geocoder:
    """Answers once, and lets a scenario commit something mid-call.

    The `during` hook stands in for the operator's 15:36 commit: it runs while
    the chain is still inside the geocode, which is the only moment at which
    the incident is reproducible at all.
    """

    def __init__(self, answer, during=None):
        self.answer = answer
        self.during = during
        self.calls = 0

    def geocode_address(self, query):
        self.calls += 1
        if self.during is not None and self.calls == 1:
            self.during()
        return dict(self.answer, query=query)


def _service(answer, during=None):
    service = PropertyLocationService()
    service.geocoding_service = _Geocoder(answer, during)
    return service


def _operator_writes_the_pin():
    """A concurrent committer, the way this suite already stages one.

    A raw `UPDATE` rather than the ORM: `session.execute` does not expire the
    identity map, so the in-memory instance keeps the stale values and only a
    refresh can see this — which is the point.
    """
    db.session.execute(
        text(
            "UPDATE properties SET location_lat = :lat, location_lon = :lon, "
            "location_accuracy = 'precise' WHERE source_email_id = 'lock_400'"
        ),
        {"lat": PIN_LAT, "lon": PIN_LON},
    )
    db.session.commit()


class TestTheIncident:
    def test_a_coordinate_written_while_the_geocode_ran_is_not_overwritten(
        self, app, prop
    ):
        """Property 733, staged.

        Before the call the row says `unknown`, so a `precise` answer improves
        on it and would be written. While the call runs, the operator commits a
        `precise` coordinate of their own. Re-read under the lock, the answer no
        longer improves on anything — and the row keeps what the operator
        wrote.

        Without the re-derivation this test writes Google's coordinate: the
        comparison would still be against the `unknown` captured before the
        call.
        """
        service = _service(
            {
                "lat": 43.6,
                "lng": -5.8,
                "accuracy": "precise",
                "formatted_address": "Llaranes, Avilés",
            },
            during=_operator_writes_the_pin,
        )

        assert service.ensure_coordinates(prop, refresh=True, commit=True) is True

        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert str(stored.location_lat) == PIN_LAT
        assert str(stored.location_lon) == PIN_LON
        record = stored.enrichment["geocoding"]
        assert record["kept"] == "fotocasa coordinate"
        assert record["answered_accuracy"] == "precise"

    def test_the_answer_is_written_when_nothing_raced_it(self, app, prop):
        """The control. The same call, with no concurrent commit, still writes
        Google's coordinate — so the test above is about the race and not about
        the geocode being refused."""
        service = _service(
            {
                "lat": 43.6,
                "lng": -5.8,
                "accuracy": "precise",
                "formatted_address": "Llaranes, Avilés",
            }
        )

        assert service.ensure_coordinates(prop, refresh=True, commit=True) is True

        db.session.expire_all()
        stored = db.session.get(Property, prop.id)
        assert float(stored.location_lat) == pytest.approx(43.6)
        assert stored.location_accuracy == "precise"


class TestTheContract:
    def test_the_row_is_read_for_update(self, app, prop, monkeypatch):
        seen = []
        original = db.session.refresh

        def spy(obj, *args, **kwargs):
            seen.append(kwargs.get("with_for_update"))
            return original(obj, *args, **kwargs)

        monkeypatch.setattr(db.session, "refresh", spy)
        _service({"lat": 43.6, "lng": -5.8, "accuracy": "precise"}).ensure_coordinates(
            prop, refresh=True, commit=True
        )

        assert True in seen

    def test_commit_false_takes_no_lock(self, app, prop, monkeypatch):
        """The module's own contract: a caller that owns the transaction gets
        no lock, because holding one across an end this code cannot see is
        worse than the race."""
        seen = []
        original = db.session.refresh

        def spy(obj, *args, **kwargs):
            seen.append(kwargs.get("with_for_update"))
            return original(obj, *args, **kwargs)

        monkeypatch.setattr(db.session, "refresh", spy)
        _service({"lat": 43.6, "lng": -5.8, "accuracy": "precise"}).ensure_coordinates(
            prop, refresh=True
        )

        assert True not in seen

    def test_the_lock_is_taken_after_the_geocode_not_before(self, app, prop):
        """#196's cost, refused again. A row held across the network calls is
        the trade this repository has already declined once."""
        order = []
        original = db.session.refresh

        def spy(obj, *args, **kwargs):
            if kwargs.get("with_for_update"):
                order.append("lock")
            return original(obj, *args, **kwargs)

        service = PropertyLocationService()

        class _Recording:
            def geocode_address(self, query):
                order.append("geocode")
                return {"lat": 43.6, "lng": -5.8, "accuracy": "precise", "query": query}

        service.geocoding_service = _Recording()
        db.session.refresh = spy
        try:
            service.ensure_coordinates(prop, refresh=True, commit=True)
        finally:
            del db.session.refresh

        assert order.index("geocode") < order.index("lock"), order

    def test_a_dirty_session_is_refused(self, app, prop):
        """And this is why the chain calls it first — see
        `PropertyEnrichmentService.enrich_property`."""
        prop.title = "touched"

        with pytest.raises(EnrichmentWriteContractError):
            _service(
                {"lat": 43.6, "lng": -5.8, "accuracy": "precise"}
            ).ensure_coordinates(prop, refresh=True, commit=True)


class TestTheBehaviourThatDidNotChange:
    def test_a_refresh_that_answers_nothing_still_unlocates_a_row_with_no_pin(
        self, app
    ):
        """Only a portal pin is defended (#393). A row without one is left
        unlocated by a refresh that found nothing — the clearing simply moved
        into the locked tail, because doing it eagerly made the refresh
        autoflush this run's own `None`s and read them back as the fresh row.
        """
        row = Property(
            source_email_id="lock_400_nopin",
            title="Finca Offers For",
            municipality="Avilés",
            location_lat=43.1,
            location_lon=-5.1,
            location_accuracy="approximate",
        )
        db.session.add(row)
        db.session.commit()

        service = PropertyLocationService()

        class _Silent:
            def geocode_address(self, query):
                return None

        service.geocoding_service = _Silent()

        assert service.ensure_coordinates(row, refresh=True, commit=True) is False

        db.session.expire_all()
        stored = db.session.get(Property, row.id)
        assert stored.location_lat is None
        assert stored.location_accuracy == "unknown"


class TestTheChainAsksForIt:
    """`enrich_property` is where the incident happened, so the wiring is
    pinned there and not only in the service."""

    def test_the_coordinate_step_commits_and_runs_before_anything_dirties_the_session(
        self, app, prop, monkeypatch
    ):
        """Both halves of one decision.

        `check_writable` refuses a `commit=True` write on a session with
        anything pending, and `advertiser.enrich(commit=False)` assigns
        `prop.enrichment` on a row whose seller nothing has established — every
        fresh fotocasa import, which is exactly the population that needs a
        coordinate. So the order is load-bearing, not tidiness.
        """
        from services import property_enrichment_service as module

        order = []

        class _Location:
            def ensure_coordinates(self, prop, refresh=False, *, commit=False):
                order.append(("coordinates", commit))
                return False

        def _advertiser(prop, *, commit=False):
            order.append(("advertiser", commit))
            return {}

        service = module.PropertyEnrichmentService()
        service.location_service = _Location()
        monkeypatch.setattr(module.advertiser, "enrich", _advertiser)
        service.enrich_property(prop, recalc_scoring=False)

        assert order[0] == ("coordinates", True), order
        assert ("advertiser", False) in order
        assert order.index(("coordinates", True)) < order.index(
            ("advertiser", False)
        ), order
