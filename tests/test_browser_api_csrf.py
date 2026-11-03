"""A foreign form POST used to toggle favorites with CSRF enabled.

Observe real stored favorites and job submission with production CSRF checks,
including the token/cookie pair delivered by an actual rendered listing page.
"""

from html.parser import HTMLParser
from unittest.mock import Mock

import pytest

from app import create_app, db
from models import Land, Property
from tests import setup_test_environment


class _TokenParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            self.token = values.get("content")


@pytest.fixture
def application():
    setup_test_environment()
    app = create_app(testing=True)
    app.config.update(WTF_CSRF_ENABLED=True, RATELIMIT_ENABLED=False)
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                Property(
                    id=1,
                    source_email_id="csrf-property",
                    title="CSRF property",
                    property_category="housing",
                    property_subtype="house",
                    is_favorite=False,
                ),
                Land(
                    id=1,
                    source_email_id="csrf-land",
                    title="CSRF land",
                    is_favorite=False,
                ),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _page_token(client):
    page = client.get("/properties", base_url="https://localhost")
    assert page.status_code == 200
    parser = _TokenParser()
    parser.feed(page.get_data(as_text=True))
    assert parser.token
    return parser.token


@pytest.mark.parametrize("resource,model", [("property", Property), ("land", Land)])
@pytest.mark.parametrize("body_type", ["form", "json"])
def test_tokenless_foreign_request_cannot_change_favorite(
    application, resource, model, body_type
):
    client = application.test_client(use_cookies=False)
    body = {"json": {}} if body_type == "json" else {"data": {}}
    response = client.post(
        f"/api/{resource}/1/favorite",
        base_url="https://localhost",
        headers={"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"},
        **body,
    )
    assert response.status_code == 400
    assert response.is_json and response.json["success"] is False
    with application.app_context():
        assert db.session.get(model, 1).is_favorite is False


@pytest.mark.parametrize("token_kind", ["missing", "forged", "another_session"])
def test_cookie_alone_or_unrelated_token_cannot_authorize_write(
    application, token_kind
):
    client = application.test_client()
    _page_token(client)
    headers = {"Referer": "https://localhost/properties"}
    if token_kind == "forged":
        headers["X-CSRFToken"] = "not-a-signed-token"
    elif token_kind == "another_session":
        headers["X-CSRFToken"] = _page_token(application.test_client())
    response = client.post(
        "/api/property/1/favorite",
        json={},
        headers=headers,
        base_url="https://localhost",
    )
    assert response.status_code == 400
    assert response.is_json and response.json["success"] is False
    with application.app_context():
        assert db.session.get(Property, 1).is_favorite is False


@pytest.mark.parametrize("resource,model", [("property", Property), ("land", Land)])
def test_page_cookie_and_token_allow_existing_favorite_button(
    application, resource, model
):
    client = application.test_client()
    token = _page_token(client)
    response = client.post(
        f"/api/{resource}/1/favorite",
        json={},
        headers={"X-CSRFToken": token, "Referer": "https://localhost/properties"},
        base_url="https://localhost",
    )
    assert response.status_code == 200 and response.json["is_favorite"] is True
    with application.app_context():
        assert db.session.get(model, 1).is_favorite is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/property/1/enrich",
        "/api/property/1/analyze/structured",
        "/api/land/1/enrich",
    ],
)
def test_csrf_refusal_precedes_job_submission(application, monkeypatch, path):
    # Exercise the production queue branch if the CSRF check regresses,
    # without running an enrichment synchronously under TESTING.
    application.config["TESTING"] = False
    enqueue = Mock(side_effect=AssertionError("CSRF refusal must precede enqueue"))
    monkeypatch.setattr("routes.api_routes._enqueue", enqueue)
    response = application.test_client().post(
        path, json={}, base_url="https://localhost"
    )
    assert response.status_code == 400
    assert response.json["success"] is False
    enqueue.assert_not_called()


def test_language_write_uses_token_and_health_remains_readable(
    application, monkeypatch
):
    client = application.test_client()
    refused = client.post(
        "/api/set-language", json={"language": "es"}, base_url="https://localhost"
    )
    assert refused.status_code == 400
    token = _page_token(client)
    accepted = client.post(
        "/api/set-language",
        json={"language": "es"},
        headers={"X-CSRFToken": token, "Referer": "https://localhost/properties"},
        base_url="https://localhost",
    )
    assert accepted.status_code == 200 and accepted.json["language"] == "es"
    monkeypatch.setattr(
        "services.health_service.collect_health_checks",
        lambda _db: {"database": "ok", "schema": "ok", "scheduler": "running"},
    )
    health = application.test_client(use_cookies=False).get("/api/healthz")
    assert health.status_code == 200 and health.json["ok"] is True


def test_browser_transport_preserves_requests_and_keeps_tokens_same_origin(tmp_path):
    """Browser writes carry the page token; reads/outside requests do not leak it."""
    from pathlib import Path

    from tests.js_harness import run_node_script

    helper = Path(__file__).parents[1] / "static/js/csrf.js"
    result = run_node_script(
        """
        const calls = [];
        const listeners = {};
        const window = {location: {href: 'https://owner.example/properties', origin: 'https://owner.example'}};
        const document = {
            querySelector: () => ({content: 'page-session-token'}),
            addEventListener: (event, callback) => { listeners[event] = callback; },
        };
        global.fetch = async (input, init) => {
            const request = new Request(input instanceof Request ? input : new URL(input, window.location.href), init);
            calls.push({
                method: request.method, headers: Object.fromEntries(request.headers),
                body: await request.text(), credentials: request.credentials,
            });
            return {status: 202};
        };
        """
        + helper.read_text()
        + """
        (async () => {
            const response = await window.csrfFetch('/api/property/1/favorite', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: '{"chosen":true}', credentials: 'same-origin',
            });
            await window.csrfFetch('/api/properties', {method: 'GET'});
            await window.csrfFetch('https://outside.example/action', {method: 'POST'});
            const request = new Request('https://owner.example/api/action', {
                method: 'PATCH', headers: {'X-Intent': 'keep'},
            });
            await window.csrfFetch(request);
            const htmx = {detail: {path: '/api/ingest/manual', verb: 'post', headers: {'HX-Request': 'true'}}};
            listeners['htmx:configRequest'](htmx);
            const outsideHtmx = {detail: {path: 'https://outside.example/action', verb: 'post', headers: {}}};
            listeners['htmx:configRequest'](outsideHtmx);
            console.log(JSON.stringify({calls, status: response.status, htmx: htmx.detail.headers, outsideHtmx: outsideHtmx.detail.headers}));
        })().catch(error => {console.error(error); process.exit(1);});
        """,
        tmp_path,
    )
    assert result["status"] == 202
    write, read, outside, request = result["calls"]
    assert write == {
        "method": "POST",
        "headers": {
            "content-type": "application/json",
            "x-csrftoken": "page-session-token",
        },
        "body": '{"chosen":true}',
        "credentials": "same-origin",
    }
    assert "x-csrftoken" not in read["headers"]
    assert "x-csrftoken" not in outside["headers"]
    assert request["method"] == "PATCH"
    assert request["headers"] == {
        "x-intent": "keep",
        "x-csrftoken": "page-session-token",
    }
    assert result["htmx"] == {
        "HX-Request": "true",
        "X-CSRFToken": "page-session-token",
    }
    assert result["outsideHtmx"] == {}
