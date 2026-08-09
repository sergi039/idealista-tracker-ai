"""Regression coverage for deterministic pagination and CSV ordering (#113).

PostgreSQL may return rows that tie on an ``ORDER BY`` expression in any
relative order.  SQLite normally hides that freedom behind repeatable rowid
order, so these tests install two equally valid indexes with opposite ID order
between page requests.  The data does not change: only the database's legal
choice of tie order does.  A complete ordering is therefore stable, while the
old single-column ordering repeats and omits rows across pages.
"""

import csv
import io
import re

import pytest
from sqlalchemy import Index

from app import create_app, db
from models import Land, Property, SearchProfile
from tests import setup_test_environment


PAGE_SIZE = 10
ROWS_PER_TIE = 12
TEST_INDEX_PREFIX = "ix_issue113_"


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
        for model in (Property, Land):
            for index in tuple(model.__table__.indexes):
                if index.name.startswith(TEST_INDEX_PREFIX):
                    model.__table__.indexes.discard(index)


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_tied_rows(model):
    profile_id = None
    if model is Property:
        profile = SearchProfile(
            name="Issue 113 deterministic sorting",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.flush()
        profile_id = profile.id

    rows = []
    for position in range(ROWS_PER_TIE * 2):
        kwargs = {
            "source_email_id": f"issue113_{model.__tablename__}_{position}",
            "title": f"Issue 113 row {position:02d}",
            "listing_status": "active",
            # One non-NULL tie followed by a NULL tie.  Each run crosses a
            # page boundary, so changing a legal tie order is observable.
            "price": 100_000 if position < ROWS_PER_TIE else None,
        }
        if model is Property:
            kwargs["search_profile_id"] = profile_id
        rows.append(model(**kwargs))

    db.session.add_all(rows)
    db.session.commit()

    priced_ids = [row.id for row in rows if row.price is not None]
    null_ids = [row.id for row in rows if row.price is None]
    return priced_ids + null_ids


def _install_plan_index(model, sort_direction, id_direction):
    expressions = []
    if model is Property:
        expressions.append(Property.search_profile_id.asc())
    expressions.extend(
        [
            getattr(model.price, sort_direction)(),
            getattr(model.id, id_direction)(),
        ]
    )
    index = Index(
        f"{TEST_INDEX_PREFIX}{model.__tablename__}_{sort_direction}_{id_direction}",
        *expressions,
    )
    index.create(bind=db.engine)
    return index


def _page_ids(client, endpoint, row_pattern, direction, page):
    response = client.get(
        f"{endpoint}?sort=price&order={direction}&per_page={PAGE_SIZE}"
        f"&page={page}&view_type=list"
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    return [int(value) for value in re.findall(row_pattern, body)]


def _csv_ids(client, endpoint, direction):
    response = client.get(f"{endpoint}?sort=price&order={direction}")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    id_column = rows[0].index("ID")
    return [int(row[id_column]) for row in rows[1:]]


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize(
    ("model", "page_endpoint", "export_endpoint", "row_pattern"),
    [
        pytest.param(
            Property,
            "/properties",
            "/properties/export.csv",
            r'data-property-id="(\d+)"',
            id="properties",
        ),
        pytest.param(
            Land,
            "/lands",
            "/export.csv",
            r'<tr class="land-row[^"]*" data-land-id="(\d+)"',
            id="lands",
        ),
    ],
)
def test_ties_do_not_repeat_or_disappear_and_csv_matches_page(
    app,
    client,
    model,
    page_endpoint,
    export_endpoint,
    row_pattern,
    direction,
):
    """Tied values and a NULL run have one total order at every boundary."""
    with app.app_context():
        expected_ids = _seed_tied_rows(model)
        descending_ties = _install_plan_index(model, direction, "desc")

    page_ids = _page_ids(client, page_endpoint, row_pattern, direction, page=1)

    # Force a second legal database tie order for the remaining page queries.
    # An explicit model-ID tiebreaker makes this plan change unobservable.
    with app.app_context():
        descending_ties.drop(bind=db.engine)
        _install_plan_index(model, direction, "asc")

    page_count = (len(expected_ids) + PAGE_SIZE - 1) // PAGE_SIZE
    for page in range(2, page_count + 1):
        page_ids.extend(_page_ids(client, page_endpoint, row_pattern, direction, page))

    assert len(page_ids) == len(expected_ids)
    assert len(set(page_ids)) == len(expected_ids), "pagination repeated tied rows"
    assert set(page_ids) == set(expected_ids), "pagination omitted tied rows"
    assert page_ids == expected_ids
    assert _csv_ids(client, export_endpoint, direction) == page_ids
