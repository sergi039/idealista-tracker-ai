"""Importing fotocasa listings by link: read, look, then write.

The two halves are separate because **this application cannot delete a
property**. There is no delete route and no `db.session.delete` on `Property`
anywhere in the tree, so a row created from a misread page cannot be taken
back: it stays in the table, in the `/municipalities` medians and in the
comparable pool of its subscription. The preview is the only undo, and these
tests pin that the read half writes nothing.

The rest is provenance. `listing_status_source` stays NULL, because the row
was neither checked nor ingested -- writing `manual` there is the defect this
same change repairs 324 rows of (STATUS-002, issue #265), and re-introducing
it at a new entry point would be the same mistake with a nicer form in front
of it.
"""

import pathlib

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import fotocasa_import
from services.fotocasa_source import parse_listing
from tests import setup_test_environment

URL = "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d"
FIXTURE = pathlib.Path(__file__).parent / "data" / "fotocasa_listing_190280914.html"


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        profile = SearchProfile(
            name="Plots 0-50 km",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        app.config["TEST_PROFILE_ID"] = profile.id
        yield app
        db.drop_all()


@pytest.fixture
def previewed():
    """The preview row a real page produces, without touching the network."""
    listing = parse_listing(FIXTURE.read_text(encoding="utf-8"), URL)
    return fotocasa_import.preview_row(listing)


class TestPreview:
    def test_a_read_page_becomes_a_new_row_of_the_preview(self, previewed):
        assert previewed["status"] == fotocasa_import.STATUS_NEW
        assert previewed["listing_id"] == 190280914
        assert previewed["price"] == 68000.0
        assert previewed["municipality"] == "Avilés"

    def test_a_refusal_is_a_line_saying_why_not_a_missing_line(self):
        listing = parse_listing("", URL)
        row = fotocasa_import.preview_row(listing)

        assert row["status"] == fotocasa_import.STATUS_REFUSED
        assert row["reason"] == "no_payload"

    def test_reading_writes_nothing(self, app, monkeypatch):
        """The whole point of the two-step flow."""
        monkeypatch.setattr(
            fotocasa_import,
            "fetch_listing",
            lambda url, session=None: parse_listing(
                FIXTURE.read_text(encoding="utf-8"), url
            ),
        )
        with app.app_context():
            before = Property.query.count()
            rows = fotocasa_import.read_urls([URL])

            assert len(rows) == 1
            assert rows[0]["status"] == fotocasa_import.STATUS_NEW
            assert Property.query.count() == before

    def test_a_link_already_in_the_table_is_reported_not_fetched(
        self, app, monkeypatch
    ):
        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("fetched a listing that is already here")

        monkeypatch.setattr(fotocasa_import, "fetch_listing", explode)

        with app.app_context():
            existing = Property(
                source_email_id="manual:plots-fotocasa-2026-08-15:190280914",
                title="Already imported by the old script",
                url=URL,
            )
            db.session.add(existing)
            db.session.commit()

            rows = fotocasa_import.read_urls([URL])

            assert rows[0]["status"] == fotocasa_import.STATUS_DUPLICATE
            assert rows[0]["existing_id"] == existing.id

    def test_a_non_listing_link_is_rejected_without_a_request(self, app, monkeypatch):
        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("fetched a search results page")

        monkeypatch.setattr(fotocasa_import, "fetch_listing", explode)

        with app.app_context():
            rows = fotocasa_import.read_urls(
                ["https://www.fotocasa.es/en/buy/lands/asturias-province/all-zones/l"]
            )

            assert rows[0]["status"] == fotocasa_import.STATUS_REJECTED


class TestInsert:
    def test_the_row_carries_what_the_page_said(self, app, previewed):
        with app.app_context():
            profile_id = app.config["TEST_PROFILE_ID"]
            outcome = fotocasa_import.insert_rows([previewed], profile_id=profile_id)

            assert len(outcome["created"]) == 1
            prop = db.session.get(Property, outcome["created"][0]["id"])
            assert prop.price == 68000
            assert prop.area == 1945
            assert prop.area_type == "plot"
            assert prop.municipality == "Avilés"
            assert prop.search_profile_id == profile_id
            assert prop.url == URL

    def test_the_status_source_is_null_not_manual_and_not_ingest(self, app, previewed):
        """STATUS-002, at the new entry point. Nobody checked this listing."""
        with app.app_context():
            outcome = fotocasa_import.insert_rows(
                [previewed], profile_id=app.config["TEST_PROFILE_ID"]
            )
            prop = db.session.get(Property, outcome["created"][0]["id"])

            assert prop.listing_status_source is None

            from services.listing_verification import read_verdict

            verdict = read_verdict(prop)
            assert verdict["state"] == "unchecked"
            assert verdict["verified"] is False

    def test_the_unchecked_note_names_fotocasa_not_idealista(self, app, previewed):
        with app.app_context():
            outcome = fotocasa_import.insert_rows(
                [previewed], profile_id=app.config["TEST_PROFILE_ID"]
            )
            prop = db.session.get(Property, outcome["created"][0]["id"])

            from services.listing_verification import read_verdict

            note = read_verdict(prop)["note"]
            assert "Fotocasa" in note
            assert "Idealista" not in note

    def test_the_coordinate_is_stored_approximate(self, app, previewed):
        """`precise` would unlock a paid travel run off a point the portal
        itself declares inexact."""
        with app.app_context():
            outcome = fotocasa_import.insert_rows(
                [previewed], profile_id=app.config["TEST_PROFILE_ID"]
            )
            prop = db.session.get(Property, outcome["created"][0]["id"])

            assert float(prop.location_lat) == pytest.approx(43.570805)
            assert prop.location_accuracy == "approximate"

            from services.coordinate_quality import is_precise

            assert is_precise(prop.location_accuracy) is False

    def test_the_portals_own_accuracy_flags_are_kept(self, app, previewed):
        with app.app_context():
            outcome = fotocasa_import.insert_rows(
                [previewed], profile_id=app.config["TEST_PROFILE_ID"]
            )
            prop = db.session.get(Property, outcome["created"][0]["id"])

            provenance = prop.enrichment["import"]
            assert provenance["source"] == "fotocasa"
            assert provenance["listing_id"] == 190280914
            assert provenance["portal_accuracy"]["is_exact"] is False

    def test_importing_the_same_listing_twice_creates_one_row(self, app, previewed):
        with app.app_context():
            profile_id = app.config["TEST_PROFILE_ID"]
            fotocasa_import.insert_rows([previewed], profile_id=profile_id)
            second = fotocasa_import.insert_rows([previewed], profile_id=profile_id)

            assert second["created"] == []
            assert len(second["skipped"]) == 1
            assert Property.query.filter_by(url=URL).count() == 1

    def test_a_collision_costs_its_own_row_and_not_the_batch(
        self, app, previewed, monkeypatch
    ):
        """The race the sequential test could not see.

        `_existing_by_listing_id` is a plain SELECT, so under READ COMMITTED it
        cannot see a row another transaction has inserted and not yet
        committed. Two confirms overlapping on one listing therefore both pass
        the check -- a double click is enough -- and the loser's flush hits the
        unique constraint on `source_email_id`. Before the savepoint that
        exception left the loop before the single `commit()` was reached, so
        every other row in the batch, all of them valid, was discarded while
        the page said "Import failed".

        The check is made blind for the colliding id and left alone for the
        rest, which is exactly the losing request's view: its SELECT answered
        "not here" and the database then said otherwise. Simulating it with two
        real threads would need two connections and a scheduler; making the
        one SELECT lie reproduces the same state and cannot pass by accident --
        remove the savepoint and this test fails.
        """
        second = dict(previewed)
        second["listing_id"] = 190210058
        second["url"] = "https://www.fotocasa.es/en/buy/land/gozon/x/190210058/d"
        second["title"] = "A second, unrelated listing"

        real_lookup = fotocasa_import._existing_by_listing_id
        blinded = {"count": 0}

        def blind_to_the_other_tabs_row(listing_id):
            # Blind on the *check*, honest afterwards: the except handler asks
            # again, and by then the other transaction has committed.
            if listing_id == 190280914 and blinded["count"] == 0:
                blinded["count"] += 1
                return None
            return real_lookup(listing_id)

        monkeypatch.setattr(
            fotocasa_import, "_existing_by_listing_id", blind_to_the_other_tabs_row
        )

        with app.app_context():
            db.session.add(
                Property(
                    source_email_id=fotocasa_import.source_email_id_for(190280914),
                    title="Committed by the other tab",
                    url=URL,
                )
            )
            db.session.commit()

            outcome = fotocasa_import.insert_rows(
                [previewed, second], profile_id=app.config["TEST_PROFILE_ID"]
            )

            # The check really was blinded, so the flush is what refused it.
            assert blinded["count"] == 1

            assert len(outcome["created"]) == 1
            assert outcome["created"][0]["title"] == "A second, unrelated listing"
            assert len(outcome["skipped"]) == 1
            assert outcome["skipped"][0]["existing_id"] is not None

            # The unrelated listing survived: the batch was not thrown away.
            assert (
                Property.query.filter_by(
                    source_email_id=fotocasa_import.source_email_id_for(190210058)
                ).count()
                == 1
            )
            assert Property.query.count() == 2

    def test_a_collision_the_check_does_see_is_still_skipped(self, app, previewed):
        """The ordinary path stays ordinary: no savepoint needed to skip it."""
        with app.app_context():
            profile_id = app.config["TEST_PROFILE_ID"]
            fotocasa_import.insert_rows([previewed], profile_id=profile_id)
            second = fotocasa_import.insert_rows([previewed], profile_id=profile_id)

            assert second["created"] == []
            assert second["skipped"][0]["existing_id"] is not None

    def test_only_new_rows_are_written(self, app, previewed):
        refused = fotocasa_import.preview_row(parse_listing("", URL))
        with app.app_context():
            outcome = fotocasa_import.insert_rows(
                [previewed, refused], profile_id=app.config["TEST_PROFILE_ID"]
            )

            assert len(outcome["created"]) == 1
            assert Property.query.count() == 1


class TestRoutes:
    def test_the_first_step_asks_only_for_links(self, app):
        """The destination is not asked here: reading does not use one, and
        putting the control on both steps is the duplication /properties is
        explicitly kept free of."""
        with app.test_client() as client:
            body = client.get("/properties/import").get_data(as_text=True)

            assert "Import listings by link" in body
            assert 'name="urls"' in body
            assert 'name="profile_id"' not in body

    def test_the_destination_is_asked_where_it_is_used(self, app, previewed):
        """It appears with the preview, beside the button that writes."""
        from services.background_jobs import run_job_sync

        with app.app_context():
            job_id = run_job_sync(
                lambda: {"rows": [previewed]}, job_type="fotocasa_import_read"
            )

        with app.test_client() as client:
            body = client.get(f"/properties/import?job={job_id}").get_data(as_text=True)

            assert 'name="profile_id"' in body
            assert "Plots 0-50 km" in body
            assert "Llaranes" in body

    def test_pasting_nothing_writes_nothing(self, app):
        with app.test_client() as client:
            response = client.post(
                "/properties/import", data={"urls": "   "}, follow_redirects=True
            )

            assert response.status_code == 200
            assert Property.query.count() == 0

    def test_confirming_without_a_destination_writes_nothing(self, app, previewed):
        from services.background_jobs import run_job_sync

        with app.app_context():
            job_id = run_job_sync(
                lambda: {"rows": [previewed]}, job_type="fotocasa_import_read"
            )

        with app.test_client() as client:
            response = client.post(
                "/properties/import/confirm",
                data={"job_id": job_id},
                follow_redirects=True,
            )

            assert response.status_code == 200
            assert Property.query.count() == 0

    def test_confirming_an_unknown_preview_writes_nothing(self, app):
        """The preview is the only undo; a missing one must not become a write."""
        with app.test_client() as client:
            response = client.post(
                "/properties/import/confirm",
                data={"job_id": "does-not-exist", "profile_id": 1},
                follow_redirects=True,
            )

            assert response.status_code == 200
            assert Property.query.count() == 0

    def test_confirming_creates_the_rows(self, app, previewed):
        from services.background_jobs import run_job_sync

        with app.app_context():
            job_id = run_job_sync(
                lambda: {"rows": [previewed]}, job_type="fotocasa_import_read"
            )
            profile_id = app.config["TEST_PROFILE_ID"]

        with app.test_client() as client:
            response = client.post(
                "/properties/import/confirm",
                data={"job_id": job_id, "profile_id": profile_id},
                follow_redirects=False,
            )

            assert response.status_code == 302
            assert Property.query.count() == 1
            prop = Property.query.one()
            assert prop.municipality == "Avilés"
            assert prop.search_profile_id == profile_id

    def test_the_listing_page_links_to_the_import(self, app):
        with app.test_client() as client:
            body = client.get("/properties").get_data(as_text=True)

            assert "/properties/import" in body
