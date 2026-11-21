"""A listing a second saved search delivers leaves a record, and nothing else.

Issue #632, `services/arrival_journal.py`. Pinned here: the entry and what
must not move (`search_profile_id`, the row count, the other `enrichment`
blocks, no fetch for a stored listing); the refusals (the row's own search,
no profile, the same delivery twice, no delivery id, a full journal, a block
it cannot read); the write discipline (the guard before any query, every
decision on the locked row); and the mail doors, including a journal that
fails mid-email.

Not proved here: two processes racing for one row. The suite is one SQLite
connection and SQLite ignores `FOR UPDATE`; what is proved is the half that
fails silently -- the stored row changes underneath the session and the
writer has to see it.
"""

import json
import logging
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


def _column_text(prop_id: int):
    """The column exactly as stored, as text."""
    raw = db.session.execute(
        text("SELECT CAST(enrichment AS TEXT) FROM properties WHERE id = :i"),
        {"i": prop_id},
    ).scalar_one()
    db.session.rollback()
    return raw


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

    def test_a_time_in_another_zone_is_stored_as_utc(self, app, home, other):
        prop = _prop(home)
        madrid = timezone(timedelta(hours=1))

        _record(prop, other, received_at=datetime(2026, 11, 20, 10, 15, tzinfo=madrid))

        assert _items(prop.id)[0]["received_at"] == "2026-11-20T09:15:00+00:00"


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

    @pytest.mark.parametrize(
        "profile, ref", [("other", None), (None, "imap_8"), ("other", "imap_8")]
    )
    def test_no_exit_reads_the_row_before_the_guard(
        self, app, home, other, profile, ref
    ):
        """Review of #698: the guard is the first statement on every path.

        Reading `prop.id` for a log line, or looking the subscription up, is
        a query; it autoflushes another row's uncommitted edit, and the next
        call's commit persists it. So every exit refuses first.
        """
        prop = _prop(home)
        unrelated = _prop(home, source_email_id="fotocasa:2")
        unrelated_id, other_id = unrelated.id, other.id
        assert unrelated.title == "Arrival fixture"
        db.session.expire(prop)
        db.session.expire(other)
        unrelated.title = "an edit nobody committed"

        for _ in range(2):
            with pytest.raises(EnrichmentWriteContractError):
                arrival_journal.record(
                    prop,
                    profile_id=other_id if profile else None,
                    source="fotocasa",
                    method=arrival_journal.METHOD_ALERT_EMAIL,
                    ref=ref,
                )

        db.session.rollback()
        assert db.session.get(Property, unrelated_id).title == "Arrival fixture"


# Everything that may sit under the key and is NOT the shape `record()` writes.
# An explicit JSON null is one of them (review of #698): somebody stored it.
NOT_THE_BLOCK = [
    None,
    "",
    0,
    [],
    ["legacy"],
    {},
    {"items": []},
    {"truncated": False},
    {"items": "x", "truncated": False},
    {"items": [], "truncated": "yes"},
    {"items": [], "truncated": None},
    # One malformed item makes the whole block unreadable. The second is the
    # review's own case: the delivery `_record` sends, without a subscription.
    {"items": [1], "truncated": False},
    {"items": [{"source": "fotocasa", "ref": "imap_7"}], "truncated": False},
    {"items": [{"profile_id": True, "source": "f", "ref": "r"}], "truncated": False},
    {"items": [{"profile_id": 3, "source": "f", "ref": " "}], "truncated": False},
    {"items": [{"profile_id": 3, "source": "", "ref": "r"}], "truncated": False},
    # Review of #704: every field `record()` writes is part of the shape. The
    # first three name this very delivery and would have passed as "already
    # recorded": a method that is not text, no time of recording, a time of
    # receipt that is not text.
    *(
        {
            "items": [
                {
                    "profile_id": 3,
                    "source": "fotocasa",
                    "method": "alert_email",
                    "ref": "imap_7",
                    "received_at": None,
                    "recorded_at": "2026-11-21T18:00:00+00:00",
                    **wrong,
                }
            ],
            "truncated": False,
        }
        for wrong in (
            {"method": []},
            {"recorded_at": None},
            {"received_at": 7},
            {"method": " "},
        )
    ),
]


WHOLE_ENTRY = {
    "profile_id": 3,
    "source": "fotocasa",
    "method": "alert_email",
    "ref": "imap_7",
    "received_at": "2026-11-21T17:59:00+00:00",
    "recorded_at": "2026-11-21T18:00:00+00:00",
}

# One wrong value at a time, per field `record()` writes.
WRONG_FIELDS = [
    ("profile_id", None),
    ("profile_id", "3"),
    ("profile_id", 3.0),
    ("profile_id", True),
    ("source", None),
    ("source", 7),
    ("source", ""),
    ("ref", None),
    ("ref", 7),
    ("ref", " "),
    ("method", None),
    ("method", []),
    ("method", ""),
    ("recorded_at", None),
    ("recorded_at", 1763748000),
    ("recorded_at", "yesterday"),
    # Second review of #704: `fromisoformat` reads a bare date as midnight.
    ("recorded_at", "2026-11-21"),
    ("recorded_at", "20261121"),
    # `record()` writes the recording time as UTC, always with its offset.
    ("recorded_at", "2026-11-21T18:00:00"),
    ("recorded_at", "2026-11-21T19:00:00+01:00"),
    ("recorded_at", "2026-11-21T18:00:00Z"),
    ("recorded_at", "2026-11-21T18:00:00.5+00:00"),
    ("recorded_at", "2026-11-21 18:00:00+00:00"),
    ("recorded_at", "2026-13-21T18:00:00+00:00"),
    ("received_at", 7),
    ("received_at", ""),
    ("received_at", "not a time"),
    ("received_at", "2026-11-21"),
    ("received_at", "2026-11-21T09:29"),
    ("received_at", "2026-11-21T10:29:59+01:00"),
    ("received_at", "2026-11-21T25:29:59"),
]

# The one entry production held when #704 was written (row 4061, read with
# SELECT 2026-11-21): `received_at` without an offset, as `_utc_text` writes
# a time whose zone the mail layer did not know.
PRODUCTION_ENTRY = {
    "profile_id": 1,
    "source": "yaencontre",
    "method": "alert_email",
    "ref": "imap_496167",
    "received_at": "2026-11-21T09:29:59",
    "recorded_at": "2026-11-21T18:00:05+00:00",
}

# What two reviews handed in, verbatim (#698 round four, then #704).
REVIEWERS_ENTRIES = [
    {
        "profile_id": 2,
        "source": "fotocasa",
        "method": [],
        "ref": "imap_7",
        "received_at": None,
        "recorded_at": None,
    },
    {
        "profile_id": 7,
        "ref": "imap_1",
        "source": "yaencontre",
        "method": [],
        "recorded_at": None,
    },
    # Second review of #704, verbatim: no `received_at` key at all.
    {
        "profile_id": 3,
        "source": "fotocasa",
        "method": "alert_email",
        "ref": "imap_7",
        "recorded_at": "2026-11-21T18:00:00+00:00",
    },
    # Second review of #704: a bare date where the recording time goes.
    {**WHOLE_ENTRY, "recorded_at": "2026-11-21"},
]


class TestAnEntryIsEveryFieldTyped:
    def test_what_record_writes_is_an_entry(self, app, home, other):
        prop = _prop(home)
        _record(prop, other)
        _record(prop, other, ref="imap_8", received_at=None)
        _record(prop, other, ref="imap_9", received_at=datetime(2026, 11, 21, 9, 29))

        written = _items(prop.id)
        assert len(written) == 3 and all(map(arrival_journal._is_entry, written))
        assert written[2]["received_at"] == "2026-11-21T09:29:00"
        assert arrival_journal._is_entry(WHOLE_ENTRY)
        assert arrival_journal._is_entry({**WHOLE_ENTRY, "received_at": None})
        assert arrival_journal._is_entry({**WHOLE_ENTRY, "extra": {"kept": 1}})

    def test_the_entry_production_holds_is_read(self):
        block = {"arrivals": {"items": [PRODUCTION_ENTRY], "truncated": False}}

        assert arrival_journal._is_entry(PRODUCTION_ENTRY)
        assert arrival_journal.read(Property(enrichment=block)) == {
            "items": [PRODUCTION_ENTRY],
            "truncated": False,
            "readable": True,
        }

    @pytest.mark.parametrize("field, wrong", WRONG_FIELDS)
    def test_one_wrong_field_is_not_an_entry(self, field, wrong):
        item = {**WHOLE_ENTRY, field: wrong}
        block = {"arrivals": {"items": [item], "truncated": False}}

        assert not arrival_journal._is_entry(item)
        assert arrival_journal.read(Property(enrichment=block))["readable"] is False

    @pytest.mark.parametrize("field", arrival_journal.ENTRY_FIELDS)
    @pytest.mark.parametrize(
        "entry", [WHOLE_ENTRY, {**WHOLE_ENTRY, "received_at": None}]
    )
    def test_a_missing_field_is_not_an_entry(self, field, entry):
        """Every key, `received_at` included: an absent key is not the null
        `record()` writes (second review of #704)."""
        item = {key: value for key, value in entry.items() if key != field}
        block = {"arrivals": {"items": [item], "truncated": False}}

        assert not arrival_journal._is_entry(item)
        assert arrival_journal.read(Property(enrichment=block))["readable"] is False

    def test_the_fields_are_the_keys_record_writes(self, app, home, other):
        prop = _prop(home)
        _record(prop, other)

        (written,) = _items(prop.id)
        assert set(written) == set(arrival_journal.ENTRY_FIELDS)

    @pytest.mark.parametrize("item", REVIEWERS_ENTRIES)
    def test_the_entries_two_reviews_handed_in_are_not_entries(self, item):
        block = {"arrivals": {"items": [item], "truncated": False}}

        assert not arrival_journal._is_entry(item)
        assert arrival_journal.read(Property(enrichment=block)) == {
            "items": [],
            "truncated": False,
            "readable": False,
        }


class TestReadingIsTotalAndWritingFailsClosed:
    def test_any_stored_value_reads_without_raising(self, app):
        empty = {"items": [], "truncated": False}
        for column in (["legacy"], "text", "", 7, 0):
            assert arrival_journal.read(Property(enrichment=column)) == {
                **empty,
                "readable": False,
            }
        for block in NOT_THE_BLOCK:
            assert arrival_journal.read(Property(enrichment={"arrivals": block})) == {
                **empty,
                "readable": False,
            }, block
        # Only an absent key, or a row with no enrichment at all, is empty.
        for column in (None, {}, {"sea": 1}):
            assert arrival_journal.read(Property(enrichment=column)) == {
                **empty,
                "readable": True,
            }

    def test_what_read_returns_is_what_the_duplicate_check_sees(self, app, home, other):
        """One reading of the shape (review of #698): only an entry `read()`
        returns can make a delivery "already recorded"."""
        entry = {
            "profile_id": other.id,
            "source": "fotocasa",
            "method": "alert_email",
            "ref": "imap_7",
            "received_at": None,
            "recorded_at": "2026-11-21T18:00:00+00:00",
        }
        prop = _prop(
            home, enrichment={"arrivals": {"items": [entry], "truncated": False}}
        )

        assert arrival_journal.read(prop)["items"] == [entry]
        assert _record(prop, other) == arrival_journal.ALREADY_RECORDED
        assert _record(prop, other, ref="imap_8") == arrival_journal.RECORDED
        assert [i["ref"] for i in arrival_journal.read(prop)["items"]] == [
            "imap_7",
            "imap_8",
        ]

    @pytest.mark.parametrize(
        "stored", [["legacy"], "", 0, *({"arrivals": b} for b in NOT_THE_BLOCK)]
    )
    def test_a_value_it_cannot_read_is_never_written_over(
        self, app, home, other, stored, caplog
    ):
        """The stored text stays byte for byte, the answer is `unwritable` --
        never the silent "already recorded" a malformed twin of this delivery
        used to get (review of #698) -- and the log names the row."""
        prop = _prop(home)
        prop_id = prop.id
        db.session.execute(
            text("UPDATE properties SET enrichment = :e WHERE id = :i"),
            {"e": json.dumps(stored), "i": prop_id},
        )
        db.session.commit()
        before = _column_text(prop_id)

        with caplog.at_level(logging.ERROR, logger="services.arrival_journal"):
            assert _record(prop, other) == arrival_journal.UNWRITABLE

        assert _column_text(prop_id) == before
        assert f"Listing {prop_id} holds" in caplog.text

    def test_the_other_keys_of_the_block_are_kept_when_an_entry_is_added(
        self, app, home, other
    ):
        block = {"items": [], "truncated": False, "kept": {"a": 1}}
        prop = _prop(home, enrichment={"arrivals": block})

        assert _record(prop, other) == arrival_journal.RECORDED

        stored = _stored(prop.id)["arrivals"]
        assert stored["items"][0]["profile_id"] == other.id
        assert stored["kept"] == {"a": 1}

    @pytest.mark.parametrize("ref", [None, "", "   ", 7])
    def test_a_delivery_without_an_id_is_not_recorded(
        self, app, home, other, ref, caplog
    ):
        """Review of #698: "no id" may not be a key. Two id-less deliveries
        through two searches were one delivery, and the second was dropped."""
        third = _profile("Cantabria")
        prop = _prop(home)

        with caplog.at_level(logging.ERROR, logger="services.arrival_journal"):
            assert _record(prop, other, ref=ref) == arrival_journal.NO_DELIVERY_ID
            assert _record(prop, third, ref=ref) == arrival_journal.NO_DELIVERY_ID

        assert "arrivals" not in _stored(prop.id)
        assert "no delivery id" in caplog.text


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

    def _other_card(self, listing_id: int) -> dict:
        return {
            **self._yaencontre_card(),
            "listing_id": listing_id,
            "url": f"https://www.yaencontre.com/venta/casa/inmueble-75866-{listing_id}",
        }

    def _failing_journal(self, monkeypatch) -> None:
        """A journal that dies half-way, with an edit of its own pending."""

        def fail(prop, **kwargs):
            prop.title = "half-written by the journal"
            raise RuntimeError("the journal could not be written")

        monkeypatch.setattr(arrival_journal, "record", fail)

    def test_a_failed_journal_costs_the_email_nothing_it_created(
        self, app, home, other, monkeypatch
    ):
        """A new card, a stored card whose journal write fails, another new
        card: both new rows land and the email answers "not all done"."""
        stored_id = self._stored_yaencontre(home)
        self._failing_journal(monkeypatch)

        created, all_done = _service()._ingest_yaencontre_email(
            {
                **_email(other),
                "cards": [
                    self._other_card(501),
                    self._yaencontre_card(),
                    self._other_card(502),
                ],
            },
            sale_only=False,
            excluded_categories=set(),
        )

        assert (created, all_done) == (2, False)
        assert {p.source_email_id for p in Property.query.all()} == {
            "yaencontre:112395195",
            "yaencontre:501",
            "yaencontre:502",
        }
        assert "arrivals" not in _stored(stored_id)
        # Its half-written edit went with it, past the commits of the cards
        # that followed.
        db.session.expire_all()
        assert db.session.get(Property, stored_id).title.startswith("Casa adosada")

    def test_a_failed_journal_does_not_undo_the_price_it_follows(
        self, app, home, other, monkeypatch
    ):
        """The journal's rollback may only undo the journal. Then the held
        email is read again: the price is not applied twice."""
        prop_id = self._stored_yaencontre(home)
        email = {**_email(other), "cards": [self._yaencontre_card(price=170000.0)]}

        with monkeypatch.context() as patch:
            self._failing_journal(patch)
            first = _service()
            assert first._ingest_yaencontre_email(
                email, sale_only=False, excluded_categories=set()
            ) == (0, False)
        db.session.expire_all()
        prop = db.session.get(Property, prop_id)
        assert (float(prop.price), float(prop.previous_price)) == (170000.0, 180000.0)
        assert first.portal_price_updated_count == 1

        # The same service, as one run reads its next email: the failure of
        # the email before does not follow it.
        assert first._ingest_yaencontre_email(
            email, sale_only=False, excluded_categories=set()
        ) == (0, True)
        db.session.expire_all()
        prop = db.session.get(Property, prop_id)
        assert (float(prop.price), float(prop.previous_price)) == (170000.0, 180000.0)
        assert first.portal_price_updated_count == 1
        assert [i["ref"] for i in _items(prop_id)] == ["imap_41"]

    def test_an_answer_on_the_facts_does_not_hold_the_email(self, app, home, other):
        """An unreadable block and a missing delivery id are the same answer
        tomorrow: logged by the journal, and the email is consumed."""
        prop_id = self._stored_yaencontre(home)
        db.session.execute(
            text("UPDATE properties SET enrichment = :e WHERE id = :i"),
            {"e": json.dumps({"arrivals": None}), "i": prop_id},
        )
        db.session.commit()
        cards = {"cards": [self._yaencontre_card()]}

        assert _service()._ingest_yaencontre_email(
            {**_email(other), **cards}, sale_only=False, excluded_categories=set()
        ) == (0, True)
        assert _service()._ingest_yaencontre_email(
            {**_email(other), **cards, "source_email_id": None},
            sale_only=False,
            excluded_categories=set(),
        ) == (0, True)
        assert _stored(prop_id) == {"arrivals": None}

    def test_a_failed_journal_holds_fotocasa_and_milanuncios_mail_too(
        self, app, home, other, monkeypatch
    ):
        listing = parse_listing(FOTOCASA_PAGE.read_text(encoding="utf-8"), FOTOCASA_URL)
        fotocasa_import.insert_rows(
            [fotocasa_import.preview_row(listing)], profile_id=home.id
        )
        row = milanuncios_source.parse_listing(MA_PAGE.read_text(), MA_URL)
        db.session.add(
            fotocasa_import.build_property(
                row, profile_id=home.id, source=milanuncios_source.SOURCE_NAME
            )
        )
        db.session.commit()
        monkeypatch.setattr(fotocasa_source, "fetch_listing", _no_fetch)
        monkeypatch.setattr(milanuncios_source, "fetch_listing", _no_fetch)
        monkeypatch.setattr(
            milanuncios_source, "resolve_tracker", lambda url, session=None: MA_URL
        )
        self._failing_journal(monkeypatch)

        assert _service()._ingest_fotocasa_email(
            {**_email(other), "urls": [FOTOCASA_URL]},
            sale_only=False,
            excluded_categories=set(),
        ) == (0, False)
        assert _service()._ingest_milanuncios_email(
            {**_email(other), "tracker_urls": ["https://click.example/t/1"]},
            sale_only=False,
            excluded_categories=set(),
        ) == (0, False)

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
