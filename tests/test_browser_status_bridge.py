"""Only authenticated, canonical public URLs may reach native Chrome."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import pytest
from tests.js_harness import load_bridge

URL = "https://www.idealista.com/inmueble/12345/"
NONCE = "a" * 32


@pytest.fixture
def bridge_server(monkeypatch):
    bridge = load_bridge("browser_status_bridge_test")
    monkeypatch.setattr(bridge, "TOKEN", "test-token")
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield bridge, server
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def request(server, body, token="test-token"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/v1/listing-status",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_bridge_status_requires_auth_before_browser_work(bridge_server):
    bridge, server = bridge_server
    with patch.object(bridge, "check_listing_in_browser", create=True) as browser:
        status, _ = request(server, {"url": URL, "nonce": NONCE}, token="wrong")
    assert status == 401
    browser.assert_not_called()


def test_bridge_status_dispatches_validated_request_without_ai(bridge_server):
    bridge, server = bridge_server
    result = {"nonce": NONCE, "url": URL, "status": 200}
    with (
        patch.object(
            bridge, "check_listing_in_browser", create=True, return_value=result
        ) as browser,
        patch.dict(
            bridge.PROVIDERS, {"claude": lambda *a: pytest.fail("not an AI call")}
        ),
    ):
        status, answer = request(server, {"url": URL, "nonce": NONCE})
    assert status == 200 and answer == result
    browser.assert_called_once_with(URL, NONCE)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "https://www.idealista.com/login/",
        "https://www.idealista.com/inmueble/12345/$(id)/",
        "https://www.idealista.com/inmueble/12345/%24%28id%29/",
        "https://www.idealista.com/inmueble/12345/?secret=do-not-browse",
        "https://www.idealista.com/inmueble/12345/ extra",
        "https://www.idealista.com/inmueble/ñ/",
        "https://user:password@www.idealista.com/inmueble/12345/",
        "https://www.idealista.com:8443/inmueble/12345/",
    ],
)
def test_bridge_rejects_noncanonical_or_private_input(bridge_server, url):
    bridge, server = bridge_server
    with patch.object(bridge, "check_listing_in_browser", create=True) as browser:
        status, _ = request(server, {"url": url, "nonce": NONCE})
    assert status == 400
    browser.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"url": URL},
        {"url": URL, "nonce": "x$(id)"},
        {"url": URL, "nonce": NONCE, "command": "anything"},
    ],
)
def test_bridge_rejects_wrong_payload_shape(bridge_server, body):
    bridge, server = bridge_server
    with patch.object(bridge, "check_listing_in_browser", create=True) as browser:
        status, _ = request(server, body)
    assert status == 400
    browser.assert_not_called()
