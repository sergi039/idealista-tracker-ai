"""Portal traffic cannot borrow the owner's direct route or retry a refusal."""

import os
import socket
import subprocess
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib3.util.retry import Retry

import pytest
import requests

from config import Config
from services import fotocasa_source, milanuncios_source, visual_input
from utils.http import PortalEgressUnavailable, request_with_retries


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "direct",
        "socks5://localhost:9",
        "http://localhost:bad",
        "http://localhost:0",
        "http://demo:demo-secret@localhost:9",
        "http://localhost:9?token=demo-secret",
        "http://localhost:9/path",
        "http://localhost:9#demo-secret",
        " http://localhost:9",
    ],
)
def test_invalid_or_absent_route_never_calls_transport(monkeypatch, value):
    monkeypatch.setattr(Config, "PORTAL_HTTP_PROXY_URL", value)
    transport = Mock()
    with pytest.raises(PortalEgressUnavailable) as error:
        request_with_retries(transport, "https://www.fotocasa.es/", portal_egress=True)
    assert "demo-secret" not in str(error.value)
    transport.assert_not_called()


def test_explicit_route_overrides_ambient_and_caller_routes_without_retry(monkeypatch):
    proxy = "http://127.0.0.1:19876"
    monkeypatch.setattr(Config, "PORTAL_HTTP_PROXY_URL", proxy)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:9")
    monkeypatch.setenv("NO_PROXY", "*")
    refused = Mock(status_code=429)
    transport = Mock(side_effect=[refused, Mock(status_code=200)])
    result = request_with_retries(
        transport,
        "https://www.fotocasa.es/",
        portal_egress=True,
        proxies={"https": None},
        max_attempts=3,
    )
    assert result is refused
    transport.assert_called_once()
    routing = transport.call_args.kwargs["proxies"]
    assert routing["http"] == routing["https"] == proxy
    assert set(routing.values()) == {proxy}


def test_non_portal_retry_contract_is_unchanged(monkeypatch):
    monkeypatch.setattr(Config, "PORTAL_HTTP_PROXY_URL", "")
    good = Mock(status_code=200)
    transport = Mock(side_effect=[Mock(status_code=429), good])
    with patch("utils.http.time.sleep"):
        assert (
            request_with_retries(
                transport, "https://public-api.example/", max_attempts=2
            )
            is good
        )
    assert transport.call_count == 2
    assert "proxies" not in transport.call_args.kwargs


@pytest.mark.parametrize(
    "route_override",
    ["none", "session_host", "environment_host", "partial_keyword", "unbound_keyword"],
)
def test_dead_proxy_cannot_fall_back_to_direct_loopback_target(
    monkeypatch, route_override
):
    direct_hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            direct_hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=target.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    # Bound but not listening: the port is unavailable and cannot be claimed
    # by another parallel test between discovery and this request.
    with socket.socket() as dead_proxy:
        dead_proxy.bind(("127.0.0.1", 0))
        monkeypatch.setattr(
            Config,
            "PORTAL_HTTP_PROXY_URL",
            f"http://127.0.0.1:{dead_proxy.getsockname()[1]}",
        )
        monkeypatch.setenv("NO_PROXY", "*")
        session = requests.Session()
        if route_override in {"session_host", "partial_keyword", "unbound_keyword"}:
            session.proxies["http://127.0.0.1"] = ""
        elif route_override == "environment_host":
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv(
                "http://127.0.0.1_proxy", f"http://127.0.0.1:{target.server_port}"
            )
        transport = session.get
        call_kwargs = {}
        if route_override in {"partial_keyword", "unbound_keyword"}:
            session.trust_env = False
            if route_override == "partial_keyword":
                transport = partial(requests.Session.get, self=session)
            else:
                transport = requests.Session.get
                call_kwargs["self"] = session
        try:
            with pytest.raises(requests.exceptions.ProxyError):
                request_with_retries(
                    transport,
                    url=f"http://127.0.0.1:{target.server_port}/listing",
                    portal_egress=True,
                    timeout=(0.2, 0.2),
                    **call_kwargs,
                )
            assert direct_hits == []
        finally:
            session.close()
            target.shutdown()
            target.server_close()
            thread.join(timeout=2)


@pytest.mark.parametrize(
    "call_kind",
    ["bound", "partial_bound", "partial_unbound", "partial_keyword", "unbound_keyword"],
)
def test_retrying_adapter_is_refused_before_it_can_repeat_a_portal_request(
    monkeypatch, call_kind
):
    hits = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(
        target=proxy.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    session = requests.Session()
    session.mount(
        "http://",
        requests.adapters.HTTPAdapter(
            max_retries=Retry(
                total=1,
                status_forcelist=[429],
                raise_on_status=False,
            )
        ),
    )
    monkeypatch.setattr(
        Config, "PORTAL_HTTP_PROXY_URL", f"http://127.0.0.1:{proxy.server_port}"
    )
    transport = {
        "bound": session.get,
        "partial_bound": partial(session.get),
        "partial_unbound": partial(requests.Session.get, session),
        "partial_keyword": partial(requests.Session.get, self=session),
        "unbound_keyword": requests.Session.get,
    }[call_kind]
    call_kwargs = {"self": session} if call_kind == "unbound_keyword" else {}
    try:
        try:
            request_with_retries(
                transport,
                url="http://portal.example/listing",
                portal_egress=True,
                timeout=1,
                **call_kwargs,
            )
        except PortalEgressUnavailable:
            pass
        else:
            pytest.fail(f"unsafe adapter used {len(hits)} HTTP attempts")
        assert hits == []
    finally:
        session.close()
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "reader,url",
    [
        (
            fotocasa_source.fetch_listing,
            "https://www.fotocasa.es/es/comprar/vivienda/cedeira/jardin/190889632/d",
        ),
        (
            milanuncios_source.fetch_listing,
            "https://www.milanuncios.com/venta-de-chalets/casa-612329827.htm",
        ),
        (milanuncios_source.resolve_tracker, "https://tracker.example/card"),
    ],
)
def test_real_portal_readers_refuse_before_fetch_without_route(
    monkeypatch, reader, url
):
    monkeypatch.setattr(Config, "PORTAL_HTTP_PROXY_URL", "")
    session = Mock()
    reader(url, session=session)
    session.get.assert_not_called()


def test_photo_reader_refuses_before_fetch_without_route(monkeypatch):
    monkeypatch.setattr(Config, "PORTAL_HTTP_PROXY_URL", "")
    session = Mock()
    with pytest.raises(visual_input.VisualInputError):
        visual_input.download_photo_source(
            {
                "source_kind": "portal_photo",
                "source_id": "property:1:photo:0",
                "url": "https://media.yaencontre.com/img/photo/1.jpg",
            },
            session=session,
        )
    session.get.assert_not_called()


def test_real_production_defaults_block_known_refused_sources_outside_fixtures():
    env = dict(os.environ)
    env.pop("PORTAL_HTTP_PROXY_URL", None)
    script = """
from config import Config
from services.listing_status_service import ListingStatusService
from utils.http import portal_proxies, PortalEgressUnavailable
from unittest.mock import patch
assert Config.PORTAL_HTTP_PROXY_URL == ''
try:
    portal_proxies()
except PortalEgressUnavailable:
    pass
else:
    raise AssertionError('missing route allowed')
with patch('services.listing_status_service.request_with_retries') as transport:
    for host in ['www.idealista.com', 'www.yaencontre.com']:
        result = ListingStatusService().observe('https://' + host + '/inmueble/123/')
        assert result.status == 'error' and result.refusal == 'blocked'
    transport.assert_not_called()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
