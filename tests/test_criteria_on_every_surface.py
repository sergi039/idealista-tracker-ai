"""Two surfaces that dropped or ignored the subscription criteria in silence.

Both are #445's rule — *a filter one surface keeps and another drops is the
regression* — measured against production on 2026-08-31, where the one
subscription carrying criteria (24, `Galicia · costa`, min 150 m² built on
700 m² of plot) holds 443 listings of which 59 are measured fails:

* **`/properties/export.csv` applied the filter and said nothing.** A default
  export returned 384 data rows against 443 under `criteria=all` — 59 rows
  gone — and its 71 columns carried `Hazard Scan Complete`, `Taste State`,
  `Owner Verdict` and the listing-status verdict but neither the criteria
  verdict that decided which rows were in the file nor `plot_area`, the
  figure that verdict rests on. So the file both dropped rows and could not
  be used to work out which.
* **`GET /api/properties` accepted `criteria` and ignored it.**
  `?profile_id=24` answered `total: 443` and `?profile_id=24&criteria=fail`
  answered `total: 443` as well, while its own scope block claimed
  `"basis": "rows as stored, no adjustment"` with empty notes. Nothing in
  `templates/` or `static/` calls that endpoint, so the defect was latent —
  which changes who it costs, not whether it is one.

The assertions are BY VALUE throughout: a column that is present and full of
blanks, or a `total` that happens to match because the fixture has no failing
row, is the shape of test this repository keeps catching itself writing.
"""

import csv
import io

from datetime import date, timedelta

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


_SEQ = iter(range(1, 10_000))


def _mk(profile_id, **overrides):
    values = dict(
        source_email_id=f"surfaces:{next(_SEQ)}",
        title=f"Listing {next(_SEQ)}",
        price=100000,
        search_profile_id=profile_id,
        listing_status="active",
    )
    values.update(overrides)
    prop = Property(**values)
    db.session.add(prop)
    db.session.commit()
    return prop


@pytest.fixture
def profile_row(app):
    row = SearchProfile(name="Galicia · costa", is_active=True, criteria=CRITERIA)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def rows(app, profile_row):
    """One listing per verdict, each with figures the assertions can check.

    The plot figures are deliberately different numbers (900 / none / 650) so
    a `Plot Area` column that shifted by one, or that exported `area` instead,
    cannot land them all correctly by accident.
    """
    return {
        "pass": _mk(
            profile_row.id,
            title="Passing house",
            area=200,
            area_type="built",
            plot_area=900,
        ),
        "unknown": _mk(
            profile_row.id,
            title="Unknown plot",
            area=200,
            area_type="built",
        ),
        "fail": _mk(
            profile_row.id,
            title="Failing tiny",
            area=200,
            area_type="built",
            plot_area=650,
        ),
    }


def _csv(client, query=""):
    response = client.get(f"/properties/export.csv{query}")
    assert response.status_code == 200, response.status_code
    parsed = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    header, *data = parsed
    return header, [dict(zip(header, row)) for row in data]


class TestTheExportSaysWhatItDropped:
    def test_the_criteria_verdict_is_a_column_and_carries_all_four_states(
        self, client, rows, profile_row
    ):
        """The verdict that decides whether a row is in the file at all.

        Asked at `criteria=all`, because that is the export in which every
        verdict is present — which is the whole recompute story: with this
        column a reader of a wide export can name the rows a default export
        drops.
        """
        bare = SearchProfile(name="No bounds", is_active=True)
        db.session.add(bare)
        db.session.commit()
        _mk(bare.id, title="Outside the criteria", area=10, area_type="built")

        header, data = _csv(client, "?profile_id=all&criteria=all")
        assert "Criteria" in header, "the export states no criteria verdict at all"

        by_title = {row["Title"]: row for row in data}
        assert by_title["Passing house"]["Criteria"] == "pass"
        assert by_title["Failing tiny"]["Criteria"] == "fail"
        # `unknown` is NOT `fail`: a plot nobody has stated is not a plot
        # that is too small.
        assert by_title["Unknown plot"]["Criteria"] == "unknown"
        # A subscription that sets no bounds gives no verdict, and its rows
        # are never touched by anybody else's.
        assert by_title["Outside the criteria"]["Criteria"] == "no_criteria"

    def test_the_plot_area_the_verdict_rests_on_is_a_column(self, client, rows):
        """By value, and per row: a column of blanks would satisfy presence.

        `Plot Area` was in no column of the 71, so a spreadsheet could not
        check a `fail` or tell an unmeasured plot from a small one.
        """
        header, data = _csv(client, "?criteria=all")
        assert "Plot Area (m²)" in header, "the parcel figure is in no column"

        by_title = {row["Title"]: row for row in data}
        assert float(by_title["Passing house"]["Plot Area (m²)"]) == 900.0
        assert float(by_title["Failing tiny"]["Plot Area (m²)"]) == 650.0
        # Nobody stated one. Blank, and the verdict beside it says `unknown`
        # rather than presenting the absence as a measurement.
        assert by_title["Unknown plot"]["Plot Area (m²)"] == ""
        assert by_title["Unknown plot"]["Criteria"] == "unknown"
        # And it is the PLOT, not the built surface next to it: all three
        # rows carry area=200, so a column reading `area` would say 200.0.
        assert by_title["Passing house"]["Area (m²)"] == "200.0"

    def test_a_stored_zero_plot_is_exported_as_zero_and_read_as_a_blank(
        self, client, profile_row
    ):
        """Fotocasa writes 0 where it has no figure, and the criteria reader
        treats that 0 as unmeasured. The export states what is STORED and
        lets the verdict column say how it was read -- blanking the cell
        would make "the portal said nothing" and "the portal said zero" the
        same cell."""
        _mk(profile_row.id, title="Zero plot", area=200, area_type="built", plot_area=0)

        _, data = _csv(client, "?criteria=all")
        row = next(row for row in data if row["Title"] == "Zero plot")
        assert float(row["Plot Area (m²)"]) == 0.0
        assert row["Criteria"] == "unknown"

    def test_the_default_export_still_drops_the_fails_and_now_names_them(
        self, client, rows
    ):
        """#445's rule is kept, not traded away: the export keeps applying the
        filter. What changed is that a wide export can now say which rows the
        narrow one omits, and the columns that exempt a row are already here.

        The recompute below is the file's own arithmetic and it is a
        disclosure rather than a guarantee, in one direction stated where the
        column is defined: the hide reads `owner_verdict IS NULL` while the
        column states the verdict, and an `owner_verdict` no writer of this
        application produces reads `undecided` there while the row is kept.
        Such a row would be over-named as dropped, never under-named.
        """
        _, narrow = _csv(client)
        _, wide = _csv(client, "?criteria=all")

        narrow_titles = {row["Title"] for row in narrow}
        wide_titles = {row["Title"] for row in wide}
        assert "Failing tiny" not in narrow_titles
        assert "Failing tiny" in wide_titles

        dropped = wide_titles - narrow_titles
        # Recomputed from the wide export alone: a measured fail that no
        # column exempts. This IS the arithmetic the file could not support.
        recomputed = {
            row["Title"]
            for row in wide
            if row["Criteria"] == "fail"
            and row["Favorite"] != "True"
            and row["Owner Verdict"] == "undecided"
            and row["Next Action State"] == "none"
        }
        assert recomputed == dropped == {"Failing tiny"}

    def test_a_row_the_owner_judged_exports_fail_and_is_not_dropped(self, client, rows):
        """The exemption has to be visible, or the recompute above is wrong
        for exactly the rows it is most likely to be asked about."""
        rows["fail"].is_favorite = True
        db.session.commit()

        _, narrow = _csv(client)
        row = next(row for row in narrow if row["Title"] == "Failing tiny")
        assert row["Criteria"] == "fail", (
            "a favorited row is kept, and the export must still say the "
            "verdict is a measured fail rather than softening it"
        )
        assert row["Favorite"] == "True"


class TestTheApiReadsTheSameParameter:
    def _scope(self, client, query):
        response = client.get(f"/api/properties{query}")
        assert response.status_code == 200, response.status_code
        payload = response.get_json()
        assert payload["success"] is True
        return payload

    def test_the_default_reading_hides_the_measured_fails(
        self, client, rows, profile_row
    ):
        """`?criteria=fail` used to answer with the whole subscription."""
        payload = self._scope(client, f"?profile_id={profile_row.id}")
        assert payload["scope"]["total"] == 2, (
            "the endpoint must apply the same default hide as /properties"
        )
        titles = {p["title"] for p in payload["properties"]}
        assert titles == {"Passing house", "Unknown plot"}

    @pytest.mark.parametrize(
        "mode, expected",
        [
            ("all", {"Passing house", "Unknown plot", "Failing tiny"}),
            ("fail", {"Failing tiny"}),
            ("pass", {"Passing house"}),
            ("unknown", {"Unknown plot"}),
        ],
    )
    def test_each_mode_selects_the_same_rows_the_page_does(
        self, client, rows, profile_row, mode, expected
    ):
        payload = self._scope(client, f"?profile_id={profile_row.id}&criteria={mode}")
        assert {p["title"] for p in payload["properties"]} == expected
        assert payload["scope"]["total"] == len(expected)

    def test_the_scope_block_states_the_narrowing_it_applied(
        self, client, rows, profile_row
    ):
        """A smaller number with nothing saying what it excluded reads as
        "that is all there is". The block used to carry `basis: rows as
        stored, no adjustment` and no notes at all."""
        scope = self._scope(client, f"?profile_id={profile_row.id}")["scope"]

        assert scope["criteria_applied"] == "default"
        assert scope["criteria_requested"] is None
        assert scope["criteria_recognized"] is True
        assert scope["criteria_hidden_by_default"] == 1
        joined = " ".join(scope["notes"])
        assert "1 listing(s) failing their subscription's criteria" in joined, joined
        assert "criteria=all" in joined, joined

    def test_a_widened_answer_reports_no_hidden_count_rather_than_zero(
        self, client, rows, profile_row
    ):
        """`0` there would claim a count somebody took. Nothing was hidden by
        a rule nobody asked for, so there is no such number (#98)."""
        scope = self._scope(client, f"?profile_id={profile_row.id}&criteria=all")[
            "scope"
        ]
        assert scope["criteria_hidden_by_default"] is None
        assert scope["criteria_applied"] == "all"
        assert any("criteria=all" in note for note in scope["notes"]), scope["notes"]

    def test_an_unreadable_mode_is_named_rather_than_quietly_hiding_rows(
        self, client, rows, profile_row
    ):
        """`criteria=failing` is not a mode, and the reading it falls back to
        is the one that HIDES rows -- so a caller who asked for the fails and
        typed it wrong would receive everything BUT the fails."""
        payload = self._scope(client, f"?profile_id={profile_row.id}&criteria=failing")
        scope = payload["scope"]
        assert scope["criteria_recognized"] is False
        assert scope["criteria_requested"] == "failing"
        assert scope["criteria_applied"] == "default"
        assert scope["total"] == 2
        assert any("is not a criteria mode" in note for note in scope["notes"]), scope[
            "notes"
        ]

    def test_every_row_carries_its_own_verdict_in_both_payload_shapes(
        self, client, rows, profile_row
    ):
        """The compact payload is the DEFAULT one, so a field added to
        `to_dict` alone is missing exactly where most consumers look."""
        for suffix in ("", "&full=1"):
            payload = self._scope(
                client, f"?profile_id={profile_row.id}&criteria=all{suffix}"
            )
            states = {p["title"]: p["criteria_state"] for p in payload["properties"]}
            assert states == {
                "Passing house": "pass",
                "Unknown plot": "unknown",
                "Failing tiny": "fail",
            }, f"full={bool(suffix)} answered {states}"

    def test_a_subscription_with_no_criteria_has_no_verdict_and_is_not_hidden(
        self, client, app
    ):
        bare = SearchProfile(name="Plain", is_active=True)
        db.session.add(bare)
        db.session.commit()
        _mk(bare.id, title="Tiny but shown", area=50, area_type="built")

        payload = self._scope(client, f"?profile_id={bare.id}")
        assert payload["scope"]["total"] == 1
        assert payload["properties"][0]["criteria_state"] == "no_criteria"

    def test_the_dormant_state_says_that_it_selected_nothing(self, client, app):
        """No subscription carries criteria, so no listing has a verdict and
        `criteria=fail` cannot select. Answering it with every row and no
        note reads as "every one of these fails"."""
        bare = SearchProfile(name="Plain", is_active=True)
        db.session.add(bare)
        db.session.commit()
        _mk(bare.id, title="Tiny but shown", area=50, area_type="built")

        scope = self._scope(client, f"?profile_id={bare.id}&criteria=fail")["scope"]
        assert scope["total"] == 1
        assert scope["criteria_hidden_by_default"] is None
        assert any("selected nothing" in note for note in scope["notes"]), scope[
            "notes"
        ]

    def test_the_subscription_mix_describes_the_narrowed_answer(
        self, client, rows, profile_row
    ):
        """The mix is tallied over the population, so it has to be counted
        after the criteria filter -- otherwise the scope block describes a set
        the payload is not a page of."""
        scope = self._scope(client, f"?profile_id={profile_row.id}")["scope"]
        assert scope["total"] == 2
        assert scope["subscriptions"]["listings"] == 2


class TestTheReadingHasOneHome:
    def test_the_page_and_the_api_run_the_same_module(self, client, rows, profile_row):
        """Not a spelling check: the four surfaces are compared by the ROWS
        they return, so a second copy of the reading that drifted would show
        up here even while both copies parsed."""
        for mode in ("all", "fail", "pass", "unknown", "default"):
            api = client.get(
                f"/api/properties?profile_id={profile_row.id}&criteria={mode}"
            ).get_json()
            api_ids = {p["id"] for p in api["properties"]}

            _, exported = _csv(client, f"?profile_id={profile_row.id}&criteria={mode}")
            csv_ids = {int(row["ID"]) for row in exported}

            assert api_ids == csv_ids, (
                f"criteria={mode}: the API returned {sorted(api_ids)} and the "
                f"export {sorted(csv_ids)}"
            )

    def test_an_unrecognised_mode_reads_as_the_default_everywhere(
        self, client, rows, profile_row
    ):
        api = client.get(
            f"/api/properties?profile_id={profile_row.id}&criteria=failing"
        ).get_json()
        _, exported = _csv(client, f"?profile_id={profile_row.id}&criteria=failing")
        assert {p["id"] for p in api["properties"]} == {
            int(row["ID"]) for row in exported
        }

    def test_row_verdict_answers_what_the_sql_clauses_selected(self, app, rows):
        """The Python twin, on the rows the SQL twin picked -- the
        `advertiser.py` contract, applied to the parameter's reading."""
        ctx = subscription_criteria.profile_context(Property)
        assert ctx is not None

        for mode, key in (("fail", "fail"), ("pass", "pass")):
            selected, _ = subscription_criteria.apply_filter(Property.query, ctx, mode)
            picked = {prop.id for prop in selected}
            assert picked == {rows[key].id}, f"{mode} selected {picked}"
            for prop in selected:
                assert subscription_criteria.row_verdict(prop, ctx)["state"] == mode

    def test_an_open_action_exempts_a_failing_row_from_both_surfaces(
        self, client, rows, profile_row
    ):
        """`hidden_by_default_expression` exempts an outstanding action as
        well as a favorite and a verdict, and both surfaces read it through
        the same clause -- so a fail with a due date is kept, and both say
        `fail`."""
        rows["fail"].next_action = "Call the agency"
        rows["fail"].next_action_due_on = date.today() + timedelta(days=3)
        db.session.commit()

        payload = client.get(f"/api/properties?profile_id={profile_row.id}").get_json()
        assert payload["scope"]["criteria_hidden_by_default"] == 0
        assert "Failing tiny" in {p["title"] for p in payload["properties"]}

        _, exported = _csv(client, f"?profile_id={profile_row.id}")
        row = next(row for row in exported if row["Title"] == "Failing tiny")
        assert row["Criteria"] == "fail"
