"""The image bridge accepts bytes, never a caller-controlled host path."""

import base64
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tests.js_harness import load_bridge


JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x01\x00\x01" + b"bridge-image"


@pytest.fixture
def bridge():
    return load_bridge("ai_bridge_visual_input_under_test")


def _image(data=JPEG):
    return {
        "source_kind": "portal_photo",
        "source_id": "property:1375:photo:0",
        "content_type": "image/jpeg",
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def _serve(bridge):
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(server, payload):
    return urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/complete",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-token",
            },
            method="POST",
        ),
        timeout=10,
    )


def test_handler_writes_hash_verified_images_and_removes_them(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    seen = {}

    def fake_codex(prompt, system, model, timeout, schema=None, image_paths=None):
        assert image_paths and len(image_paths) == 1
        path = image_paths[0]
        seen["path"] = path
        seen["bytes"] = open(path, "rb").read()
        assert os.path.basename(path).startswith("ai-bridge-image-")
        return {"text": "{}", "usage": {}, "provider": "codex"}

    monkeypatch.setitem(bridge.PROVIDERS, "codex", fake_codex)
    server = _serve(bridge)
    try:
        response = _post(
            server, {"provider": "codex", "prompt": "hi", "images": [_image()]}
        )
        assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()

    assert seen["bytes"] == JPEG
    assert not os.path.exists(seen["path"])


def test_handler_refuses_tampered_image_bytes_before_starting_a_cli(
    bridge, monkeypatch
):
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    called = []
    monkeypatch.setitem(
        bridge.PROVIDERS, "codex", lambda *args, **kwargs: called.append(1)
    )
    image = _image()
    image["content_base64"] = base64.b64encode(b"\xff\xd8\xffchanged").decode()
    server = _serve(bridge)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, {"provider": "codex", "prompt": "hi", "images": [image]})
        assert excinfo.value.code == 400
    finally:
        server.shutdown()
        server.server_close()

    assert called == []


def test_handler_refuses_claude_image_payload_without_running_it(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    called = []
    monkeypatch.setitem(
        bridge.PROVIDERS, "claude", lambda *args, **kwargs: called.append(1)
    )
    server = _serve(bridge)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, {"provider": "claude", "prompt": "hi", "images": [_image()]})
        assert excinfo.value.code == 400
    finally:
        server.shutdown()
        server.server_close()

    assert called == []
