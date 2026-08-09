"""Issue #113: a sorted listing must end its ORDER BY on a unique key.

Every sort surface puts exactly one *nullable* column into ORDER BY and stops
there -- `/properties`, `/lands`, both CSV exports and the two JSON list
endpoints. Rows that tie on that column (equal price, equal area, equal score,
or the whole NULL run that `nullslast()` parks at the end) therefore come back
in whatever order the database happened to produce for that one execution.

That is not cosmetic. `/properties` and `/lands` run one query *per page*, so
between page 1 and page 2 the same row can be handed out twice while another
is never shown at all; the CSV exports run a single query over the whole set,
so the file and the screen disagree. `/api/properties` and `/api/lands`
paginate by offset/limit and lose rows the same way.

Two layers, and only one of them is proof:

* The **contract layer** inspects the SQL the routes really executed -- a
  `before_cursor_execute` listener on the app's own engine -- and requires the
  listing SELECT's ORDER BY to end on `properties.id` / `lands.id`. This is
  what fails today: not one of the six ordering sites appends the primary key,
  and `/lands` asked for a sort `Land` does not have emits no ORDER BY at all.

* The **behavioural layer** is the ticket's acceptance criteria: sweep every
  page, no duplicates, no gaps, export equal to screen. Be honest about it --
  this suite runs on sqlite in-memory (tests/__init__.py:30), and sqlite hands
  tied rows back in rowid order, which coincides with the id tiebreaker. Most
  of this layer therefore passes *before* the fix; it pins the guarantee for
  the future and is not evidence of the defect. The behavioural cases that do
  fail against unfixed code are the ones sqlite cannot mask: `/lands` asked
  for a sort it does not have (the page applies no ordering at all while its
  own export falls back to `score_total`), the three archive surfaces asked to
  sort by a real-but-non-column attribute such as `to_dict` (`hasattr()` waved
  it into the column branch and `.desc()` raised there), and
  `/properties/export.csv` on a `mode` URL (the page resolves that mode's
  score as its default sort, the export resolved `created_at`).

Two deliberate deviations from the ticket text, both forced by the code:

* `per_page` is clamped to a minimum of 10 by both pages, so the paging sweep
  uses `per_page=10` over a 14-row fixture instead of the `per_page=2` the
  ticket suggests. The tie groups are placed so that one straddles the page
  boundary in *each* direction.
* `sort=travel_time_nearest_beach` is a real `Land` column (models.py), so it
  is not the unknown sort for `/lands`. The unknown-sort case uses a
  `/properties`-shaped key that `Land` genuinely lacks.

Nothing here mocks the routes: the assertions run against the real Flask test
client, the real templates and the real SQL. No external API is touched.
"""

import csv
import io
import re
from datetime import datetime

import pytest
from sqlalchemy import event

from app import create_app, db
from models import Land, Property, SearchProfile
from tests import setup_test_environment
from utils.sorting import sortable_columns

# The /properties sort allow-list (routes/main_routes.py) plus the investment
# rating, which is ranked in a branch of its own.
PROPERTY_SORTS = (
    "title",
    "created_at",
    "price",
    "area",
    "score_total",
    "score_investment",
    "score_lifestyle",
    "investment_metrics",
)

# /lands has no allow-list: it accepts any attribute of `Land`. These are the
# values its own UI emits.
LAND_SORTS = PROPERTY_SORTS

# Sorts `Land` does not have. `/lands` currently answers them with a SELECT
# carrying no ORDER BY, while its own /export.csv falls back to the mode
# default (routes/main_routes.py:2713-2718).
LAND_UNKNOWN_SORTS = ("property_subtype", "bogus_sort_from_an_old_bookmark")

# The nastier half of the same defect: names `Land` really does have, which a
# `hasattr()` gate waves through into the column branch even though none of
# them can produce an ORDER BY term. `to_dict` is the model's own serialiser
# (models.py:666), `metadata` the declarative MetaData. Both are asserted to
# exist -- and not to be columns -- before they are used as sorts.
LAND_NON_COLUMN_ATTRS = ("to_dict", "metadata")

# Every `Land` sort that really is a mapped column. A gate stricter than
# `hasattr()` must not quietly demote any of these to the fallback branch --
# the scores and the beach travel time are columns (models.py:608-616), even
# though `/properties` has no equivalent for the last one.
LAND_COLUMN_SORTS = (
    "title",
    "created_at",
    "price",
    "area",
    "score_total",
    "score_investment",
    "score_lifestyle",
    "travel_time_nearest_beach",
)

# `/properties` resolves its default sort from `mode` and normalises an
# unknown sort onto it (routes/main_routes.py:193-199, 269-270). Its export
# has to reach the same conclusion or criterion 3 only holds for URLs that
# already carry a usable sort.
PROPERTY_MODES = ("combined", "investment", "lifestyle")

ORDERS = ("asc", "desc")

# Both pages clamp per_page into [10, 100]; 10 is the smallest page reachable
# over HTTP, which is why the fixture carries 14 rows.
PER_PAGE = 10

# label, price, score_total. Insertion order is deliberately neither the price
# order nor the score order, so "sqlite returned rowid order" can never be
# mistaken for "the route sorted correctly".
#
# Sorted by price DESC (NULLs last) the 200000 group lands on positions 9-11
# and straddles the page boundary; sorted ASC the 800000 group lands on 10-11
# and straddles it. The two NULL prices are the tail in both directions.
TIED_ROWS = (
    ("mid_400", 400000, 35),
    ("tie_low_a", 200000, 71),
    ("null_b", None, 12),
    ("top_900", 900000, 58),
    ("tie_high_a", 800000, 90),
    ("mid_300", 300000, 24),
    ("tie_low_b", 200000, 66),
    ("mid_600", 600000, 41),
    ("null_a", None, 83),
    ("tie_high_b", 800000, 17),
    ("bottom_100", 100000, 55),
    ("mid_500", 500000, 29),
    ("tie_low_c", 200000, 77),
    ("mid_700", 700000, 48),
)

TIED_PRICE = 200000
TIED_PRICE_LABELS = ("tie_low_a", "tie_low_b", "tie_low_c")
NULL_PRICE_LABELS = ("null_a", "null_b")

# Enough rows carry a rating for the rank branch to have both matches and
# NULLs, so `investment_metrics` exercises its nullslast() run too.
RATINGS = {
    "top_900": "EXCELLENT - strong demand",
    "tie_low_a": "GOOD - steady",
    "tie_low_b": "GOOD - steady",
    "tie_high_a": "MODERATE - thin market",
    "null_a": "MODERATE - thin market",
}


class SqlRecorder:
    """Every statement the engine actually executed, whitespace-normalised."""

    def __init__(self):
        self.statements = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(" ".join(statement.split()))

    def reset(self):
        self.statements.clear()

    def listing_selects(self, table):
        """The multi-row entity SELECTs this request ran against `table`.

        Three other statement shapes reach the same table and must not be
        confused with the listing query:

        * `paginate()`'s `SELECT count(*) ... FROM (SELECT ...)`, which wraps
          the row query and legitimately carries no ORDER BY;
        * the filter dropdowns' `SELECT DISTINCT <one column>`, which never
          selects the whole entity;
        * per-row loads emitted while the template renders a deferred column,
          which are keyed by the primary key and return exactly one row.
        """
        found = []
        for statement in self.statements:
            if not re.match(r"(?i)^select\b", statement):
                continue
            if re.match(r"(?i)^select\s+count\(", statement):
                continue
            if f"{table}.source_email_id AS " not in statement:
                continue
            if re.search(rf"{table}\.id = [:?]", statement):
                continue
            found.append(statement)
        return found


def order_by_clause(statement):
    """The statement's trailing ORDER BY without LIMIT/OFFSET, or None.

    The last ORDER BY in the text is the outermost one for every query these
    routes build (none of them orders inside a subquery).
    """
    matches = list(re.finditer(r"(?i)\border by\b", statement))
    if not matches:
        return None
    clause = statement[matches[-1].end() :]
    clause = re.split(r"(?i)\s+limit\s+", clause)[0]
    clause = re.split(r"(?i)\s+offset\s+", clause)[0]
    return clause.strip()


def assert_ends_with_unique_key(recorder, table, surface):
    """The one listing SELECT must break ties on `<table>.id`."""
    selects = recorder.listing_selects(table)
    assert len(selects) == 1, (
        f"{surface}: expected exactly one listing SELECT over {table}, captured "
        f"{len(selects)}. " + " || ".join(s[:160] for s in selects)
    )

    clause = order_by_clause(selects[0])
    assert clause is not None, (
        f"{surface}: the listing SELECT over {table} carries no ORDER BY at all, "
        f"so the row order is whatever the database happened to return "
        f"(issue #113). SQL: {selects[0][:300]}"
    )
    assert re.search(
        rf"(?i)\b{table}\.id(\s+(asc|desc))?(\s+nulls\s+(first|last))?$", clause
    ), (
        f"{surface}: ORDER BY does not end on the unique key {table}.id, so rows "
        f"tied on the sort column are ordered arbitrarily and pagination can "
        f"repeat or drop them (issue #113). ORDER BY {clause}"
    )
    return clause


def assert_orders_on_column(clause, table, sort_by, order, surface):
    """The ORDER BY must *start* on the column the caller asked for.

    This is the guard against over-correcting: tightening the gate that
    decides "is this a sortable column" could silently push a working sort
    into the unknown-sort fallback, and every surface would still end on the
    primary key while quietly ordering by something else.
    """
    assert re.search(rf"(?i)^{table}\.{sort_by}\s+{order}\b", clause), (
        f"{surface}: the requested column no longer leads the ORDER BY, so the "
        f"sort fell through to the fallback branch. ORDER BY {clause}"
    )


def dedupe(values):
    """First occurrence of each value, order preserved.

    The templates stamp the row id on both the row and its favourite button,
    so a raw findall double-counts. Two different rows can never share an id,
    which is what makes this safe.
    """
    seen = set()
    ordered = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def swept_page_ids(client, path, query, id_attr, max_pages=20):
    """Ids in render order across every page of a paginated listing."""
    collected = []
    for page in range(1, max_pages + 1):
        url = f"{path}?{query}&view_type=list&per_page={PER_PAGE}&page={page}"
        response = client.get(url)
        assert response.status_code == 200, f"{url} -> {response.status_code}"
        ids = dedupe(re.findall(rf'{id_attr}="(\d+)"', response.get_data(as_text=True)))
        if not ids:
            return collected
        collected.extend(ids)
    raise AssertionError(f"pagination of {path}?{query} never ran out of pages")


def csv_ids(client, path, query):
    """The ID column of an export, in file order."""
    response = client.get(f"{path}?{query}")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    assert rows, f"{path}?{query} returned an empty CSV"
    id_column = rows[0].index("ID")
    return [row[id_column] for row in rows[1:]]


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


@pytest.fixture
def recorder(app):
    """Records the SQL of the app's own engine for the duration of a test."""
    engine = db.engine
    sql_recorder = SqlRecorder()
    event.listen(engine, "before_cursor_execute", sql_recorder)
    yield sql_recorder
    event.remove(engine, "before_cursor_execute", sql_recorder)


@pytest.fixture
def tied_properties(app):
    """14 properties with deliberate ties on price and a NULL run."""
    with app.app_context():
        profile = SearchProfile(
            name="Issue 113",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()

        ids = {}
        for position, (label, price, score) in enumerate(TIED_ROWS):
            prop = Property(
                source_email_id=f"issue113_prop_{label}",
                title=f"Issue113 {label} UniqueTitle",
                municipality="Cudillero",
                search_profile_id=profile.id,
                listing_status="active",
                property_category="housing",
                property_subtype="house",
                price=price,
                area=100 + position * 10,
                score_total=score,
                score_investment=score,
                score_lifestyle=100 - score,
                created_at=datetime(2026, 8, 1, 9, 0, 0).replace(minute=position),
                ai_analysis=(
                    {"rental_market_analysis": {"investment_rating": RATINGS[label]}}
                    if label in RATINGS
                    else None
                ),
            )
            db.session.add(prop)
            db.session.commit()
            ids[label] = str(prop.id)

        return {"profile_id": profile.id, "ids": ids}


@pytest.fixture
def tied_lands(app):
    """The same tie shape on the archived `lands` table."""
    with app.app_context():
        ids = {}
        for position, (label, price, score) in enumerate(TIED_ROWS):
            land = Land(
                source_email_id=f"issue113_land_{label}",
                title=f"Issue113 {label} UniqueLand",
                municipality="Cudillero",
                listing_status="active",
                price=price,
                area=100 + position * 10,
                score_total=score,
                score_investment=score,
                score_lifestyle=100 - score,
                created_at=datetime(2026, 8, 1, 9, 0, 0).replace(minute=position),
                ai_analysis=(
                    {"rental_market_analysis": {"investment_rating": RATINGS[label]}}
                    if label in RATINGS
                    else None
                ),
            )
            db.session.add(land)
            db.session.commit()
            ids[label] = str(land.id)

        return {"ids": ids}


class TestFixtureReallyTies:
    """Guard the guard: a fixture without ties would prove nothing."""

    def test_properties_fixture_has_a_tie_group_and_a_null_run(
        self, app, tied_properties
    ):
        with app.app_context():
            tied = Property.query.filter(Property.price == TIED_PRICE).count()
            nulls = Property.query.filter(Property.price.is_(None)).count()
            total = Property.query.count()
        assert tied == len(TIED_PRICE_LABELS)
        assert nulls == len(NULL_PRICE_LABELS)
        assert total == len(TIED_ROWS)
        # The tie group has to straddle the page boundary, or paging over it
        # could not repeat or drop a row in the first place.
        assert total > PER_PAGE

    def test_lands_fixture_scores_are_distinct_and_not_in_insertion_order(
        self, tied_lands
    ):
        scores = [score for _, _, score in TIED_ROWS]
        assert len(set(scores)) == len(scores)
        assert scores != sorted(scores, reverse=True)
        assert len(tied_lands["ids"]) == len(TIED_ROWS)


class TestPropertiesPageOrdersByAUniqueKey:
    """Criterion 1: /properties, every sort, both directions."""

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", PROPERTY_SORTS)
    def test_order_by_ends_on_the_primary_key(
        self, client, recorder, tied_properties, sort_by, order
    ):
        query = (
            f"profile_id={tied_properties['profile_id']}&sort={sort_by}&order={order}"
        )
        recorder.reset()
        response = client.get(f"/properties?{query}&view_type=list")

        assert response.status_code == 200
        # The route swallows failures and re-renders an empty page, so an
        # assertion on SQL alone could pass over a broken request.
        assert re.search(r'data-property-id="\d+"', response.get_data(as_text=True)), (
            f"/properties?{query} rendered no rows"
        )
        assert_ends_with_unique_key(
            recorder, "properties", f"/properties?sort={sort_by}&order={order}"
        )

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", ("price", "area", "score_total"))
    def test_nulls_stay_last_alongside_the_tiebreaker(
        self, client, recorder, tied_properties, sort_by, order
    ):
        """Criterion 4: the tiebreaker must not cost us nullslast()."""
        query = (
            f"profile_id={tied_properties['profile_id']}&sort={sort_by}&order={order}"
        )
        recorder.reset()
        client.get(f"/properties?{query}&view_type=list")

        clause = assert_ends_with_unique_key(
            recorder, "properties", f"/properties?sort={sort_by}&order={order}"
        )
        assert re.search(
            rf"(?i)properties\.{sort_by}\s+(asc|desc)\s+nulls last", clause
        ), (
            f"/properties?sort={sort_by} lost NULLS LAST on the sort column: "
            f"ORDER BY {clause}"
        )


class TestPropertiesExportOrdersByAUniqueKey:
    """Criterion 1/3: the export builds its own ORDER BY and must match."""

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", PROPERTY_SORTS)
    def test_order_by_ends_on_the_primary_key(
        self, client, recorder, tied_properties, sort_by, order
    ):
        query = (
            f"profile_id={tied_properties['profile_id']}&sort={sort_by}&order={order}"
        )
        recorder.reset()
        response = client.get(f"/properties/export.csv?{query}")

        assert response.status_code == 200
        assert_ends_with_unique_key(
            recorder,
            "properties",
            f"/properties/export.csv?sort={sort_by}&order={order}",
        )


class TestLandsPageOrdersByAUniqueKey:
    """Criterion 1/5: the archive page, including sorts `Land` lacks."""

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", LAND_SORTS)
    def test_order_by_ends_on_the_primary_key(
        self, client, recorder, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}"
        recorder.reset()
        response = client.get(f"/lands?{query}&view_type=list")

        assert response.status_code == 200
        assert re.search(r'data-land-id="\d+"', response.get_data(as_text=True)), (
            f"/lands?{query} rendered no rows"
        )
        assert_ends_with_unique_key(recorder, "lands", f"/lands?{query}")

    @pytest.mark.parametrize("sort_by", LAND_UNKNOWN_SORTS)
    def test_unknown_sort_still_orders_the_query(
        self, client, recorder, tied_lands, sort_by
    ):
        """An unknown sort currently produces a SELECT with no ORDER BY.

        `/export.csv` already has the fallback branch this page is missing.
        """
        recorder.reset()
        response = client.get(f"/lands?sort={sort_by}&order=desc&view_type=list")

        assert response.status_code == 200
        assert_ends_with_unique_key(recorder, "lands", f"/lands?sort={sort_by}")


class TestLandsExportOrdersByAUniqueKey:
    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", LAND_SORTS)
    def test_order_by_ends_on_the_primary_key(
        self, client, recorder, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}"
        recorder.reset()
        response = client.get(f"/export.csv?{query}")

        assert response.status_code == 200
        assert_ends_with_unique_key(recorder, "lands", f"/export.csv?{query}")

    @pytest.mark.parametrize("sort_by", LAND_UNKNOWN_SORTS)
    def test_unknown_sort_keeps_its_fallback_and_gains_the_tiebreaker(
        self, client, recorder, tied_lands, sort_by
    ):
        recorder.reset()
        client.get(f"/export.csv?sort={sort_by}&order=desc")

        clause = assert_ends_with_unique_key(
            recorder, "lands", f"/export.csv?sort={sort_by}"
        )
        assert "lands.score_total" in clause, (
            f"/export.csv?sort={sort_by} must keep falling back to the mode "
            f"default: ORDER BY {clause}"
        )


class TestJsonApiOrdersByAUniqueKey:
    """Criterion 6: offset/limit windows need a total order too."""

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize(
        "sort_by", ("created_at", "price", "area", "score_total", "score_investment")
    )
    def test_api_properties_order_by_ends_on_the_primary_key(
        self, client, recorder, tied_properties, sort_by, order
    ):
        query = (
            f"profile_id={tied_properties['profile_id']}"
            f"&sort={sort_by}&order={order}&limit={PER_PAGE}&offset=0"
        )
        recorder.reset()
        response = client.get(f"/api/properties?{query}")

        assert response.status_code == 200
        assert response.get_json()["properties"], "/api/properties returned no rows"
        assert_ends_with_unique_key(
            recorder, "properties", f"/api/properties?sort={sort_by}&order={order}"
        )

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", ("price", "area", "score_total"))
    def test_api_lands_order_by_ends_on_the_primary_key(
        self, client, recorder, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}&limit={PER_PAGE}&offset=0"
        recorder.reset()
        response = client.get(f"/api/lands?{query}")

        assert response.status_code == 200
        assert response.get_json()["lands"], "/api/lands returned no rows"
        assert_ends_with_unique_key(recorder, "lands", f"/api/lands?{query}")


class TestPagingCoversEveryRowExactlyOnce:
    """Criteria 2 and 4, behavioural.

    Passes on sqlite before the fix (tied rows come back in rowid order); it
    holds the guarantee for PostgreSQL, where the order is not promised.
    """

    @pytest.mark.parametrize("order", ORDERS)
    def test_properties_pages_neither_repeat_nor_drop_a_row(
        self, client, tied_properties, order
    ):
        query = f"profile_id={tied_properties['profile_id']}&sort=price&order={order}"
        swept = swept_page_ids(client, "/properties", query, "data-property-id")

        assert len(swept) == len(set(swept)), (
            f"a row was shown on two pages of /properties?{query}: {swept}"
        )
        assert set(swept) == set(tied_properties["ids"].values()), (
            f"paging over /properties?{query} lost rows: "
            f"{sorted(set(tied_properties['ids'].values()) - set(swept))}"
        )

    @pytest.mark.parametrize("order", ORDERS)
    def test_properties_null_run_stays_at_the_end_and_repeats(
        self, client, tied_properties, order
    ):
        query = f"profile_id={tied_properties['profile_id']}&sort=price&order={order}"
        swept = swept_page_ids(client, "/properties", query, "data-property-id")

        expected_nulls = {tied_properties["ids"][label] for label in NULL_PRICE_LABELS}
        assert set(swept[-len(expected_nulls) :]) == expected_nulls, (
            f"the NULL-price rows are no longer last on /properties?{query}: {swept}"
        )
        assert swept == swept_page_ids(
            client, "/properties", query, "data-property-id"
        ), f"/properties?{query} returned a different order on the second run"

    @pytest.mark.parametrize("order", ORDERS)
    def test_lands_pages_neither_repeat_nor_drop_a_row(self, client, tied_lands, order):
        query = f"sort=price&order={order}"
        swept = swept_page_ids(client, "/lands", query, "data-land-id")

        assert len(swept) == len(set(swept)), (
            f"a row was shown on two pages of /lands?{query}: {swept}"
        )
        assert set(swept) == set(tied_lands["ids"].values())


class TestExportMatchesTheScreen:
    """Criterion 3: the CSV sequence equals the pages glued together."""

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", ("price", "score_total", "investment_metrics"))
    def test_properties_export_matches_the_swept_pages(
        self, client, tied_properties, sort_by, order
    ):
        query = (
            f"profile_id={tied_properties['profile_id']}&sort={sort_by}&order={order}"
        )
        swept = swept_page_ids(client, "/properties", query, "data-property-id")
        exported = csv_ids(client, "/properties/export.csv", query)

        assert exported == swept, (
            f"/properties/export.csv?{query} disagrees with the page order"
        )

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", ("price", "score_total", "investment_metrics"))
    def test_lands_export_matches_the_swept_pages(
        self, client, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}"
        swept = swept_page_ids(client, "/lands", query, "data-land-id")
        exported = csv_ids(client, "/export.csv", query)

        assert exported == swept, f"/export.csv?{query} disagrees with the page order"

    @pytest.mark.parametrize("sort_by", LAND_UNKNOWN_SORTS)
    def test_lands_unknown_sort_matches_its_own_export(
        self, client, tied_lands, sort_by
    ):
        """Fails today, and not because of sqlite.

        The page applies no ordering at all for a sort `Land` lacks, while
        `/export.csv` falls back to `score_total DESC`. The fixture's scores
        are distinct and deliberately not in insertion order, so the two
        sequences cannot coincide by luck.
        """
        query = f"sort={sort_by}&order=desc"
        swept = swept_page_ids(client, "/lands", query, "data-land-id")
        exported = csv_ids(client, "/export.csv", query)

        assert exported == swept, (
            f"/lands?{query} and /export.csv?{query} order the same rows "
            f"differently: page {swept} vs export {exported}"
        )


class TestApiWindowsDoNotOverlap:
    """Criterion 6, behavioural: consecutive offset windows are disjoint."""

    @pytest.mark.parametrize("order", ORDERS)
    def test_api_properties_windows_cover_every_row_once(
        self, client, tied_properties, order
    ):
        base = (
            f"/api/properties?profile_id={tied_properties['profile_id']}"
            f"&sort=price&order={order}&limit={PER_PAGE}"
        )
        first = [
            str(p["id"])
            for p in client.get(f"{base}&offset=0").get_json()["properties"]
        ]
        second = [
            str(p["id"])
            for p in client.get(f"{base}&offset={PER_PAGE}").get_json()["properties"]
        ]

        assert not set(first) & set(second), (
            f"offset windows of {base} overlap: {sorted(set(first) & set(second))}"
        )
        assert set(first) | set(second) == set(tied_properties["ids"].values())

    @pytest.mark.parametrize("order", ORDERS)
    def test_api_lands_windows_cover_every_row_once(self, client, tied_lands, order):
        base = f"/api/lands?sort=price&order={order}&limit={PER_PAGE}"
        first = [
            str(land["id"])
            for land in client.get(f"{base}&offset=0").get_json()["lands"]
        ]
        second = [
            str(land["id"])
            for land in client.get(f"{base}&offset={PER_PAGE}").get_json()["lands"]
        ]

        assert not set(first) & set(second), (
            f"offset windows of {base} overlap: {sorted(set(first) & set(second))}"
        )
        assert set(first) | set(second) == set(tied_lands["ids"].values())


class TestLandsSortNamingARealButUnsortableAttribute:
    """`hasattr(Land, sort_by)` is the wrong question.

    `to_dict` and `metadata` are genuine attributes of the mapped class, so
    the gate let them into the column branch, where `.desc()` raised. Each
    surface then failed in its own way: the page swallowed the exception and
    rendered an empty listing having run no SELECT at all, `/export.csv`
    redirected instead of returning a CSV, `/api/lands` answered 500. The
    fallback branch these should have taken already existed.
    """

    def test_the_attributes_exist_and_are_not_mapped_columns(self):
        for attr in LAND_NON_COLUMN_ATTRS:
            assert hasattr(Land, attr), (
                f"Land.{attr} no longer exists, so this case no longer covers "
                f"the hasattr() gate it was written for"
            )
            assert attr not in sortable_columns(Land), (
                f"Land.{attr} is a mapped column now; pick another non-column "
                f"attribute or the case proves nothing"
            )

    @pytest.mark.parametrize("sort_by", LAND_NON_COLUMN_ATTRS)
    def test_page_still_runs_an_ordered_listing_select(
        self, client, recorder, tied_lands, sort_by
    ):
        recorder.reset()
        response = client.get(f"/lands?sort={sort_by}&order=desc&view_type=list")

        assert response.status_code == 200
        assert re.search(r'data-land-id="\d+"', response.get_data(as_text=True)), (
            f"/lands?sort={sort_by} rendered no rows: the route raised on the "
            f"sort column and fell into its error handler"
        )
        assert_ends_with_unique_key(recorder, "lands", f"/lands?sort={sort_by}")

    @pytest.mark.parametrize("sort_by", LAND_NON_COLUMN_ATTRS)
    def test_export_stays_a_csv_and_matches_the_page(self, client, tied_lands, sort_by):
        query = f"sort={sort_by}&order=desc"
        response = client.get(f"/export.csv?{query}")

        assert response.status_code == 200, (
            f"/export.csv?{query} answered {response.status_code} instead of a "
            f"CSV (302 means it hit the error handler and redirected)"
        )
        swept = swept_page_ids(client, "/lands", query, "data-land-id")
        exported = csv_ids(client, "/export.csv", query)
        assert exported == swept, (
            f"/lands?{query} and /export.csv?{query} order the same rows "
            f"differently: page {swept} vs export {exported}"
        )

    @pytest.mark.parametrize("sort_by", LAND_NON_COLUMN_ATTRS)
    def test_api_windows_answer_and_do_not_overlap(self, client, tied_lands, sort_by):
        base = f"/api/lands?sort={sort_by}&order=desc&limit=2"
        responses = [client.get(f"{base}&offset={offset}") for offset in (0, 2)]

        for offset, response in zip((0, 2), responses):
            assert response.status_code == 200, (
                f"{base}&offset={offset} answered {response.status_code}"
            )
        windows = [
            [str(land["id"]) for land in response.get_json()["lands"]]
            for response in responses
        ]
        assert all(len(window) == 2 for window in windows), (
            f"{base} did not fill both windows: {windows}"
        )
        assert not set(windows[0]) & set(windows[1]), (
            f"offset windows of {base} overlap: "
            f"{sorted(set(windows[0]) & set(windows[1]))}"
        )


class TestKnownLandSortsKeepTheirOwnColumn:
    """The stricter gate must not demote a sort that always worked.

    Every name here is a mapped column of `Land`, so all three surfaces have
    to keep ordering on it -- ending on the primary key is not enough if the
    leading term silently became the fallback's `score_total`.
    """

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", LAND_COLUMN_SORTS)
    def test_page_orders_on_the_requested_column(
        self, client, recorder, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}"
        recorder.reset()
        response = client.get(f"/lands?{query}&view_type=list")

        assert response.status_code == 200
        clause = assert_ends_with_unique_key(recorder, "lands", f"/lands?{query}")
        assert_orders_on_column(clause, "lands", sort_by, order, f"/lands?{query}")

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", LAND_COLUMN_SORTS)
    def test_export_orders_on_the_requested_column(
        self, client, recorder, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}"
        recorder.reset()
        response = client.get(f"/export.csv?{query}")

        assert response.status_code == 200
        clause = assert_ends_with_unique_key(recorder, "lands", f"/export.csv?{query}")
        assert_orders_on_column(clause, "lands", sort_by, order, f"/export.csv?{query}")

    @pytest.mark.parametrize("order", ORDERS)
    @pytest.mark.parametrize("sort_by", LAND_COLUMN_SORTS)
    def test_api_orders_on_the_requested_column(
        self, client, recorder, tied_lands, sort_by, order
    ):
        query = f"sort={sort_by}&order={order}&limit={PER_PAGE}&offset=0"
        recorder.reset()
        response = client.get(f"/api/lands?{query}")

        assert response.status_code == 200
        clause = assert_ends_with_unique_key(recorder, "lands", f"/api/lands?{query}")
        assert_orders_on_column(clause, "lands", sort_by, order, f"/api/lands?{query}")


class TestPropertiesExportResolvesItsDefaultLikeThePage:
    """Criterion 3 for the URLs that carry `mode` instead of a usable sort.

    `/properties` picks its default sort from `mode` and normalises an unknown
    sort onto that default; its export used to jump straight to `created_at`,
    so one URL produced two different orders. The fixture's scores are
    distinct and deliberately not in creation order, so the two sequences
    cannot coincide by luck.
    """

    @pytest.mark.parametrize(
        "sort_tail", ("", "&sort=travel_time_nearest_beach", "&sort=")
    )
    @pytest.mark.parametrize("mode", PROPERTY_MODES)
    def test_export_matches_the_page_for_a_mode_url(
        self, client, tied_properties, mode, sort_tail
    ):
        query = (
            f"profile_id={tied_properties['profile_id']}"
            f"&mode={mode}&order=desc{sort_tail}"
        )
        swept = swept_page_ids(client, "/properties", query, "data-property-id")
        exported = csv_ids(client, "/properties/export.csv", query)

        assert exported == swept, (
            f"/properties/export.csv?{query} disagrees with the page order: "
            f"page {swept} vs export {exported}"
        )
