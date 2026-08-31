"""The subscription's criteria: pass / fail / unknown, in both languages.

One matrix through the Python reader AND the SQL expressions (the
`advertiser.py` contract) — a count that disagrees with the verdicts under
it is a third wrong number. Then the surfaces: the default view hides only
measured fails and NEVER a favorited or reviewed row; the disclosure line
counts what was hidden; the map and the CSV read the same parameter (#445's
rule: a filter one surface keeps and another drops is the regression).
"""

import csv
import io

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import subscription_criteria
from tests import setup_test_environment

CRITERIA = {"min_house_m2": 150.0, "min_plot_m2": 700.0}


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile_row(app):
    row = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    db.session.add(row)
    db.session.commit()
    return row


_SEQ = iter(range(1, 10_000))


def _mk(profile_id, **overrides):
    values = dict(
        source_email_id=f"crit:{next(_SEQ)}",
        title=f"Listing {next(_SEQ)}",
        price=100000,
        search_profile_id=profile_id,
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


# (area, area_type, plot_area, expected_state) — the one matrix both
# languages run.
MATRIX = [
    (200, "built", 800, "pass"),
    (200, "built", 650, "fail"),  # plot measurably short
    (120, "built", 800, "fail"),  # house measurably short
    (200, "built", None, "unknown"),  # plot never measured
    (None, "built", 800, "unknown"),  # house never measured
    (200, None, 800, "pass"),  # NULL area_type reads as built
    (800, "plot", None, "unknown"),  # bare land: area IS the plot, house unknown
    (650, "plot", None, "fail"),  # bare land, plot short
    (650, "plot", 900, "pass_or_unknown_house"),  # plot_area wins over area
    (0, "built", 800, "unknown"),  # zero is a blank, never a tiny house
    (200, "built", 0, "unknown"),  # zero plot is a blank too
]


class TestTheTwoReadingsAgree:
    @pytest.mark.parametrize("area, area_type, plot, expected", MATRIX)
    def test_python_and_sql_answer_alike(
        self, app, profile_row, area, area_type, plot, expected
    ):
        prop = _mk(profile_row.id, area=area, area_type=area_type, plot_area=plot)
        verdict = subscription_criteria.read_verdict(prop, CRITERIA)

        if expected == "pass_or_unknown_house":
            # A bare-land row with a stated plot passes the plot bound but
            # can never answer the house bound — unknown, both languages.
            expected = "unknown"
        assert verdict["state"] == expected, (
            f"python said {verdict['state']} for area={area}/{area_type}, plot={plot}"
        )

        fails = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.failing_expression(Property, CRITERIA)
            )
        }
        passes = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.passing_expression(Property, CRITERIA)
            )
        }
        assert (prop.id in fails) == (expected == "fail"), "SQL fail disagrees"
        assert (prop.id in passes) == (expected == "pass"), "SQL pass disagrees"
        # unknown is ~fail AND ~pass, and both expressions are definite per
        # row — the NULL third value would silently eat rows here.
        unknowns = {
            p.id
            for p in Property.query.filter(
                ~subscription_criteria.failing_expression(Property, CRITERIA),
                ~subscription_criteria.passing_expression(Property, CRITERIA),
            )
        }
        assert (prop.id in unknowns) == (expected == "unknown"), "SQL unknown disagrees"

    def test_no_criteria_and_malformed_criteria_read_as_none(self, app):
        clean = SearchProfile(name="No criteria", is_active=True)
        broken = SearchProfile(
            name="Broken", is_active=True, criteria={"min_house_m2": "big"}
        )
        negative = SearchProfile(
            name="Negative", is_active=True, criteria={"min_plot_m2": -5}
        )
        db.session.add_all([clean, broken, negative])
        db.session.commit()
        assert subscription_criteria.read_criteria(clean) is None
        assert subscription_criteria.read_criteria(broken) is None
        assert subscription_criteria.read_criteria(negative) is None

    def test_the_owner_judgement_is_never_hidden(self, app, profile_row):
        failing = _mk(profile_row.id, area=100, area_type="built")
        favorite = _mk(profile_row.id, area=100, area_type="built", is_favorite=True)
        reviewed = _mk(profile_row.id, area=100, area_type="built")
        reviewed.owner_verdict = "interested"
        db.session.commit()
        hidden = {
            p.id
            for p in Property.query.filter(
                subscription_criteria.hidden_by_default_expression(Property, CRITERIA)
            )
        }
        assert failing.id in hidden
        assert favorite.id not in hidden
        assert reviewed.id not in hidden


class TestTheSurfaces:
    @pytest.fixture
    def rows(self, app, profile_row):
        passing = _mk(
            profile_row.id,
            title="Passing house",
            area=200,
            area_type="built",
            plot_area=900,
        )
        failing = _mk(profile_row.id, title="Failing tiny", area=100, area_type="built")
        unknown = _mk(profile_row.id, title="Unknown plot", area=200, area_type="built")
        return {"pass": passing, "fail": failing, "unknown": unknown}

    def test_the_default_view_hides_only_measured_fails(self, client, rows):
        html = client.get("/properties").data.decode()
        assert "Passing house" in html
        assert "Unknown plot" in html
        assert "Failing tiny" not in html
        assert "Criteria: 1 failing hidden" in html

    def test_criteria_all_shows_everything_and_no_disclosure(self, client, rows):
        html = client.get("/properties?criteria=all").data.decode()
        assert "Failing tiny" in html
        assert "failing hidden" not in html

    @pytest.mark.parametrize(
        "mode, visible, hidden",
        [
            ("pass", ["Passing house"], ["Failing tiny", "Unknown plot"]),
            ("fail", ["Failing tiny"], ["Passing house", "Unknown plot"]),
            ("unknown", ["Unknown plot"], ["Passing house", "Failing tiny"]),
        ],
    )
    def test_each_verdict_mode_selects_its_rows(
        self, client, rows, mode, visible, hidden
    ):
        html = client.get(f"/properties?criteria={mode}").data.decode()
        for title in visible:
            assert title in html, f"{mode} lost {title}"
        for title in hidden:
            assert title not in html, f"{mode} leaked {title}"

    def test_the_map_reads_the_same_parameter(self, client, rows):
        for prop in rows.values():
            prop.location_lat = 43.0
            prop.location_lon = -9.0
        db.session.commit()
        default_map = client.get("/map").data.decode()
        assert "Failing tiny" not in default_map
        wide_map = client.get("/map?criteria=all").data.decode()
        assert "Failing tiny" in wide_map

    def test_the_csv_reads_the_same_parameter(self, client, rows):
        body = client.get("/properties/export.csv").data.decode()
        titles = [row[2] for row in csv.reader(io.StringIO(body))]
        assert "Passing house" in " ".join(titles)
        assert "Failing tiny" not in " ".join(titles)
        wide = client.get("/properties/export.csv?criteria=all").data.decode()
        assert "Failing tiny" in wide

    def test_without_criteria_the_control_is_absent_and_nothing_hides(
        self, client, app
    ):
        bare = SearchProfile(name="Plain", is_active=True)
        db.session.add(bare)
        db.session.commit()
        _mk(bare.id, title="Tiny but shown", area=50, area_type="built")
        html = client.get("/properties").data.decode()
        assert "Tiny but shown" in html
        assert 'name="criteria"' not in html
