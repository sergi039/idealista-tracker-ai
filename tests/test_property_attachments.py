"""Attached documents and photos: what is stored, and what is refused (#430).

The worked example is the ficha catastral the agency sent by WhatsApp — evidence
that is not a measurement and cannot be recomputed. Three groups of test carry
the weight:

* **what the bytes are** decides everything, and the filename decides nothing.
  An SVG named `photo.jpg` is refused, an HTML file named `plan.pdf` is refused,
  and a filename full of `../` is stored as *metadata* while the path on disk
  stays a hash. This is the part that would be quiet if it broke: a stored
  polyglot is not visible until somebody opens it;
* **the write order** — bytes fsynced before the row is committed — because the
  two systems share no transaction and only one of the two failure directions
  is recoverable;
* **the sweeper's three refusals**, each of which is a way to delete something
  that is not garbage.
"""

import io
import os
from datetime import datetime, timedelta

import pytest

from app import create_app, db
from models import Property, PropertyActivity, PropertyAttachment, SearchProfile
from services import attachments as attachments_service
from services import owner_review
from tests import setup_test_environment

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"x" * 2000
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + b"\x00" * 500
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = (
    b"<!DOCTYPE html><html><body><script>alert(document.cookie)</script></body></html>"
)
ZIP = b"PK\x03\x04" + b"\x00" * 200


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    from config import Config

    directory = tmp_path / "attachments"
    monkeypatch.setattr(Config, "ATTACHMENTS_DIR", str(directory), raising=False)
    return directory


@pytest.fixture
def app(store_dir):
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
def prop(app):
    profile = SearchProfile(name="Asturias", is_active=True, is_default=True)
    db.session.add(profile)
    db.session.commit()
    row = Property(source_email_id="bayas", title="Bayas", search_profile_id=profile.id)
    db.session.add(row)
    db.session.commit()
    return row


def _upload(client, prop, data, filename, kind="note", **extra):
    payload = {"kind": kind, "body": "an entry", **extra}
    payload["attachment"] = (io.BytesIO(data), filename)
    return client.post(
        f"/properties/{prop.id}/activity",
        data=payload,
        content_type="multipart/form-data",
    )


class TestWhatTheBytesAre:
    def test_a_pdf_is_stored_under_its_hash(self, app, client, prop, store_dir):
        _upload(client, prop, PDF, "ficha catastral.pdf")

        record = PropertyAttachment.query.one()
        assert record.content_type == "application/pdf"
        assert record.kind == "document"
        assert record.size_bytes == len(PDF)
        # The name on disk is the hash; the name from the browser is metadata.
        assert record.storage_path.endswith(f"{record.content_sha256}.pdf")
        assert record.original_filename == "ficha catastral.pdf"
        assert os.path.exists(os.path.join(str(store_dir), record.storage_path))

    def test_an_svg_named_as_a_photo_is_refused(self, app, client, prop):
        """The stored-XSS case, and the reason extensions decide nothing.

        SVG is XML and can carry `<script>`. It is not on the allowlist at all,
        so the question is never "is this SVG safe to serve" -- it cannot be
        stored.
        """
        _upload(client, prop, SVG, "photo.jpg")
        assert PropertyAttachment.query.count() == 0

    def test_html_named_as_a_document_is_refused(self, app, client, prop):
        _upload(client, prop, HTML, "plan.pdf")
        assert PropertyAttachment.query.count() == 0

    def test_something_nobody_listed_is_refused(self, app, client, prop):
        _upload(client, prop, ZIP, "photos.png")
        assert PropertyAttachment.query.count() == 0

    def test_an_empty_file_is_refused(self, app, client, prop):
        _upload(client, prop, b"", "nothing.pdf")
        assert PropertyAttachment.query.count() == 0

    def test_a_traversal_filename_cannot_reach_out_of_the_root(
        self, app, client, prop, store_dir
    ):
        """The name is kept and the path is not built from it."""
        _upload(client, prop, PDF, "../../../../etc/passwd")

        record = PropertyAttachment.query.one()
        assert record.original_filename == "../../../../etc/passwd"
        assert ".." not in record.storage_path
        resolved = os.path.realpath(os.path.join(str(store_dir), record.storage_path))
        assert resolved.startswith(os.path.realpath(str(store_dir)))

    def test_the_entry_survives_a_refused_file(self, app, client, prop):
        """What was typed is not lost because the file was wrong."""
        _upload(client, prop, SVG, "photo.jpg")
        assert len(owner_review.timeline(prop)) == 1
        assert PropertyAttachment.query.count() == 0

    def test_a_file_over_the_cap_is_refused(self, app, client, prop, monkeypatch):
        monkeypatch.setattr(attachments_service, "MAX_FILE_BYTES", 1024)
        _upload(client, prop, PDF, "big.pdf")
        assert PropertyAttachment.query.count() == 0


class TestTheWriteOrder:
    def test_the_bytes_are_on_disk_before_the_row_exists(self, app, prop, store_dir):
        """An orphan file is inert; an orphan row is a 404 nobody can explain.

        The row is inserted by `attach`, after `store` returns -- so a failure
        to insert must leave the file rather than the other way round.
        """
        seen = {}

        real_store = attachments_service.store

        def watched(stream, **kwargs):
            result = real_store(stream, **kwargs)
            seen["rows_at_store_time"] = PropertyAttachment.query.count()
            seen["file_exists"] = os.path.exists(
                os.path.join(str(store_dir), result["storage_path"])
            )
            return result

        attachments_service.store = watched
        try:
            upload = type(
                "Upload", (), {"stream": io.BytesIO(PDF), "filename": "a.pdf"}
            )()
            attachments_service.attach(prop, upload)
        finally:
            attachments_service.store = real_store

        assert seen["file_exists"] is True
        assert seen["rows_at_store_time"] == 0
        assert PropertyAttachment.query.count() == 1

    def test_the_same_file_twice_is_one_file_and_two_rows(
        self, app, client, prop, store_dir
    ):
        """Dedup is on disk; a row is a link, and links are cheap.

        There is deliberately no unique constraint on (property, hash): the
        same document may be attached to two exchanges, and after a soft delete
        such a constraint would refuse the re-upload of the file it refers to.
        """
        _upload(client, prop, PDF, "first.pdf")
        _upload(client, prop, PDF, "again.pdf")

        rows = PropertyAttachment.query.all()
        assert len(rows) == 2
        assert rows[0].content_sha256 == rows[1].content_sha256
        assert rows[0].storage_path == rows[1].storage_path

        on_disk = []
        for dirpath, _, filenames in os.walk(str(store_dir)):
            on_disk.extend(
                name for name in filenames if not name.startswith(".incoming-")
            )
        assert len(on_disk) == 1


class TestServingIt:
    def test_a_pdf_is_a_download_with_its_own_type(self, app, client, prop):
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()

        response = client.get(f"/properties/{prop.id}/attachments/{record.id}")
        assert response.status_code == 200
        # The type comes from the STORED sniffed value. Left to Werkzeug it
        # would be guessed from `download_name`, which is the client's own
        # filename.
        assert response.headers["Content-Type"].startswith("application/pdf")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Disposition"].startswith("attachment")

    def test_a_photo_may_be_drawn_in_place(self, app, client, prop):
        _upload(client, prop, PNG, "frontage.png")
        record = PropertyAttachment.query.one()

        response = client.get(f"/properties/{prop.id}/attachments/{record.id}")
        assert response.headers["Content-Type"].startswith("image/png")
        assert response.headers["Content-Disposition"].startswith("inline")
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_the_type_served_is_the_sniffed_one_not_the_filename(
        self, app, client, prop
    ):
        """A PDF uploaded as `photo.png` is still served as a PDF."""
        _upload(client, prop, PDF, "photo.png")
        record = PropertyAttachment.query.one()

        response = client.get(f"/properties/{prop.id}/attachments/{record.id}")
        assert response.headers["Content-Type"].startswith("application/pdf")
        assert response.headers["Content-Disposition"].startswith("attachment")

    def test_another_propertys_attachment_is_not_reachable_here(
        self, app, client, prop
    ):
        other = Property(
            source_email_id="other",
            title="Other",
            search_profile_id=prop.search_profile_id,
        )
        db.session.add(other)
        db.session.commit()
        _upload(client, other, PDF, "theirs.pdf")
        record = PropertyAttachment.query.one()

        response = client.get(f"/properties/{prop.id}/attachments/{record.id}")
        assert response.status_code == 404

    def test_a_row_whose_bytes_are_gone_is_loud(self, app, client, prop, store_dir):
        """Should be impossible; must not read as "no such attachment"."""
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        os.unlink(os.path.join(str(store_dir), record.storage_path))

        response = client.get(f"/properties/{prop.id}/attachments/{record.id}")
        assert response.status_code == 410

    def test_a_removed_attachment_is_not_served(self, app, client, prop):
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        client.post(f"/properties/{prop.id}/attachments/{record.id}/delete")

        response = client.get(f"/properties/{prop.id}/attachments/{record.id}")
        assert response.status_code == 404


class TestDeletingIsSoft:
    def test_the_row_is_marked_and_the_bytes_stay(self, app, client, prop, store_dir):
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        path = os.path.join(str(store_dir), record.storage_path)

        client.post(f"/properties/{prop.id}/attachments/{record.id}/delete")

        db.session.expire_all()
        assert db.session.get(PropertyAttachment, record.id).deleted_at is not None
        # The sweeper is the only thing that unlinks bytes, and only after it
        # has checked that no live row still references the hash.
        assert os.path.exists(path)

    def test_it_leaves_the_page(self, app, client, prop):
        _upload(client, prop, PDF, "ficha catastral.pdf")
        record = PropertyAttachment.query.one()
        client.post(f"/properties/{prop.id}/attachments/{record.id}/delete")

        body = client.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert "ficha catastral.pdf" not in body


class TestTheSweeper:
    def _survey(self, store_dir, **kwargs):
        from utils import sweep_attachments

        return sweep_attachments.survey(str(store_dir), **kwargs)

    def test_a_referenced_file_is_kept(self, app, client, prop, store_dir):
        _upload(client, prop, PDF, "ficha.pdf")
        result = self._survey(store_dir)
        assert len(result["kept"]) == 1
        assert result["collectable"] == []

    def test_an_unreferenced_file_is_too_young_to_collect(
        self, app, client, prop, store_dir
    ):
        """The window between fsync and commit, which every upload passes through.

        A sweep inside it would delete the bytes of an upload that is about to
        reference them -- so age dominates the reference check, not the other
        way round.
        """
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        db.session.delete(record)
        db.session.commit()

        result = self._survey(store_dir)
        assert len(result["too_young"]) == 1
        assert result["collectable"] == []

    def test_an_old_unreferenced_file_is_collectable(
        self, app, client, prop, store_dir
    ):
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        path = os.path.join(str(store_dir), record.storage_path)
        db.session.delete(record)
        db.session.commit()

        old = datetime.utcnow() - timedelta(hours=72)
        os.utime(path, (old.timestamp(), old.timestamp()))

        result = self._survey(store_dir)
        assert len(result["collectable"]) == 1

    def test_a_soft_deleted_row_does_not_protect_its_bytes_forever(
        self, app, client, prop, store_dir
    ):
        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        path = os.path.join(str(store_dir), record.storage_path)
        client.post(f"/properties/{prop.id}/attachments/{record.id}/delete")

        old = datetime.utcnow() - timedelta(hours=72)
        os.utime(path, (old.timestamp(), old.timestamp()))

        result = self._survey(store_dir)
        assert len(result["collectable"]) == 1

    def test_a_file_two_rows_share_is_kept_when_one_of_them_goes(
        self, app, client, prop, store_dir
    ):
        """Content-addressed storage means a file has no single owner."""
        _upload(client, prop, PDF, "first.pdf")
        _upload(client, prop, PDF, "again.pdf")
        first = PropertyAttachment.query.order_by(PropertyAttachment.id).first()
        client.post(f"/properties/{prop.id}/attachments/{first.id}/delete")

        path = os.path.join(str(store_dir), first.storage_path)
        old = datetime.utcnow() - timedelta(hours=72)
        os.utime(path, (old.timestamp(), old.timestamp()))

        result = self._survey(store_dir)
        assert result["collectable"] == []
        assert len(result["kept"]) == 1

    def test_an_upload_in_flight_is_not_looked_at(self, app, store_dir):
        """`tmp/` holds partial uploads, which are not leftovers."""
        from utils import sweep_attachments

        incoming = os.path.join(str(store_dir), "tmp")
        os.makedirs(incoming, exist_ok=True)
        partial = os.path.join(incoming, ".incoming-abc123")
        with open(partial, "wb") as handle:
            handle.write(b"half a file")
        old = datetime.utcnow() - timedelta(hours=72)
        os.utime(partial, (old.timestamp(), old.timestamp()))

        result = sweep_attachments.survey(str(store_dir))
        assert result["collectable"] == []
        assert os.path.exists(partial)

    def test_sweeping_moves_rather_than_deletes(self, app, client, prop, store_dir):
        from utils import sweep_attachments

        _upload(client, prop, PDF, "ficha.pdf")
        record = PropertyAttachment.query.one()
        path = os.path.join(str(store_dir), record.storage_path)
        db.session.delete(record)
        db.session.commit()
        old = datetime.utcnow() - timedelta(hours=72)
        os.utime(path, (old.timestamp(), old.timestamp()))

        result = sweep_attachments.survey(str(store_dir))
        destination = sweep_attachments.sweep(str(store_dir), result["collectable"])

        assert not os.path.exists(path)
        # What it deletes is unrecoverable, so it does not delete: emptying the
        # quarantine is a separate decision with the file list in front of you.
        moved = os.listdir(destination)
        assert len(moved) == 1


class TestTheAttachmentCannotBelongToAnotherPropertysEntry:
    def test_the_model_declares_the_composite_key(self, app):
        """The declaration, here; the refusal itself, on PostgreSQL.

        SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`
        is set per connection, and this suite does not set it -- so a test
        here that expected the INSERT to fail would pass for the wrong reason
        or fail for one. What is checked here is that the pair is declared
        together, which is what makes the database able to refuse; the insert
        that must actually be refused is attempted against a real server in
        `tests/test_postgres_migrations.py`.
        """
        constraint = next(
            item
            for item in PropertyAttachment.__table__.constraints
            if getattr(item, "name", "") == "fk_property_attachment_activity"
        )
        columns = [column.name for column in constraint.columns]
        assert sorted(columns) == ["activity_id", "property_id"]
        referred = sorted(element.target_fullname for element in constraint.elements)
        assert referred == [
            "property_activity.id",
            "property_activity.property_id",
        ]

    def test_an_attachment_with_no_entry_is_fine(self, app, prop):
        record = PropertyAttachment(
            property_id=prop.id,
            activity_id=None,
            content_sha256="b" * 64,
            storage_path="bb/bb/" + "b" * 64 + ".pdf",
            content_type="application/pdf",
            size_bytes=10,
            kind="document",
        )
        db.session.add(record)
        db.session.commit()
        assert record.id is not None


class TestCsrf:
    @pytest.fixture
    def app(self, store_dir):
        setup_test_environment()
        application = create_app()
        application.config["TESTING"] = True
        with application.app_context():
            db.create_all()
            yield application
            db.session.remove()
            db.drop_all()

    def test_removing_without_a_token_is_refused(self, app):
        profile = SearchProfile(name="A", is_active=True, is_default=True)
        db.session.add(profile)
        db.session.commit()
        row = Property(source_email_id="x", title="P", search_profile_id=profile.id)
        db.session.add(row)
        db.session.commit()
        record = PropertyAttachment(
            property_id=row.id,
            content_sha256="c" * 64,
            storage_path="cc/cc/" + "c" * 64 + ".pdf",
            content_type="application/pdf",
            size_bytes=10,
            kind="document",
        )
        db.session.add(record)
        db.session.commit()

        response = app.test_client().post(
            f"/properties/{row.id}/attachments/{record.id}/delete"
        )
        assert response.status_code == 400
        db.session.expire_all()
        assert db.session.get(PropertyAttachment, record.id).deleted_at is None


def test_the_request_body_has_a_ceiling(app):
    """Werkzeug refuses past this before the bytes are buffered."""
    assert app.config["MAX_CONTENT_LENGTH"] == 32 * 1024 * 1024


def test_a_verdict_entry_can_carry_no_file(app, client, prop):
    """The upload rides on the add form, which only writes notes and contacts."""
    owner_review.set_review(prop, decision="rejected", reason="irregular parcel")
    entry = PropertyActivity.query.filter_by(kind="verdict").one()
    assert attachments_service.for_property(prop).get(entry.id) is None
