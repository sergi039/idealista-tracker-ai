"""The read-only count over the arrival journal (#632).

`utils/report_arrivals.py` answers "how many stored listings has a second
saved search delivered" from `enrichment["arrivals"]`. Pinned here: what it
counts, that it reads each entry through today's routes, that a journal it
cannot read is counted on its own line rather than as "no arrivals", and
that the command writes nothing.
"""

import json

import pytest
from sqlalchemy import text

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from services import arrival_journal  # noqa: E402
from utils import report_arrivals  # noqa: E402


class _Row:
    def __init__(self, row_id, home, items=None, truncated=False, enrichment=None):
        self.id = row_id
        self.search_profile_id = home
        self.enrichment = enrichment
        if items is not None:
            self.enrichment = {"arrivals": {"items": items, "truncated": truncated}}


def _item(profile_id, ref="imap_1", source="yaencontre"):
    return {"profile_id": profile_id, "ref": ref, "source": source}


class TestTheCount:
    def test_it_counts_listings_entries_and_pairs(self):
        rows = [
            _Row(1, 24),
            _Row(2, 24, [_item(7, "imap_1"), _item(7, "imap_2")]),
            _Row(3, 1, [_item(24, "imap_3", "fotocasa"), _item(7, "imap_4")]),
            _Row(4, 24, [_item(7, "imap_5")], truncated=True),
        ]

        report = report_arrivals.collect(rows, {1: None, 7: None, 24: None})

        assert report["rows_scanned"] == 4
        assert report["rows_with_a_journal"] == 3
        assert report["rows_truncated"] == 1
        assert report["rows_unreadable"] == 0
        assert report["entries"] == 5
        assert report["entries_absorbed_by_a_later_route"] == 0
        assert report["listings_delivered_by_a_second_search"] == 3
        assert report["by_source"] == {"fotocasa": 1, "yaencontre": 4}
        assert report["pairs"] == [
            {
                "home_profile_id": 24,
                "arrival_profile_id": 7,
                "listings": 2,
                "entries": 3,
            },
            {
                "home_profile_id": 1,
                "arrival_profile_id": 24,
                "listings": 1,
                "entries": 1,
            },
            {
                "home_profile_id": 1,
                "arrival_profile_id": 7,
                "listings": 1,
                "entries": 1,
            },
        ]

    def test_a_search_since_routed_onto_the_home_is_absorbed_not_counted(self):
        rows = [_Row(1, 24, [_item(31), _item(7, "imap_2")])]

        report = report_arrivals.collect(rows, {7: None, 24: None, 31: 24})

        assert report["entries"] == 2
        assert report["entries_absorbed_by_a_later_route"] == 1
        assert report["listings_delivered_by_a_second_search"] == 1
        assert [p["arrival_profile_id"] for p in report["pairs"]] == [7]

    def test_a_row_whose_every_entry_was_absorbed_is_not_a_second_search(self):
        rows = [_Row(1, 24, [_item(31)])]

        report = report_arrivals.collect(rows, {24: None, 31: 24})

        assert report["rows_with_a_journal"] == 1
        assert report["listings_delivered_by_a_second_search"] == 0
        assert report["pairs"] == []

    @pytest.mark.parametrize(
        "enrichment",
        [
            ["legacy"],
            {"arrivals": None},
            {"arrivals": {"items": [{"ref": "imap_1"}], "truncated": False}},
        ],
    )
    def test_a_journal_it_cannot_read_is_counted_apart_and_never_raises(
        self, enrichment
    ):
        """Not "no arrivals": the module could not read the row, and one such
        row must not abort the pass over the rest (review of #698)."""
        rows = [_Row(1, 24, enrichment=enrichment), _Row(2, 24, [_item(7)])]

        report = report_arrivals.collect(rows, {7: None, 24: None})

        assert report["rows_scanned"] == 2
        assert report["rows_unreadable"] == 1
        assert report["rows_with_a_journal"] == 1
        assert report["listings_delivered_by_a_second_search"] == 1


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def journaled(app):
    """One listing at home, delivered once by another subscription."""
    home = SearchProfile(name="Galicia costa", is_active=True, is_default=True)
    other = SearchProfile(name="Asturias", is_active=True)
    db.session.add_all([home, other])
    db.session.commit()
    prop = Property(source_email_id="fotocasa:1", title="x", search_profile_id=home.id)
    db.session.add(prop)
    db.session.commit()
    outcome = arrival_journal.record(
        prop,
        profile_id=other.id,
        source="fotocasa",
        method=arrival_journal.METHOD_ALERT_EMAIL,
        ref="imap_7",
    )
    assert outcome == arrival_journal.RECORDED
    return prop.id, home.id, other.id


def _column(prop_id):
    value = db.session.execute(
        text("SELECT CAST(enrichment AS TEXT) FROM properties WHERE id = :i"),
        {"i": prop_id},
    ).scalar_one()
    db.session.rollback()
    return value


class TestTheCommand:
    def test_it_reads_the_table_and_writes_nothing(self, app, journaled, capsys):
        prop_id, home_id, other_id = journaled
        before = _column(prop_id)

        assert report_arrivals.main(["--format", "json"], app=app) == 0

        report = json.loads(capsys.readouterr().out)
        assert report["rows_scanned"] == 1
        assert report["listings_delivered_by_a_second_search"] == 1
        assert report["pairs"] == [
            {
                "home_profile_id": home_id,
                "arrival_profile_id": other_id,
                "listings": 1,
                "entries": 1,
            }
        ]
        assert _column(prop_id) == before

    def test_the_text_names_the_subscriptions_and_its_blind_spots(
        self, app, journaled, capsys
    ):
        _, home_id, other_id = journaled

        assert report_arrivals.main([], app=app) == 0

        out = capsys.readouterr().out
        assert "Listings a second search delivered:    1" in out
        assert "Rows whose journal cannot be read:     0" in out
        assert f"{home_id} Galicia costa <- {other_id} Asturias: 1, 1" in out
        assert "Not counted" in out
