import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from services import subscription_transport, visual_input


JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x01\x00\x01" + b"visual-input"


def _image(data=JPEG):
    return {
        "source_kind": "portal_photo",
        "source_id": "property:1375:photo:0",
        "content_type": "image/jpeg",
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


class _Response:
    def __init__(self, *, status=200, chunks=(JPEG,), headers=None):
        self.status_code = status
        self._chunks = chunks
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == visual_input._CHUNK_BYTES
        return iter(self._chunks)

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response

    def close(self):
        self.closed = True


def test_dossier_sources_require_the_property_specific_host():
    response = _Response(
        chunks=(b'<img src="/img/front.jpg"><img src="https://evil.test/x.jpg">',)
    )
    prop = SimpleNamespace(
        id=969,
        enrichment={"dossier": {"url": "https://969.cervantes50.com/"}},
    )

    sources = visual_input.dossier_photo_sources(prop, session=_Session(response))

    assert sources == [
        {
            "source_kind": "dossier_photo",
            "source_id": "property:969:dossier:0",
            "url": "https://969.cervantes50.com/img/front.jpg",
        }
    ]


def test_dossier_source_rejects_a_host_that_does_not_match_its_property():
    prop = SimpleNamespace(
        id=969,
        enrichment={"dossier": {"url": "https://1282.cervantes50.com/"}},
    )
    assert visual_input.dossier_photo_sources(prop, session=_Session(_Response())) == []


def test_portal_photo_sources_reject_a_db_injected_host():
    prop = SimpleNamespace(
        id=1,
        enrichment={
            "import": {
                "photos": {
                    "published": 1,
                    "items": [{"url": "https://evil.test/photo.jpg"}],
                }
            }
        },
    )

    assert visual_input.portal_photo_sources(prop) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://media.yaencontre.com:8443/photo.jpg",
        "https://media.yaencontre.com:invalid/photo.jpg",
    ],
)
def test_portal_photo_sources_require_default_https_port(url):
    prop = SimpleNamespace(
        id=1,
        enrichment={"import": {"photos": {"published": 1, "items": [{"url": url}]}}},
    )

    assert visual_input.portal_photo_sources(prop) == []


def test_dossier_sources_require_default_https_port():
    prop = SimpleNamespace(
        id=969,
        enrichment={"dossier": {"url": "https://969.cervantes50.com:8443/"}},
    )

    assert visual_input.dossier_photo_sources(prop, session=_Session(_Response())) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user@969.cervantes50.com/",
        "https://user:password@969.cervantes50.com/",
    ],
)
def test_dossier_sources_refuse_url_credentials(url):
    prop = SimpleNamespace(id=969, enrichment={"dossier": {"url": url}})

    assert visual_input.dossier_photo_sources(prop, session=_Session(_Response())) == []


def test_download_photo_source_streams_with_no_redirect_and_hashes_bytes():
    response = _Response(headers={"Content-Length": str(len(JPEG))})
    session = _Session(response)

    image = visual_input.download_photo_source(
        {
            "source_kind": "portal_photo",
            "source_id": "property:1375:photo:0",
            "url": "https://media.yaencontre.com/photo.jpg",
        },
        session=session,
    )

    assert image == _image()
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["allow_redirects"] is False
    assert response.closed is True
    assert session.closed is False


def test_download_photo_source_closes_only_an_internally_created_session(monkeypatch):
    response = _Response(headers={"Content-Length": str(len(JPEG))})
    owned = _Session(response)
    monkeypatch.setattr(visual_input.requests, "Session", lambda: owned)

    visual_input.download_photo_source(
        {
            "source_kind": "portal_photo",
            "source_id": "property:1375:photo:0",
            "url": "https://media.yaencontre.com/photo.jpg",
        }
    )

    assert owned.closed is True


def test_download_photo_source_closes_an_owned_session_after_a_request_error(
    monkeypatch,
):
    class FailingSession:
        closed = False

        def get(self, *_args, **_kwargs):
            raise visual_input.requests.RequestException("offline")

        def close(self):
            self.closed = True

    owned = FailingSession()
    monkeypatch.setattr(visual_input.requests, "Session", lambda: owned)

    with pytest.raises(visual_input.VisualInputError, match="did not answer"):
        visual_input.download_photo_source(
            {
                "source_kind": "portal_photo",
                "source_id": "property:1375:photo:0",
                "url": "https://media.yaencontre.com/photo.jpg",
            }
        )

    assert owned.closed is True


def test_download_dossier_source_refuses_url_credentials_before_network():
    session = _Session(_Response())

    with pytest.raises(visual_input.VisualInputError, match="exact-host"):
        visual_input.download_photo_source(
            {
                "source_kind": "dossier_photo",
                "source_id": "property:969:dossier:0",
                "url": "https://user:password@969.cervantes50.com/front.jpg",
            },
            session=session,
        )

    assert session.calls == []


def test_download_photo_source_refuses_an_oversized_stream_before_it_grows():
    response = _Response(chunks=(b"x" * (visual_input.MAX_IMAGE_BYTES + 1),))
    with pytest.raises(visual_input.VisualInputError, match="byte limit"):
        visual_input.download_photo_source(
            {
                "source_kind": "portal_photo",
                "source_id": "property:1375:photo:0",
                "url": "https://media.yaencontre.com/photo.jpg",
            },
            session=_Session(response),
        )
    assert response.closed is True


def test_visual_input_envelope_fingerprints_validated_byte_identity():
    first = visual_input.build_visual_input([_image()])
    changed = visual_input.build_visual_input([_image(JPEG + b"changed")])

    assert first["schema_version"] == 1
    assert first["visual_observations"] == []
    assert len(first["input_fingerprint"]) == 64
    assert changed["input_fingerprint"] != first["input_fingerprint"]


def test_visual_input_envelope_refuses_tampered_hash():
    image = _image()
    image["content_sha256"] = "0" * 64

    with pytest.raises(visual_input.VisualInputError, match="hash"):
        visual_input.build_visual_input([image])


def test_attachment_input_reconstructs_the_hash_addressed_path(tmp_path, monkeypatch):
    digest = hashlib.sha256(JPEG).hexdigest()
    relative = visual_input.attachments.storage_path(digest, "jpg")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(JPEG)
    monkeypatch.setattr(
        visual_input.attachments, "attachments_dir", lambda: str(tmp_path)
    )
    record = SimpleNamespace(
        id=7,
        content_type="image/jpeg",
        content_sha256=digest,
        storage_path=relative,
    )

    image = visual_input.attachment_photo_input(record)

    assert image["source_kind"] == "attachment_photo"
    assert image["source_id"] == "attachment:7"
    assert visual_input.build_visual_input([image])["schema_version"] == 1


def test_attachment_input_refuses_a_path_outside_its_hash_address(
    tmp_path, monkeypatch
):
    digest = hashlib.sha256(JPEG).hexdigest()
    monkeypatch.setattr(
        visual_input.attachments, "attachments_dir", lambda: str(tmp_path)
    )
    record = SimpleNamespace(
        id=7,
        content_type="image/jpeg",
        content_sha256=digest,
        storage_path="../../outside.jpg",
    )

    with pytest.raises(visual_input.VisualInputError, match="path"):
        visual_input.attachment_photo_input(record)


def _observation(image, *, aspect_id="house_condition", status="claimed"):
    return {
        "aspect_id": aspect_id,
        "value": "weathered",
        "status": status,
        "evidence": {
            "source_kind": "photo",
            "source_id": image["source_id"],
            "image_sha256": image["content_sha256"],
            "image_index": 0,
        },
        "confidence": 0.7,
        "limitation": "Only the visible exterior is in frame.",
    }


def test_visual_observation_must_name_the_exact_hashed_image():
    image = _image()
    result = visual_input.validate_visual_observations(
        {"visual_observations": [_observation(image)]}, [image]
    )

    assert result[0]["status"] == "claimed"
    assert result[0]["evidence"]["image_sha256"] == image["content_sha256"]


def test_transport_schema_uses_flat_codex_subset_and_canonical_value_union():
    schema = visual_input.visual_output_schema()
    item = schema["properties"]["visual_observations"]["items"]
    encoded = str(schema)

    assert all(keyword not in encoded for keyword in ("allOf", "if", "then"))
    assert set(item["properties"]["value"]["enum"]) == {
        None,
        *(
            value
            for values in visual_input.VISUAL_VALUE_VOCABULARY.values()
            for value in values
        ),
    }
    assert item["additionalProperties"] is False


def test_visual_observation_cannot_treat_a_photo_as_a_parcel_outline():
    image = _image()
    observation = _observation(image, aspect_id="plot_outline", status="supported")
    observation["value"] = None
    with pytest.raises(visual_input.VisualInputError, match="parcel outline"):
        visual_input.validate_visual_observations(
            {"visual_observations": [observation]},
            [image],
        )


def test_local_validation_requires_unknown_observations_to_have_null_value():
    image = _image()
    observation = _observation(image, status="unknown")

    with pytest.raises(visual_input.VisualInputError, match="must have no value"):
        visual_input.validate_visual_observations(
            {"visual_observations": [observation]}, [image]
        )


def test_visual_observation_rejects_a_noncanonical_value_before_persistence():
    image = _image()
    observation = _observation(image)
    observation["value"] = "weathered exterior"
    with pytest.raises(visual_input.VisualInputError, match="canonical vocabulary"):
        visual_input.validate_visual_observations(
            {"visual_observations": [observation]}, [image]
        )


def test_local_validation_rejects_cross_aspect_value_allowed_by_flat_schema():
    image = _image()
    observation = _observation(image, aspect_id="house_condition")
    observation["value"] = "stone_house"

    with pytest.raises(visual_input.VisualInputError, match="canonical vocabulary"):
        visual_input.validate_visual_observations(
            {"visual_observations": [observation]}, [image]
        )


def test_local_validation_rejects_transport_valid_but_wrong_image_identity():
    image = _image()
    observation = _observation(image)
    observation["evidence"]["source_id"] = "property:other:photo:0"

    with pytest.raises(visual_input.VisualInputError, match="does not match"):
        visual_input.validate_visual_observations(
            {"visual_observations": [observation]}, [image]
        )


def test_oversized_png_dimensions_are_refused_before_model_input():
    data = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + (100_000).to_bytes(4, "big")
        + (100_000).to_bytes(4, "big")
    )
    with pytest.raises(visual_input.VisualInputError, match="pixel"):
        visual_input._sniff_content_type(data)


def test_extract_visual_observations_uses_codex_only(monkeypatch):
    image = _image()
    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return {"text": '{"visual_observations": []}'}

    monkeypatch.setattr(
        "services.subscription_transport.complete_with_images", fake_complete
    )
    result = visual_input.extract_visual_observations(
        "describe visible features", [image]
    )

    assert result["visual_observations"] == []
    assert captured["provider"] == "codex"
    assert captured["images"] == [image]
    assert captured["schema"] == visual_input.visual_output_schema()
    manifest_text = captured["prompt"].split("<IMAGE_EVIDENCE_MANIFEST>\n", 1)[1]
    manifest = json.loads(manifest_text.split("\n</IMAGE_EVIDENCE_MANIFEST>", 1)[0])
    assert manifest == [
        {
            "image_index": 0,
            "source_kind": "photo",
            "source_id": image["source_id"],
            "image_sha256": image["content_sha256"],
        }
    ]
    assert image["content_base64"] not in captured["prompt"]
    assert "http" not in captured["prompt"]


def test_extract_visual_observations_normalises_transport_failure(monkeypatch):
    image = _image()
    monkeypatch.setattr(
        "services.subscription_transport.complete_with_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subscription_transport.SubscriptionTransportError("bridge unavailable")
        ),
    )

    with pytest.raises(visual_input.VisualInputError, match="transport failed"):
        visual_input.extract_visual_observations("describe visible features", [image])
