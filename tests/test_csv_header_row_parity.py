"""The CSV export's header and its rows must be the same width.

Not a hypothetical. On 2026-08-16 two branches each added columns to this one
function -- #330 a sea-view target, #324 the three listing-status columns -- and
git auto-merged all four edits, in `base_header` and in `row`, with no conflict
marker anywhere. Had one side landed a header without its value, or a value
without its header, every column after the divergence would be shifted by one:
a silently wrong export, not a crash. Nothing in the suite would have said a
word, because no test compared the two lists.

The check is deliberately behavioural rather than a count of literals in the
source. What matters is what the file actually contains, and the width depends
on the request: `travel_headers` are appended per profile, so a bare export and
a per-profile one legitimately differ from each other -- but never from their
own rows.
"""

from __future__ import annotations

import csv
import io

import pytest

from app import create_app
from models import Property, db
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _listing():
    prop = Property(
        source_email_id="csv-parity-1",
        title="A house",
        url="https://www.idealista.com/inmueble/10000001/",
        municipality="El Franco",
        property_category="land",
        price=99000,
        area=2600,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _rows(client, query: str):
    response = client.get(f"/properties/export.csv{query}")
    assert response.status_code == 200, response.status_code
    return list(csv.reader(io.StringIO(response.get_data(as_text=True))))


def test_every_exported_row_is_as_wide_as_its_header(app, client):
    _listing()

    rows = _rows(client, "?profile_id=unassigned")
    assert len(rows) > 1, "the export produced no data row, so it proves nothing"

    header, *data = rows
    for index, row in enumerate(data):
        assert len(row) == len(header), (
            f"row {index} has {len(row)} cells against {len(header)} headers -- "
            "every column after the first divergence is mislabelled"
        )


def test_the_status_columns_sit_where_the_header_says(app, client):
    """Parity alone still passes if a header and its value are both missing."""
    _listing()

    header, row = _rows(client, "?profile_id=unassigned")[:2]
    for column in ("Status", "Status Source", "Status Checked At"):
        assert column in header, f"{column} is missing from the export header"
        # Position, not just presence: a value read from the wrong index is the
        # failure this file exists to catch.
        assert len(row) > header.index(column)
