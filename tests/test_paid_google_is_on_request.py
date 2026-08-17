"""Automatic Google spend is off: ingestion geocodes a new row and stops there.

Owner decision, 2026-08-17, after a billing overrun ("будем пересчитывать по
запросу"). One automatic enrichment was 6 preset Places Nearby lookups + 1 for
the beaches + a Distance Matrix request of ~26 elements: about $0.36 a listing,
fired twice a day, unattended, on *both* machines — the scheduler ran on the
Mac mini and on the laptop against the same mailbox, so the laptop paid a
second time into a throwaway dev database. On 2026-08-16 four new saved
searches delivered 306 listings there between 07:00 and 10:00, roughly $110 of
Google credit in one morning that nobody asked for and nobody read.

Travel was the only automatic paid caller in the app — everything else Google
is behind a button press or a CLI backfill — so `AUTO_TRAVEL_ENRICHMENT`
defaulting to false is what makes the unattended path free.

The half that is easy to get wrong, and is the reason this file exists rather
than a one-line default change: `calculate_for_property` opens with
`ensure_coordinates`, so travel was also what *geocoded* every new listing.
Switching it off would have taken the coordinate with it, and with the
coordinate the sea distance, the sea-view verdict, the OSM amenities and the
quality-of-life block — four free measurements lost to a flag about a paid
one, and a row that reads "nothing nearby" when the truth is "nobody looked".
That is #98's defect arriving through the back door of a cost control. So
geocoding is its own flag, still on, at $0.005 a listing.

Pinned here: the defaults themselves; that ingestion fires no Places/Distance
Matrix call by default; that it still geocodes exactly once and the free pass
sees the coordinate; that turning travel back on still works and does not
geocode twice; that `AUTO_GEOCODING=false` reaches no Google API at all; and
that a refused geocode neither fails ingestion nor loses the row.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from app import create_app, db
from config import Config
from models import Property, SearchProfile
from services.property_enrichment_service import PropertyEnrichmentService
from services.property_imap_service import PropertyIMAPService
from services.property_location_service import PropertyLocationService
from services.property_travel_service import PropertyTravelService
from tests import setup_test_environment

INTERNAL_DATE = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)

# Navia, Asturias — the coordinate the fake geocoder returns.
COORD_LAT = 43.5400
COORD_LON = -6.7200


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
def flags(monkeypatch):
    """Production defaults for the two flags under test, everything else off.

    Sea distance and the free pass are switched off here so this file asserts
    on the *paid* boundary alone; tests/test_issue_299_ingestion_free_enrichers
    owns the free pass. `monkeypatch.setattr` and not a bare assignment: three
    other modules in this suite assign `Config.AUTO_TRAVEL_ENRICHMENT = False`
    on the class and leak it into the rest of the session (see
    tests/conftest.py), and a test about a default must not inherit one.
    """
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
    monkeypatch.setattr(Config, "AUTO_GEOCODING", True)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", False)


class _Spy:
    """Records every call. A counter alone would not do here (#297): the
    ordering test needs to know what the free pass actually *saw*."""

    def __init__(self):
        self.calls = []

    @property
    def count(self):
        return len(self.calls)


@pytest.fixture
def travel_spy(monkeypatch):
    spy = _Spy()

    def fake_travel(self, prop, commit=False):
        spy.calls.append(prop.id)
        prop.location_lat = COORD_LAT
        prop.location_lon = COORD_LON
        prop.location_accuracy = "precise"
        if commit:
            db.session.commit()
        return True

    monkeypatch.setattr(PropertyTravelService, "calculate_for_property", fake_travel)
    return spy


@pytest.fixture
def geocode_spy(monkeypatch):
    """Stands in for the billed Geocoding call, at the service boundary.

    `ensure_coordinates` is the whole paid surface of this step: it is the
    method that reaches `GeocodingService`, and everything below it in
    `utils/geocoding.py` is transport.
    """
    spy = _Spy()

    def fake_ensure(self, prop, refresh=False):
        spy.calls.append(prop.id)
        prop.location_lat = COORD_LAT
        prop.location_lon = COORD_LON
        prop.location_accuracy = "precise"
        return True

    monkeypatch.setattr(PropertyLocationService, "ensure_coordinates", fake_ensure)
    return spy


@pytest.fixture
def free_pass_spy(monkeypatch):
    """Records the coordinates the free pass was handed, not just that it ran."""
    spy = _Spy()

    def fake_free(self, prop, *, commit, use_ai):
        spy.calls.append((prop.location_lat, prop.location_lon))

    monkeypatch.setattr(PropertyEnrichmentService, "enrich_free_sources", fake_free)
    return spy


def _profile():
    profile = SearchProfile(
        name="Default",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _listing_email(profile_id, idealista_id=990817):
    """The dict shape `get_idealista_emails()` produces for a listing email."""
    return {
        "type": "listing",
        "source_email_id": f"imap_budget_{idealista_id}",
        "email_received_at": INTERNAL_DATE,
        "email_subject": "New home in your search: Navia",
        "email_sender": "Idealista <noresponder@idealista.com>",
        "title": "Casa rural en Navia",
        "url": f"https://www.idealista.com/inmueble/{idealista_id}/",
        "idealista_property_id": idealista_id,
        "search_profile_id": profile_id,
        "deal_type": "sale",
        "price": 120000,
        "area": 800,
        "municipality": "Navia",
        "property_category": "housing",
        "property_subtype": "house",
    }


def _ingest(monkeypatch, emails):
    service = PropertyIMAPService()
    monkeypatch.setattr(
        service, "get_idealista_emails", lambda max_results=None: list(emails)
    )
    return service.run_ingestion(sync_type="test")


def test_the_defaults_themselves(monkeypatch):
    """Travel off, geocoding on — read from a clean interpreter.

    A subprocess and not `importlib.reload`: reloading `config` rebinds
    `config.Config` to a new class while every `from config import Config`
    already executed in this session keeps the old one, so a later test's
    `monkeypatch.setattr(Config, ...)` would patch an object the services no
    longer read. That divergence is silent and outlives the test.
    """
    env = {k: v for k, v in os.environ.items()}
    env.pop("AUTO_TRAVEL_ENRICHMENT", None)
    env.pop("AUTO_GEOCODING", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json;from config import Config;"
            "print(json.dumps({'travel': Config.AUTO_TRAVEL_ENRICHMENT,"
            "'geocoding': Config.AUTO_GEOCODING}))",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    defaults = json.loads(result.stdout.strip().splitlines()[-1])

    assert defaults["travel"] is False, (
        "AUTO_TRAVEL_ENRICHMENT must default to off: it is the only automatic "
        "caller of Google Places and Distance Matrix in this repository"
    )
    assert defaults["geocoding"] is True, (
        "AUTO_GEOCODING must default to on: without a coordinate the free "
        "enrichers have nothing to measure"
    )


def test_ingestion_fires_no_paid_travel_call(
    app, flags, travel_spy, geocode_spy, monkeypatch
):
    with app.app_context():
        profile = _profile()
        created = _ingest(monkeypatch, [_listing_email(profile.id)])

        assert created == 1
        assert travel_spy.count == 0, (
            "ingestion asked Google for Places/Distance Matrix; that is "
            f"~$0.36 per listing, unattended (calls: {travel_spy.calls})"
        )


def test_ingestion_still_geocodes_the_new_row_exactly_once(
    app, flags, travel_spy, geocode_spy, monkeypatch
):
    with app.app_context():
        profile = _profile()
        created = _ingest(monkeypatch, [_listing_email(profile.id)])

        assert created == 1
        assert geocode_spy.count == 1

        prop = Property.query.one()
        assert float(prop.location_lat) == pytest.approx(COORD_LAT)
        assert float(prop.location_lon) == pytest.approx(COORD_LON)


def test_the_free_pass_sees_the_coordinate_the_geocode_wrote(
    app, flags, travel_spy, geocode_spy, free_pass_spy, monkeypatch
):
    """Order matters, and only a spy that records its argument can prove it.

    The geocode step is placed before the free pass for one reason: OSM
    amenities, quality of life and the sea-view verdict all key on the
    coordinate. A free pass that runs first records "no coordinates" for a row
    that is about to have one.
    """
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", True)

    with app.app_context():
        profile = _profile()
        _ingest(monkeypatch, [_listing_email(profile.id)])

        assert free_pass_spy.count == 1
        lat, lon = free_pass_spy.calls[0]
        assert lat is not None and lon is not None, (
            "the free pass ran before the row was geocoded, so every free "
            "measurement records an honest gap for a locatable listing"
        )
        assert float(lat) == pytest.approx(COORD_LAT)


def test_travel_still_runs_when_the_owner_turns_it_on(
    app, flags, travel_spy, geocode_spy, monkeypatch
):
    """The flag is a switch, not a removal — and it geocodes once, not twice.

    `calculate_for_property` geocodes on its way to Google, so the ingestion
    path must not add a second `ensure_coordinates` beside it.
    """
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", True)

    with app.app_context():
        profile = _profile()
        created = _ingest(monkeypatch, [_listing_email(profile.id)])

        assert created == 1
        assert travel_spy.count == 1
        assert geocode_spy.count == 0, (
            "the travel step already geocoded this row; a second call here "
            "would be a second billed geocode per listing"
        )


def test_auto_geocoding_false_reaches_no_google_api_at_all(
    app, flags, travel_spy, geocode_spy, monkeypatch
):
    monkeypatch.setattr(Config, "AUTO_GEOCODING", False)

    with app.app_context():
        profile = _profile()
        created = _ingest(monkeypatch, [_listing_email(profile.id)])

        assert created == 1
        assert travel_spy.count == 0
        assert geocode_spy.count == 0


def test_a_refused_geocode_neither_fails_ingestion_nor_loses_the_row(
    app, flags, travel_spy, monkeypatch
):
    """A geocoder that raises must not take the listing down with it.

    Every other enrichment step at ingestion carries this contract; the new
    one has to as well, or a Google outage stops the mailbox being read.
    """

    def exploding_ensure(self, prop, refresh=False):
        raise RuntimeError("Google returned 500")

    monkeypatch.setattr(PropertyLocationService, "ensure_coordinates", exploding_ensure)

    with app.app_context():
        profile = _profile()
        created = _ingest(monkeypatch, [_listing_email(profile.id)])

        assert created == 1
        prop = Property.query.one()
        assert prop.idealista_property_id == 990817
        assert prop.location_lat is None
