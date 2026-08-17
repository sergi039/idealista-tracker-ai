"""Creating listings from the owner's own research sheet.

The sheet that prompted this carried 50 listings across five sites and **not
one fotocasa**, so `services/fotocasa_import.py` -- which reads a portal page
-- could take none of it. Idealista answers DataDome to this machine and the
other three sites nothing here can read, so the data comes from the sheet and
the row says so.

Two rules carry the honesty of that, and both are pinned below: the provenance
names the sheet rather than a portal and carries **no** coordinate block, since
there is no portal pin behind these numbers; and the research notes are
labelled as notes, because `description` is where the advert's own words go and
the AI valuation reads it as such.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.coordinate_quality import portal_coordinate
from tests import setup_test_environment
from utils import import_research_sheet as sheet_import

SHEET = "Asturias research (Aug 2026)"

HEADER = (
    "Rank,Priority,Location,Municipality,Price €,Plot m²,€/m²,Type,"
    "Planning Status,Buildable m²,Utilities,Key Positives,Key Risks / Notes,"
    "Source,Direct URL,Ref / ID,Updated,Seller Type"
)


def _csv(tmp_path, *lines, header=HEADER):
    path = tmp_path / "sheet.csv"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return str(path)


PLOT = (
    '8,A,"San Martín de Podes, Gozón",Gozón,45000,1552,29,Land,'
    "Edificable (up to 300 m² claimed),300,Not confirmed,"
    '"Flat, sunny, distant sea views",Exact address hidden,'
    "Yaencontre,https://www.yaencontre.com/venta/terreno/inmueble-46389-112263617,"
    "46389-112263617,,Agency: Rianorte"
)


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


class TestReading:
    def test_a_row_becomes_the_columns_the_scorer_needs(self, tmp_path):
        rows = sheet_import.read_rows(_csv(tmp_path, PLOT), SHEET)

        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == sheet_import.STATUS_NEW
        assert row["title"] == "San Martín de Podes, Gozón"
        assert row["municipality"] == "Gozón"
        assert row["price"] == 45000
        assert row["area"] == 1552
        assert row["area_type"] == "plot"

    def test_a_house_is_not_a_plot(self, tmp_path):
        house = PLOT.replace(",Land,", ",House (reform),")
        rows = sheet_import.read_rows(_csv(tmp_path, house), SHEET)

        assert rows[0]["area_type"] == "built"

    def test_the_notes_say_they_are_notes(self, tmp_path):
        """`description` is read by the AI valuation as the advert's own words."""
        rows = sheet_import.read_rows(_csv(tmp_path, PLOT), SHEET)

        description = rows[0]["description"]
        assert description.startswith(f"Research notes from {SHEET}")
        assert "not the advert text" in description
        assert "Planning: Edificable" in description
        assert "Positives: Flat, sunny, distant sea views" in description

    def test_a_row_with_no_link_is_rejected_not_guessed(self, tmp_path):
        no_url = PLOT.replace(
            "https://www.yaencontre.com/venta/terreno/inmueble-46389-112263617", ""
        )
        rows = sheet_import.read_rows(_csv(tmp_path, no_url), SHEET)

        assert rows[0]["status"] == sheet_import.STATUS_REJECTED
        assert rows[0]["reason"] == "no link in the sheet"

    def test_a_renamed_column_is_refused_rather_than_half_read(self, tmp_path):
        """A silently empty price is a listing with no price, and this cannot
        tell that from a column somebody renamed."""
        path = _csv(tmp_path, PLOT, header=HEADER.replace("Price €", "Precio"))

        with pytest.raises(SystemExit):
            sheet_import.read_rows(path, SHEET)

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("45000", 45000),
            ("€45 000", 45000),
            ("14.3", 14.3),
            # Ambiguous: forty-five thousand to a Spanish sheet, forty-five to
            # a decimal parser, and nothing in the string says which.
            ("45.000", None),
            ("45,000", None),
            ("0", None),
            ("", None),
            ("n/a", None),
        ],
    )
    def test_numbers_are_read_or_absent_never_guessed(self, raw, expected):
        assert sheet_import._number(raw) == expected


class TestDuplicates:
    def test_a_listing_already_here_by_its_idealista_id_is_not_imported_twice(
        self, app, tmp_path
    ):
        """The measured case: 15 of the sheet's 50 were already in the table,
        and only 2 of those would have been found by comparing URLs exactly --
        the stored links carry a `?utm_...` tail the sheet's copy does not."""
        with app.app_context():
            db.session.add(
                Property(
                    source_email_id="alert-1",
                    title="Already ingested from the alert email",
                    idealista_property_id=112111908,
                    url=(
                        "https://www.idealista.com/en/inmueble/112111908/"
                        "?utm_medium=email&utm_campaign=express_newAd_sale_particular"
                    ),
                )
            )
            db.session.commit()

            line = PLOT.replace(
                "https://www.yaencontre.com/venta/terreno/inmueble-46389-112263617",
                "https://www.idealista.com/es/inmueble/112111908/",
            )
            rows = sheet_import.read_rows(_csv(tmp_path, line), SHEET)
            sheet_import.mark_duplicates(rows)

            assert rows[0]["status"] == sheet_import.STATUS_DUPLICATE
            assert rows[0]["existing_id"] is not None

    def test_a_link_nobody_has_is_new(self, app, tmp_path):
        with app.app_context():
            rows = sheet_import.read_rows(_csv(tmp_path, PLOT), SHEET)
            sheet_import.mark_duplicates(rows)

            assert rows[0]["status"] == sheet_import.STATUS_NEW


class TestWriting:
    def _import(self, app, tmp_path, line=PLOT):
        rows = sheet_import.read_rows(_csv(tmp_path, line), SHEET)
        sheet_import.mark_duplicates(rows)
        profile = SearchProfile(name="Manus AI", is_active=True)
        db.session.add(profile)
        db.session.commit()
        outcome = sheet_import.insert_rows(rows, profile_id=profile.id, sheet=SHEET)
        return db.session.get(Property, outcome["created"][0]["id"])

    def test_the_row_carries_what_the_sheet_said(self, app, tmp_path):
        with app.app_context():
            prop = self._import(app, tmp_path)

            assert prop.price == 45000
            assert prop.area == 1552
            assert prop.municipality == "Gozón"
            assert prop.deal_type == "sale"

    def test_the_provenance_names_the_sheet_and_no_portal(self, app, tmp_path):
        with app.app_context():
            prop = self._import(app, tmp_path)

            block = prop.enrichment["import"]
            assert block["source"] == "research_sheet"
            assert block["sheet"] == SHEET
            assert block["research"]["Seller Type"] == "Agency: Rianorte"

    def test_there_is_no_portal_pin_because_there_is_no_portal(self, app, tmp_path):
        """Inventing one would be #393 with the evidence fabricated rather than
        merely missing."""
        with app.app_context():
            prop = self._import(app, tmp_path)

            assert prop.location_lat is None
            assert portal_coordinate(prop) is None
            assert "coordinate" not in prop.enrichment["import"]

    def test_nobody_checked_it_so_the_status_source_is_null(self, app, tmp_path):
        with app.app_context():
            prop = self._import(app, tmp_path)

            assert prop.listing_status_source is None

            from services.listing_verification import read_verdict

            assert read_verdict(prop)["state"] == "unchecked"

    def test_the_same_sheet_imported_twice_creates_one_row(self, app, tmp_path):
        with app.app_context():
            first = self._import(app, tmp_path)

            rows = sheet_import.read_rows(_csv(tmp_path, PLOT), SHEET)
            sheet_import.mark_duplicates(rows)

            assert rows[0]["status"] == sheet_import.STATUS_DUPLICATE
            assert rows[0]["existing_id"] == first.id


class TestAmbiguity:
    def test_an_unreadable_price_refuses_the_row_rather_than_importing_it_blank(
        self, tmp_path
    ):
        """A listing with a quietly missing price scores as if it had none."""
        line = PLOT.replace(",45000,1552,", ",45.000,1552,")
        rows = sheet_import.read_rows(_csv(tmp_path, line), SHEET)

        assert rows[0]["status"] == sheet_import.STATUS_REJECTED
        assert "ambiguous" in rows[0]["reason"]
        assert "45.000" in rows[0]["reason"]


class TestRowsThatShareALink:
    """Six of the sheet's 50 rows share two links, and they are not duplicates.

    No direct listing URL existed for them, so the owner recorded the category
    page: four different Ribadesella plots behind
    `.../terrenos/ribadesella/e-baratos`, two in Folgueras behind a `/geo/`
    page. Keying identity on the URL made the second insert violate the unique
    constraint -- which is how the first real run of this importer failed --
    and folding them together would have discarded four real listings.
    """

    SHARED = "https://www.yaencontre.com/venta/terrenos/ribadesella/e-baratos"

    def _line(self, location, price, area):
        return (
            f'1,A,"{location}",Ribadesella,{price},{area},20,Land,'
            f"Edificable,300,Not confirmed,Positive,Risk,Yaencontre,"
            f"{self.SHARED},,,Unknown"
        )

    def test_four_plots_behind_one_link_are_four_listings(self, app, tmp_path):
        lines = [
            self._line("Soto, Ribadesella", 45000, 1906),
            self._line("Ribadesella (claimed license)", 58000, 1370),
            self._line("~3 km from Ribadesella beaches", 60000, 2046),
            self._line("Soto, Ribadesella", 60000, 2373),
        ]
        with app.app_context():
            rows = sheet_import.read_rows(_csv(tmp_path, *lines), SHEET)
            sheet_import.mark_duplicates(rows)

            assert all(r["status"] == sheet_import.STATUS_NEW for r in rows)
            keys = {sheet_import.source_email_id_for(r) for r in rows}
            assert len(keys) == 4

            profile = SearchProfile(name="Manus AI", is_active=True)
            db.session.add(profile)
            db.session.commit()
            outcome = sheet_import.insert_rows(rows, profile_id=profile.id, sheet=SHEET)

            assert len(outcome["created"]) == 4
            assert Property.query.count() == 4

    def test_two_rows_alike_in_every_recorded_way_are_one_listing(self, app, tmp_path):
        """The key is what the sheet records; rows it cannot tell apart are
        one row, and the second is skipped rather than aborting the batch."""
        same = self._line("Soto, Ribadesella", 45000, 1906)
        with app.app_context():
            rows = sheet_import.read_rows(_csv(tmp_path, same, same), SHEET)
            sheet_import.mark_duplicates(rows)

            profile = SearchProfile(name="Manus AI", is_active=True)
            db.session.add(profile)
            db.session.commit()
            outcome = sheet_import.insert_rows(rows, profile_id=profile.id, sheet=SHEET)

            assert len(outcome["created"]) == 1
            assert len(outcome["skipped"]) == 1
            assert Property.query.count() == 1

    def test_importing_the_same_sheet_twice_adds_nothing(self, app, tmp_path):
        lines = [
            self._line("Soto, Ribadesella", 45000, 1906),
            self._line("Ribadesella (claimed license)", 58000, 1370),
        ]
        with app.app_context():
            profile = SearchProfile(name="Manus AI", is_active=True)
            db.session.add(profile)
            db.session.commit()

            rows = sheet_import.read_rows(_csv(tmp_path, *lines), SHEET)
            sheet_import.mark_duplicates(rows)
            sheet_import.insert_rows(rows, profile_id=profile.id, sheet=SHEET)

            again = sheet_import.read_rows(_csv(tmp_path, *lines), SHEET)
            sheet_import.mark_duplicates(again)

            assert all(r["status"] == sheet_import.STATUS_DUPLICATE for r in again)
            assert Property.query.count() == 2
