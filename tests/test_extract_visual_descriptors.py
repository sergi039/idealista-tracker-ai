"""The explicit visual CLI discards an extraction superseded by a row update."""

import base64
from contextlib import contextmanager
import hashlib

import pytest

import app as app_module
from app import create_app, db
from models import Property, SearchProfile
from services import taste_descriptors, visual_input
from tests import setup_test_environment
from utils import extract_visual_descriptors


JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x01\x00\x01visual-cli"


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _image():
    return {
        "source_kind": "dossier_photo",
        "source_id": "property:1:dossier:0",
        "content_type": "image/jpeg",
        "content_sha256": hashlib.sha256(JPEG).hexdigest(),
        "content_base64": base64.b64encode(JPEG).decode("ascii"),
    }


def _without_real_marker(monkeypatch, seen=None):
    @contextmanager
    def marker(name, **kwargs):
        if seen is not None:
            seen.append((name, kwargs))
        yield

    monkeypatch.setattr(extract_visual_descriptors, "inflight", marker)


def test_apply_discards_a_visual_result_after_the_property_changes(
    app, monkeypatch, capsys
):
    _without_real_marker(monkeypatch)
    profile = SearchProfile(name="Galicia", is_active=True)
    db.session.add(profile)
    db.session.commit()
    prop = Property(
        source_email_id="visual-cli:1",
        title="Before extraction",
        search_profile_id=profile.id,
        area=120,
    )
    db.session.add(prop)
    db.session.commit()
    image = _image()
    envelope = visual_input.build_visual_input([image])

    monkeypatch.setattr(
        visual_input,
        "download_portal_photo_inputs",
        lambda *_args, **_kwargs: [image],
    )
    monkeypatch.setattr(app_module, "create_app", lambda: app)

    def extract_then_change(*_args, **_kwargs):
        # This mimics another writer committing while the model call is in flight.
        db.session.execute(
            db.text("UPDATE properties SET title = :title WHERE id = :id"),
            {"title": "Changed during extraction", "id": prop.id},
        )
        db.session.commit()
        return {**envelope, "visual_observations": []}

    monkeypatch.setattr(
        visual_input, "extract_visual_observations", extract_then_change
    )

    assert (
        extract_visual_descriptors.run(
            [
                "--ids",
                str(prop.id),
                "--apply",
                "--max-rows",
                "1",
                "--max-images",
                "1",
                "--max-calls",
                "1",
            ]
        )
        == 0
    )

    db.session.refresh(prop)
    assert prop.taste is None
    assert prop.title == "Changed during extraction"
    assert (
        "discarded (property inputs changed during extraction)"
        in capsys.readouterr().out
    )


def test_apply_skips_an_unchanged_valid_visual_descriptor_without_another_call(
    app, monkeypatch, capsys
):
    markers = []
    _without_real_marker(monkeypatch, markers)
    profile = SearchProfile(name="Galicia", is_active=True)
    db.session.add(profile)
    db.session.commit()
    prop = Property(
        source_email_id="visual-cli:unchanged",
        title="Already extracted",
        search_profile_id=profile.id,
        area=120,
    )
    db.session.add(prop)
    db.session.commit()
    image = _image()
    envelope = visual_input.build_visual_input([image])
    prop.taste = {
        "visual_descriptor": {
            **envelope,
            "property_fingerprint": taste_descriptors.input_fingerprint(prop),
        }
    }
    db.session.commit()

    monkeypatch.setattr(
        visual_input,
        "download_portal_photo_inputs",
        lambda *_args, **_kwargs: [image],
    )
    monkeypatch.setattr(app_module, "create_app", lambda: app)
    monkeypatch.setattr(
        visual_input,
        "extract_visual_observations",
        lambda *_args, **_kwargs: pytest.fail("unchanged input repaid the model call"),
    )

    assert (
        extract_visual_descriptors.run(
            [
                "--ids",
                str(prop.id),
                "--apply",
                "--max-rows",
                "1",
                "--max-images",
                "1",
                "--max-calls",
                "1",
            ]
        )
        == 0
    )

    assert markers == [
        (
            "extract_visual_descriptors",
            {
                "resumable": True,
                "argv": [
                    "--ids",
                    str(prop.id),
                    "--apply",
                    "--max-rows",
                    "1",
                    "--max-images",
                    "1",
                    "--max-calls",
                    "1",
                ],
            },
        )
    ]
    assert "skipped (visual descriptor inputs unchanged)" in capsys.readouterr().out
