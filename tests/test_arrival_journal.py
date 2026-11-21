"""A listing a second saved search delivers leaves a record, and nothing else.

Issue #632. A portal listing is one row, with one `search_profile_id`; a
second saved search that delivers it used to find the row and move on without
a trace. `services/arrival_journal.py` now appends one entry to
`enrichment["arrivals"]` and touches nothing else. What these tests pin:

* the entry: the subscription after `canonical_profile()`, the portal, the
  door, the email's id, two UTC times;
* what must not move: `search_profile_id`, the row count, every other
  `enrichment` block, and no listing page is fetched for a stored listing;
* the refusals: the row's own search, an email no profile claimed, the same
  email twice, a full journal;
* the write discipline: the journal is re-read from the stored row, not from
  the copy in memory (the #339 shape), and a pending session is refused;
* all four doors: fotocasa, milanuncios and yaencontre mail, the late
  duplicate, and the paste-links confirm.

Not proved here: two processes racing for one row. The suite is one SQLite
connection and SQLite ignores `FOR UPDATE`; what is proved is the half that
can fail silently -- the stored row changes underneath the session and the
writer has to see it.
"""

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from config import Config  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from services import (  # noqa: E402
    arrival_journal,
    fotocasa_import,
    fotocasa_source,
    milanuncios_source,
    yaencontre_source,
)
from services.enrichment_write import EnrichmentWriteContractError  # noqa: E402
from services.fotocasa_source import parse_listing  # noqa: E402
from services.property_imap_service import PropertyIMAPService  # noqa: E402

DATA = pathlib.Path(__file__).parent / "data"
FOTOCASA_URL = "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d"
FOTOCASA_PAGE = DATA / "fotocasa_listing_190280914.html"
MA_URL = (
    "https://www.milanuncios.com/venta-de-chalets-en-los-quintanales-(mieres)"
    "-asturias/los-quintanales-aldea-los-quintanales-612329827.htm"
)
MA_PAGE = DATA / "milanuncios_listing_612329827.html"
RECEIVED = datetime(2026, 11, 20, 9, 15, tzinfo=timezone.utc)


def _profile(name: str, **fields) -> SearchProfile:
    profile = SearchProfile(
        name=name,
        is_active=True,
        is_default=fields.pop("is_default", False),
        travel_targets={"presets": {}, "custom": []},
        **fields,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def app(monkeypatch):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    monkeypatch.setattr(Config, "FREE_ENRICHMENT_ENABLED", False)
    with app.app_context():
        db.create_all()
        _profile("Default", is_default=True)
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def home(app):
    return _profile("Galicia costa")


@pytest.fixture
def other(app):
    return _profile("Asturias")


def _prop(profile: SearchProfile, source_email_id="fotocasa:1", **fields) -> Property:
    prop = Property(
        source_email_id=source_email_id,
        title="Arrival fixture",
        search_profile_id=profile.id,
        **fields,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _record(prop, profile, ref="imap_7", **overrides):
    arguments = {
        "profile_id": profile.id if profile is not None else None,
        "source": "fotocasa",
        "method": arrival_journal.METHOD_ALERT_EMAIL,
        "ref": ref,
        "received_at": RECEIVED,
    }
    arguments.update(overrides)
    return arrival_journal.record(prop, **arguments)


def _stored(prop_id: int) -> dict:
    """The column as the database holds it, past the session's own copy."""
    raw = db.session.execute(
        text("SELECT enrichment FROM properties WHERE id = :i"), {"i": prop_id}
    ).scalar_one()
    db.session.rollback()
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


def _stored_raw(prop_id: int):
    """The column whatever JSON type it holds."""
    raw = db.session.execute(
        text("SELECT enrichment FROM properties WHERE id = :i"), {"i": prop_id}
    ).scalar_one()
    db.session.rollback()
    return json.loads(raw) if isinstance(raw, str) else raw


def _items(prop_id: int) -> list:
    return (_stored(prop_id).get("arrivals") or {}).get("items") or []


class TestTheEntry:
    def test_another_search_leaves_one_entry_saying_who_and_when(
        self, app, home, other
    ):
        prop = _prop(home)

        assert _record(prop, other) == arrival_journal.RECORDED

        (entry,) = _items(prop.id)
        assert entry["profile_id"] == other.id
        assert entry["source"] == "fotocasa"
        assert entry["method"] == "alert_email"
        assert entry["ref"] == "imap_7"
        assert entry["received_at"] == "2026-11-20T09:15:00+00:00"
        recorded = datetime.fromisoformat(entry["recorded_at"])
        assert recorded.utcoffset() == timedelta(0)
        assert _stored(prop.id)["arrivals"]["truncated"] is False

    def test_the_rows_own_subscription_is_not_changed(self, app, home, other):
        prop = _prop(home)

        _record(prop, other)

        db.session.expire_all()
        assert db.session.get(Property, prop.id).search_profile_id == home.id

    def test_the_other_blocks_of_the_column_survive(self, app, home, other):
        prop = _prop(home, enrichment={"import": {"source": "fotocasa"}, "sea": 1})

        _record(prop, other)

        stored = _stored(prop.id)
        assert stored["import"] == {"source": "fotocasa"}
        assert stored["sea"] == 1

    def test_a_time_in_another_zone_is_stored_as_utc(self, app, home, other):
        prop = _prop(home)
        madrid = timezone(timedelta(hours=1))

        _record(prop, other, received_at=datetime(2026, 11, 20, 10, 15, tzinfo=madrid))

        assert _items(prop.id)[0]["received_at"] == "2026-11-20T09:15:00+00:00"

    def test_a_time_without_a_zone_is_not_given_one(self, app, home, other):
        prop = _prop(home)

        _record(prop, other, received_at=datetime(2026, 11, 20, 10, 15))

        assert _items(prop.id)[0]["received_at"] == "2026-11-20T10:15:00"

    def test_a_missing_time_stays_missing(self, app, home, other):
        prop = _prop(home)

        _record(prop, other, received_at=None)

        assert _items(prop.id)[0]["received_at"] is None


class TestOnlyAnotherSearch:
    def test_the_rows_own_search_records_nothing(self, app, home):
        prop = _prop(home)

        assert _record(prop, home) == arrival_journal.SAME_SEARCH
        assert "arrivals" not in _stored(prop.id)

    def test_a_stub_routed_onto_the_home_is_the_same_search(self, app, home):
        stub = _profile("Galicia Rias Altas", routed_to=home.id, is_hidden=True)
        prop = _prop(home)

        assert _record(prop, stub) == arrival_journal.SAME_SEARCH
        assert "arrivals" not in _stored(prop.id)

    def test_a_stub_is_recorded_as_the_subscription_it_routes_to(
        self, app, home, other
    ):
        stub = _profile("Asturias oriente", routed_to=other.id, is_hidden=True)
        prop = _prop(home)

        assert _record(prop, stub) == arrival_journal.RECORDED
        assert _items(prop.id)[0]["profile_id"] == other.id

    def test_an_email_no_profile_claimed_records_nothing(self, app, home):
        prop = _prop(home)

        assert _record(prop, None) == arrival_journal.NO_PROFILE
        assert _record(prop, None, profile_id=987654) == arrival_journal.NO_PROFILE
        assert "arrivals" not in _stored(prop.id)


class TestIdempotentAndBounded:
    def test_the_same_email_twice_is_one_entry(self, app, home, other):
        prop = _prop(home)

        assert _record(prop, other) == arrival_journal.RECORDED
        assert _record(prop, other) == arrival_journal.ALREADY_RECORDED

        assert len(_items(prop.id)) == 1

    def test_another_email_and_another_search_are_their_own_entries(
        self, app, home, other
    ):
        third = _profile("Cantabria")
        prop = _prop(home)

        _record(prop, other, ref="imap_7")
        _record(prop, other, ref="imap_8")
        _record(prop, third, ref="imap_9")
        # The same id from another portal is another delivery.
        _record(prop, third, ref="imap_9", source="milanuncios")

        assert [(i["profile_id"], i["source"], i["ref"]) for i in _items(prop.id)] == [
            (other.id, "fotocasa", "imap_7"),
            (other.id, "fotocasa", "imap_8"),
            (third.id, "fotocasa", "imap_9"),
            (third.id, "milanuncios", "imap_9"),
        ]

    def test_a_replay_after_the_subscription_was_routed_is_still_one_delivery(
        self, app, home, other
    ):
        """Review of #698: the key is the delivery, never the subscription.

        `imap_7` arrives through `other`; `other` is then routed onto a third
        subscription; the same UID is read again. It resolves to the third
        subscription now, and it is still the email already recorded.
        """
        third = _profile("Cantabria")
        prop = _prop(home)
        assert _record(prop, other) == arrival_journal.RECORDED

        other.routed_to = third.id
        db.session.commit()

        assert _record(prop, other) == arrival_journal.ALREADY_RECORDED
        assert [(i["profile_id"], i["ref"]) for i in _items(prop.id)] == [
            (other.id, "imap_7")
        ]

    def test_a_full_journal_says_truncated_and_stops_growing(
        self, app, home, other, monkeypatch
    ):
        monkeypatch.setattr(arrival_journal, "MAX_ITEMS", 2)
        prop = _prop(home)

        assert _record(prop, other, ref="imap_1") == arrival_journal.RECORDED
        assert _record(prop, other, ref="imap_2") == arrival_journal.RECORDED
        assert _stored(prop.id)["arrivals"]["truncated"] is False

        assert _record(prop, other, ref="imap_3") == arrival_journal.TRUNCATED
        assert _record(prop, other, ref="imap_4") == arrival_journal.TRUNCATED

        stored = _stored(prop.id)["arrivals"]
        assert [i["ref"] for i in stored["items"]] == ["imap_1", "imap_2"]
        assert stored["truncated"] is True
        # An email already in a full journal is still recognised as recorded.
        assert _record(prop, other, ref="imap_1") == arrival_journal.ALREADY_RECORDED


class TestTheWriteReadsTheStoredRow:
    def _behind_the_session(self, prop, payload):
        """Commit straight to the row, as another process would.

        `session.execute` does not expire the identity map, so `prop` goes on
        returning the `enrichment` it loaded earlier -- the stale copy. No
        commit here: a commit expires `prop`, the next read reloads it, and
        the test then passes with the locked re-read removed (measured).
        """
        db.session.execute(
            text("UPDATE properties SET enrichment = :e WHERE id = :i"),
            {"e": json.dumps(payload), "i": prop.id},
        )

    def test_a_block_another_process_wrote_meanwhile_is_kept(self, app, home, other):
        prop = _prop(home, enrichment={"import": {"source": "fotocasa"}})
        assert prop.enrichment == {"import": {"source": "fotocasa"}}
        self._behind_the_session(
            prop, {"import": {"source": "fotocasa"}, "pool": {"status": "ok"}}
        )

        assert _record(prop, other) == arrival_journal.RECORDED

        stored = _stored(prop.id)
        assert stored["pool"] == {"status": "ok"}
        assert len(stored["arrivals"]["items"]) == 1

    def test_an_entry_another_run_stored_meanwhile_is_not_repeated(
        self, app, home, other
    ):
        prop = _prop(home)
        assert prop.enrichment is None
        theirs = {
            "profile_id": other.id,
            "source": "fotocasa",
            "method": "alert_email",
            "ref": "imap_7",
            "received_at": None,
            "recorded_at": "2026-11-20T09:16:00+00:00",
        }
        self._behind_the_session(
            prop, {"arrivals": {"items": [theirs], "truncated": False}}
        )

        assert _record(prop, other) == arrival_journal.ALREADY_RECORDED

        assert _items(prop.id) == [theirs]

    def test_a_home_that_moved_meanwhile_is_the_same_search(self, app, home, other):
        prop = _prop(home)
        assert prop.search_profile_id == home.id
        other_id = other.id
        db.session.execute(
            text("UPDATE properties SET search_profile_id = :p WHERE id = :i"),
            {"p": other_id, "i": prop.id},
        )
        assert prop.search_profile_id == home.id  # still the stale copy

        assert _record(prop, other) == arrival_journal.SAME_SEARCH
        assert "arrivals" not in _stored(prop.id)

    def test_a_session_with_work_pending_is_refused_not_committed(
        self, app, home, other
    ):
        prop = _prop(home)
        # Loaded first, so nothing below autoflushes the edit away before the
        # guard looks: a commit expires every attribute, and re-reading one
        # would flush the pending change and leave the session looking clean.
        assert (prop.search_profile_id, other.routed_to) == (home.id, None)
        prop.title = "an unrelated edit somebody has not committed"

        with pytest.raises(EnrichmentWriteContractError):
            _record(prop, other)

        db.session.rollback()
        assert "arrivals" not in _stored(prop.id)

    def test_a_home_the_listing_has_left_is_another_search_again(
        self, app, home, other
    ):
        """Review of #698, the inverse of the test above: the session still
        holds home A, the stored row has moved to B, and A's alert arrives.
        Judged from the copy that is "the same search" and the arrival is
        lost; judged from the locked row it is a second search."""
        prop = _prop(home)
        home_id, other_id = home.id, other.id
        assert prop.search_profile_id == home_id
        db.session.execute(
            text("UPDATE properties SET search_profile_id = :p WHERE id = :i"),
            {"p": other_id, "i": prop.id},
        )
        assert prop.search_profile_id == home_id  # still the stale copy

        assert _record(prop, home) == arrival_journal.RECORDED
        assert [i["profile_id"] for i in _items(prop.id)] == [home_id]

    def test_a_route_set_meanwhile_is_the_route_that_counts(self, app, home, other):
        """The subscription is re-read too: it was routed onto the home behind
        this session, so its alert is the home's own search now."""
        prop = _prop(home)
        assert other.routed_to is None
        db.session.execute(
            text("UPDATE search_profiles SET routed_to = :h WHERE id = :o"),
            {"h": home.id, "o": other.id},
        )
        assert other.routed_to is None  # still the stale copy

        assert _record(prop, other) == arrival_journal.SAME_SEARCH
        assert "arrivals" not in _stored(prop.id)

    def test_an_edit_pending_elsewhere_is_refused_before_any_lookup(
        self, app, home, other
    ):
        """Review of #698: the guard runs before the first query.

        An unrelated row carries an uncommitted edit and the subscription is
        not loaded. Looking the subscription up autoflushes the edit, after
        which the session looks clean and the journal's commit persists it.
        """
        prop = _prop(home)
        unrelated = _prop(home, source_email_id="fotocasa:2")
        unrelated_id, other_id = unrelated.id, other.id
        assert (prop.search_profile_id, unrelated.title) == (home.id, "Arrival fixture")
        db.session.expire(other)
        unrelated.title = "an edit nobody committed"

        with pytest.raises(EnrichmentWriteContractError):
            arrival_journal.record(
                prop,
                profile_id=other_id,
                source="fotocasa",
                method=arrival_journal.METHOD_ALERT_EMAIL,
                ref="imap_7",
            )

        db.session.rollback()
        assert "arrivals" not in _stored(prop.id)
        assert db.session.get(Property, unrelated_id).title == "Arrival fixture"


class TestReadingIsTotalAndWritingFailsClosed:
    def test_any_stored_value_reads_without_raising(self, app):
        empty = {"items": [], "truncated": False}
        for value in (["legacy"], "text", 7, {"arrivals": []}, {"arrivals": "x"}):
            assert arrival_journal.read(Property(enrichment=value)) == {
                **empty,
                "readable": False,
            }
        assert arrival_journal.read(
            Property(enrichment={"arrivals": {"items": "x"}})
        ) == {**empty, "readable": False}
        for value in (None, {}, {"sea": 1}):
            assert arrival_journal.read(Property(enrichment=value)) == {
                **empty,
                "readable": True,
            }

    def test_only_well_formed_entries_are_handed_to_a_reader(self, app):
        good = {"profile_id": 3, "source": "fotocasa", "ref": "imap_1"}
        block = {"items": [1, "a", {}, {"profile_id": True}, good], "truncated": True}

        assert arrival_journal.read(Property(enrichment={"arrivals": block})) == {
            "items": [good],
            "truncated": True,
            "readable": True,
        }

    @pytest.mark.parametrize(
        "stored", [["legacy"], {"arrivals": ["legacy"]}, {"arrivals": {"items": 1}}]
    )
    def test_a_value_it_cannot_read_is_never_written_over(
        self, app, home, other, stored
    ):
        prop = _prop(home)
        db.session.execute(
            text("UPDATE properties SET enrichment = :e WHERE id = :i"),
            {"e": json.dumps(stored), "i": prop.id},
        )
        db.session.commit()

        assert _record(prop, other) == arrival_journal.UNWRITABLE
        assert _stored_raw(prop.id) == stored

    def test_odd_stored_entries_are_kept_when_one_is_added(self, app, home, other):
        prop = _prop(home, enrichment={"arrivals": {"items": [1, {"note": "x"}]}})

        assert _record(prop, other) == arrival_journal.RECORDED

        items = _stored(prop.id)["arrivals"]["items"]
        assert items[:2] == [1, {"note": "x"}]
        assert items[2]["profile_id"] == other.id


def _service() -> PropertyIMAPService:
    service = PropertyIMAPService()
    service._portal_refusals = {}
    service._deferred_enrichment = []
    service.portal_price_updated_count = 0
    return service


def _email(profile, uid=41) -> dict:
    return {
        "uid": uid,
        "source_email_id": f"imap_{uid}",
        "email_received_at": RECEIVED,
        "email_subject": "an alert",
        "email_sender": "portal <alerts@example.com>",
        "search_profile_id": profile.id if profile is not None else None,
    }


def _no_fetch(*args, **kwargs):  # pragma: no cover - must not run
    raise AssertionError("a stored listing must cost no request")


class TestThePortalMailDoors:
    def test_fotocasa_records_the_second_search_without_a_fetch(
        self, app, home, other, monkeypatch
    ):
        listing = parse_listing(FOTOCASA_PAGE.read_text(encoding="utf-8"), FOTOCASA_URL)
        fotocasa_import.insert_rows(
            [fotocasa_import.preview_row(listing)], profile_id=home.id
        )
        prop_id = Property.query.one().id
        monkeypatch.setattr(fotocasa_source, "fetch_listing", _no_fetch)

        created, all_done = _service()._ingest_fotocasa_email(
            {**_email(other), "urls": [FOTOCASA_URL]},
            sale_only=False,
            excluded_categories=set(),
        )

        assert (created, all_done) == (0, True)
        assert Property.query.count() == 1
        (entry,) = _items(prop_id)
        assert entry["profile_id"] == other.id
        assert entry["source"] == "fotocasa"
        assert entry["method"] == "alert_email"
        assert entry["ref"] == "imap_41"
        assert entry["received_at"] == "2026-11-20T09:15:00+00:00"
        assert db.session.get(Property, prop_id).search_profile_id == home.id

    def test_milanuncios_records_the_second_search_without_a_page_fetch(
        self, app, home, other, monkeypatch
    ):
        row = milanuncios_source.parse_listing(MA_PAGE.read_text(), MA_URL)
        prop = fotocasa_import.build_property(
            row, profile_id=home.id, source=milanuncios_source.SOURCE_NAME
        )
        db.session.add(prop)
        db.session.commit()
        prop_id = prop.id
        monkeypatch.setattr(
            milanuncios_source, "resolve_tracker", lambda url, session=None: MA_URL
        )
        monkeypatch.setattr(milanuncios_source, "fetch_listing", _no_fetch)

        created, all_done = _service()._ingest_milanuncios_email(
            {**_email(other), "tracker_urls": ["https://click.example/t/1"]},
            sale_only=False,
            excluded_categories=set(),
        )

        assert (created, all_done) == (0, True)
        (entry,) = _items(prop_id)
        assert (entry["profile_id"], entry["source"], entry["ref"]) == (
            other.id,
            "milanuncios",
            "imap_41",
        )

    def _yaencontre_card(self, price=180000.0) -> dict:
        return {
            "listing_id": 112395195,
            "url": "https://www.yaencontre.com/venta/casa/inmueble-75866-112395195",
            "title": "Casa adosada en venta en avenida Compostela, Outes",
            "price": price,
            "area": 294.0,
            "municipality": "Outes",
        }

    def _stored_yaencontre(self, home) -> int:
        service = _service()
        assert service._create_portal_row(
            self._yaencontre_card(),
            source=yaencontre_source.SOURCE_NAME,
            email_data=_email(home, uid=40),
            sale_only=False,
            excluded_categories=set(),
            record_advertiser=False,
        )
        return Property.query.one().id

    def test_yaencontre_records_the_second_search(self, app, home, other):
        prop_id = self._stored_yaencontre(home)

        created, all_done = _service()._ingest_yaencontre_email(
            {**_email(other), "cards": [self._yaencontre_card()]},
            sale_only=False,
            excluded_categories=set(),
        )

        assert (created, all_done) == (0, True)
        assert Property.query.count() == 1
        (entry,) = _items(prop_id)
        assert (entry["profile_id"], entry["source"], entry["ref"]) == (
            other.id,
            "yaencontre",
            "imap_41",
        )

    def test_yaencontre_records_it_beside_a_price_change(self, app, home, other):
        prop_id = self._stored_yaencontre(home)

        _service()._ingest_yaencontre_email(
            {**_email(other), "cards": [self._yaencontre_card(price=170000.0)]},
            sale_only=False,
            excluded_categories=set(),
        )

        db.session.expire_all()
        prop = db.session.get(Property, prop_id)
        assert float(prop.price) == 170000.0
        assert float(prop.previous_price) == 180000.0
        assert len(_items(prop_id)) == 1
        assert prop.search_profile_id == home.id

    def test_a_listing_a_concurrent_writer_stored_first_is_recorded_too(
        self, app, home, other, monkeypatch
    ):
        """The late duplicate: the dedup check saw nothing, the insert collided."""
        prop_id = self._stored_yaencontre(home)

        landed = _service()._create_portal_row(
            self._yaencontre_card(),
            source=yaencontre_source.SOURCE_NAME,
            email_data=_email(other),
            sale_only=False,
            excluded_categories=set(),
            record_advertiser=False,
        )

        assert landed is False
        assert Property.query.count() == 1
        (entry,) = _items(prop_id)
        assert (entry["profile_id"], entry["ref"]) == (other.id, "imap_41")


class TestThePasteLinksDoor:
    def _stored(self, home) -> Property:
        listing = parse_listing(FOTOCASA_PAGE.read_text(encoding="utf-8"), FOTOCASA_URL)
        fotocasa_import.insert_rows(
            [fotocasa_import.preview_row(listing)], profile_id=home.id
        )
        return Property.query.one()

    def _duplicate_row(self, prop) -> dict:
        return {
            "url": FOTOCASA_URL,
            "listing_id": 190280914,
            "status": fotocasa_import.STATUS_DUPLICATE,
            "reason": None,
            "existing_id": prop.id,
            "title": prop.title,
        }

    def test_a_link_already_stored_is_journaled_for_the_other_subscription(
        self, app, home, other
    ):
        prop = self._stored(home)
        prop_id = prop.id

        outcome = fotocasa_import.insert_rows(
            [self._duplicate_row(prop)], profile_id=other.id, arrival_ref="import:j1"
        )

        assert outcome["created"] == []
        assert Property.query.count() == 1
        (entry,) = _items(prop_id)
        assert entry["profile_id"] == other.id
        assert entry["source"] == "fotocasa"
        assert entry["method"] == "portal_payload"
        assert entry["ref"] == "import:j1"
        assert entry["received_at"] is None
        assert db.session.get(Property, prop_id).search_profile_id == home.id

    def test_a_row_that_became_stored_after_the_preview_is_journaled_too(
        self, app, home, other
    ):
        prop_id = self._stored(home).id
        listing = parse_listing(FOTOCASA_PAGE.read_text(encoding="utf-8"), FOTOCASA_URL)

        outcome = fotocasa_import.insert_rows(
            [fotocasa_import.preview_row(listing)],
            profile_id=other.id,
            arrival_ref="import:j2",
        )

        assert len(outcome["skipped"]) == 1
        assert [i["ref"] for i in _items(prop_id)] == ["import:j2"]

    def test_importing_into_the_rows_own_subscription_records_nothing(self, app, home):
        prop = self._stored(home)

        fotocasa_import.insert_rows(
            [self._duplicate_row(prop)], profile_id=home.id, arrival_ref="import:j1"
        )

        assert "arrivals" not in _stored(prop.id)

    def test_the_confirm_route_names_its_preview_so_two_presses_are_one_entry(
        self, app, home, other
    ):
        from services.background_jobs import run_job_sync

        prop = self._stored(home)
        prop_id, other_id = prop.id, other.id
        row = self._duplicate_row(prop)
        job_id = run_job_sync(lambda: {"rows": [row]}, job_type="fotocasa_import_read")

        with app.test_client() as client:
            for _ in range(2):
                response = client.post(
                    "/properties/import/confirm",
                    data={"job_id": job_id, "profile_id": other_id},
                    follow_redirects=False,
                )
                assert response.status_code == 302

        (entry,) = _items(prop_id)
        assert entry["ref"] == f"import:{job_id}"
        assert entry["profile_id"] == other_id
