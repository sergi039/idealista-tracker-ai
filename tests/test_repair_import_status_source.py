"""Taking back a claim the importer never had the evidence for (STATUS-002).

The repair is narrow on purpose, and the narrowness is the only thing standing
between it and a hand-set verdict the owner really made: `manual` is what the
status button writes too, and only the importer's rows also carry a
`source_email_id` beginning `manual:`.

Production was repaired on 2026-08-17 with exactly that condition -- the
snapshot left behind holds 324 rows, all carrying the prefix, none carrying a
`listing_last_checked`. So the fixture below is not the shape production has
today; it is the shape the rule has to survive, and it deliberately includes the
row that must not be touched, which production happened not to have.
"""

import json

import pytest

from app import create_app, db
from models import Property
from tests import setup_test_environment
from utils import repair_import_status_source as repair_module


@pytest.fixture
def app(tmp_path):
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                # The importer's rows: the word `manual` in both columns.
                Property(
                    source_email_id="manual:plots-fotocasa-2026-08-15:190280914",
                    title="Imported plot",
                    url="https://www.fotocasa.es/es/comprar/terreno/a/b/190280914/d",
                    listing_status="active",
                    listing_status_source="manual",
                ),
                Property(
                    source_email_id="manual:solar-2026-08-15:112283304",
                    title="Imported solar",
                    url="https://www.idealista.com/es/inmueble/112283304/",
                    listing_status="active",
                    listing_status_source="manual",
                ),
                # The owner's own verdict, set with the button on the property
                # page. Same word, different meaning, and it stays.
                Property(
                    source_email_id="alert-email-99",
                    title="Checked by hand",
                    url="https://www.idealista.com/es/inmueble/99/",
                    listing_status="removed",
                    listing_status_source="manual",
                ),
                # An ordinary ingested row, untouched by either.
                Property(
                    source_email_id="alert-email-1",
                    title="Ingested",
                    url="https://www.idealista.com/es/inmueble/1/",
                    listing_status="active",
                    listing_status_source="ingest",
                ),
            ]
        )
        db.session.commit()
        yield app
        db.drop_all()


def _sources(app):
    return {
        row.source_email_id: row.listing_status_source for row in Property.query.all()
    }


class TestReporting:
    def test_it_reports_and_writes_nothing_without_apply(self, app):
        with app.app_context():
            before = _sources(app)

            summary = repair_module.repair(apply=False)

            assert summary["to_repair"] == 2
            assert summary["protected_hand_set"] == 1
            assert summary["applied"] is False
            assert summary["snapshot"] is None
            db.session.expire_all()
            assert _sources(app) == before

    def test_it_reports_the_corroboration_the_narrowing_rests_on(self, app):
        """None of the importer's rows carry a check, which is the evidence
        that its `manual` was never a reading."""
        with app.app_context():
            summary = repair_module.repair(apply=False)

            assert summary["with_a_recorded_check"] == 0


class TestApplying:
    def test_the_importers_claim_becomes_null(self, app):
        with app.app_context():
            repair_module.repair(apply=True, backup=False)
            db.session.expire_all()

            sources = _sources(app)
            assert sources["manual:plots-fotocasa-2026-08-15:190280914"] is None
            assert sources["manual:solar-2026-08-15:112283304"] is None

    def test_the_hand_set_verdict_survives(self, app):
        """The eight rows in production this must not touch."""
        with app.app_context():
            repair_module.repair(apply=True, backup=False)
            db.session.expire_all()

            assert _sources(app)["alert-email-99"] == "manual"

    def test_an_ingested_row_is_untouched(self, app):
        with app.app_context():
            repair_module.repair(apply=True, backup=False)
            db.session.expire_all()

            assert _sources(app)["alert-email-1"] == "ingest"

    def test_the_repaired_rows_read_as_unchecked_afterwards(self, app):
        """The whole point: the coverage line stops counting them."""
        from services.listing_verification import read_verdict

        with app.app_context():
            repair_module.repair(apply=True, backup=False)
            db.session.expire_all()

            row = Property.query.filter_by(
                source_email_id="manual:plots-fotocasa-2026-08-15:190280914"
            ).one()
            verdict = read_verdict(row)

            assert verdict["state"] == "unchecked"
            assert verdict["verified"] is False

    def test_running_it_twice_is_a_no_op(self, app):
        with app.app_context():
            repair_module.repair(apply=True, backup=False)
            second = repair_module.repair(apply=True, backup=False)

            assert second["to_repair"] == 0


class TestSnapshot:
    def test_a_snapshot_is_written_before_the_write(self, app, tmp_path):
        path = str(tmp_path / "snapshot.json")
        with app.app_context():
            summary = repair_module.repair(apply=True, snapshot_path=path)

            assert summary["snapshot"] == path
            payload = json.loads((tmp_path / "snapshot.json").read_text())
            assert len(payload["rows"]) == 2
            assert all(
                row["listing_status_source"] == "manual" for row in payload["rows"]
            )

    def test_the_snapshot_restores_what_it_recorded(self, app, tmp_path):
        """A rollback that is never exercised is a rollback nobody has."""
        path = str(tmp_path / "snapshot.json")
        with app.app_context():
            repair_module.repair(apply=True, snapshot_path=path)
            db.session.expire_all()
            assert _sources(app)["manual:solar-2026-08-15:112283304"] is None

            outcome = repair_module.restore(path, apply=True)
            db.session.expire_all()

            assert outcome["restored"] == 2
            assert _sources(app)["manual:solar-2026-08-15:112283304"] == "manual"

    def test_restore_reports_without_apply(self, app, tmp_path):
        path = str(tmp_path / "snapshot.json")
        with app.app_context():
            repair_module.repair(apply=True, snapshot_path=path)
            db.session.expire_all()

            repair_module.restore(path, apply=False)
            db.session.expire_all()

            assert _sources(app)["manual:solar-2026-08-15:112283304"] is None

    def test_a_snapshot_is_never_left_half_written(self, app, tmp_path, monkeypatch):
        """It is the only way back, so a truncated one is worse than none."""
        path = str(tmp_path / "snapshot.json")
        real_dump = json.dump

        def explode(*args, **kwargs):
            real_dump(*args, **kwargs)
            raise OSError("disk full")

        monkeypatch.setattr(json, "dump", explode)

        with app.app_context():
            with pytest.raises(OSError):
                repair_module.repair(apply=True, snapshot_path=path)

            assert not (tmp_path / "snapshot.json").exists()
            assert list(tmp_path.glob(".*.tmp")) == []
            # And nothing was written to the database either.
            db.session.expire_all()
            assert _sources(app)["manual:solar-2026-08-15:112283304"] == "manual"
