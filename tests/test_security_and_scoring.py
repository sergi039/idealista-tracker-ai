"""
TEST-01: Tests for security (auth, CSRF) and scoring DB-weight integration.

Ported from Legacy to Universal.  Covers:
  - Unauthorized POST → 401 on all protected endpoints (Land + Property)
  - Session-based auth flow (login, access, logout)
  - Scoring uses DB profile weights (not hardcoded)
  - DB weight change → actual score change
  - Combined mix persistence to DB
  - Error messages don't leak internals
"""

import os
import pytest
import json
from decimal import Decimal
from unittest.mock import patch
from app import create_app, db
from config import Config
from models import Land, Property, ScoringCriteria
from services.scoring_service import ScoringService
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def isolated_app():
    """Like `app`, but points SQLALCHEMY_DATABASE_URI at a real in-memory
    database *before* create_app()/db.init_app() bind the engine.

    The `app` fixture above sets `app.config['SQLALCHEMY_DATABASE_URI']`
    *after* create_app() returns, which is too late: db.init_app() already
    bound the engine to DATABASE_URL from the environment, which
    tests/__init__.py points at a real file (sqlite:///test.db) shared by
    every test module's `app` fixture across the whole suite. Late in a full
    `pytest tests/` run, that file accumulates enough concurrently-open
    connections from other modules' never-disposed engines that a handful of
    admin-authenticated page renders in this file (land detail, map, CSV
    export — heavier than the rest of the suite's API-only tests) can hit a
    genuine 'database is locked' on this fixture's own db.drop_all().
    Overriding DATABASE_URL before create_app() gives these tests a private,
    uncontended in-memory database instead.
    """
    setup_test_environment()
    orig_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    try:
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            yield app
            db.drop_all()
    finally:
        if orig_db_url is not None:
            os.environ["DATABASE_URL"] = orig_db_url
        else:
            os.environ.pop("DATABASE_URL", None)


@pytest.fixture
def disposed_client(isolated_app):
    return isolated_app.test_client()


@pytest.fixture
def isolated_test_land(isolated_app):
    """Create a land in the isolated_app's private in-memory DB."""
    with isolated_app.app_context():
        land = Land(
            source_email_id="isolated_test_1",
            title="Isolated Test Land",
            municipality="Valencia",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
        )
        db.session.add(land)
        db.session.commit()
        return land.id


@pytest.fixture
def isolated_test_property(isolated_app):
    """Create a property in the isolated_app's private in-memory DB."""
    with isolated_app.app_context():
        prop = Property(
            source_email_id="isolated_test_prop_1",
            title="Isolated Test Property",
            municipality="Barcelona",
            property_category="housing",
            property_subtype="apartment",
            price=Decimal("250000.00"),
            area=Decimal("90.00"),
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


@pytest.fixture
def auth_disabled_app():
    """App fixture with TESTING=False so auth checks are enforced.
    ADMIN_API_TOKEN is NOT set so all requests are denied (fail-closed)."""
    setup_test_environment()
    orig_token = os.environ.pop("ADMIN_API_TOKEN", None)
    app = create_app()
    app.config["TESTING"] = False
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()
    if orig_token is not None:
        os.environ["ADMIN_API_TOKEN"] = orig_token


@pytest.fixture
def auth_disabled_client(auth_disabled_app):
    return auth_disabled_app.test_client()


@pytest.fixture
def test_land(app):
    """Create a land with enriched data for scoring tests."""
    with app.app_context():
        land = Land(
            source_email_id="sec_test_1",
            title="Security Test Land",
            municipality="Valencia",
            land_type="developed",
            price=Decimal("150000.00"),
            area=Decimal("1500.00"),
            location_lat=Decimal("39.4699"),
            location_lon=Decimal("-0.3763"),
            description="Parcela urbana con vistas al mar y orientación sur",
            legal_status="Developed",
            infrastructure_basic={
                "electricity": True,
                "water": True,
                "internet": False,
                "gas": True,
            },
            infrastructure_extended={
                "supermarket_available": True,
                "supermarket_distance": 800,
                "school_available": True,
                "school_distance": 1200,
                "hospital_available": True,
                "hospital_distance": 2500,
                "restaurant_available": True,
                "restaurant_distance": 500,
            },
            transport={
                "train_station_available": True,
                "train_station_distance": 3000,
                "bus_station_available": True,
                "bus_station_distance": 600,
                "airport_available": True,
                "airport_distance": 45000,
            },
            environment={
                "sea_view": True,
                "mountain_view": False,
                "forest_view": False,
                "orientation": "south",
            },
            services_quality={
                "school_avg_rating": 4.2,
                "restaurant_avg_rating": 4.5,
                "cafe_avg_rating": 4.0,
            },
        )
        db.session.add(land)
        db.session.commit()
        return land.id


@pytest.fixture
def test_property(app):
    """Create a universal Property for endpoint auth tests."""
    with app.app_context():
        prop = Property(
            source_email_id="sec_test_prop_1",
            title="Security Test Property",
            municipality="Barcelona",
            property_category="housing",
            property_subtype="apartment",
            price=Decimal("250000.00"),
            area=Decimal("90.00"),
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


@pytest.fixture
def auth_disabled_test_land(auth_disabled_app):
    """Create a land in the auth-disabled app context."""
    with auth_disabled_app.app_context():
        land = Land(
            source_email_id="auth_test_1",
            title="Auth Test Land",
            municipality="Madrid",
            land_type="buildable",
            price=Decimal("100000.00"),
            area=Decimal("1000.00"),
        )
        db.session.add(land)
        db.session.commit()
        return land.id


@pytest.fixture
def auth_disabled_test_property(auth_disabled_app):
    """Create a property in the auth-disabled app context."""
    with auth_disabled_app.app_context():
        prop = Property(
            source_email_id="auth_prop_test_1",
            title="Auth Test Property",
            municipality="Barcelona",
            property_category="housing",
            property_subtype="apartment",
            price=Decimal("250000.00"),
            area=Decimal("90.00"),
        )
        db.session.add(prop)
        db.session.commit()
        return prop.id


# ---------------------------------------------------------------------------
# Security: unauthorized POST → 401
# ---------------------------------------------------------------------------
class TestUnauthorizedAccess:
    """Admin-only endpoints must return 401 for anonymous requests."""

    PROTECTED_POST_ENDPOINTS = [
        "/api/ingest/email/run",
        "/api/lands/enrich-all",
    ]

    def test_anonymous_post_returns_401(
        self, auth_disabled_client, auth_disabled_test_land
    ):
        """All protected Land POST endpoints must reject anonymous requests."""
        lid = auth_disabled_test_land
        endpoints = self.PROTECTED_POST_ENDPOINTS + [
            f"/api/land/{lid}/enrich",
            f"/api/analyze/property/{lid}/structured",
            f"/api/analysis/generate/{lid}/openai",
            f"/api/enhance/description/{lid}",
            f"/api/land/{lid}/environment",
            f"/api/analyze/property/{lid}",
            f"/api/land/{lid}/set-status",
            f"/api/land/{lid}/check-status",
        ]

        for endpoint in endpoints:
            resp = auth_disabled_client.post(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )
            data = json.loads(resp.data)
            assert data["success"] is False

    def test_anonymous_property_post_returns_401(
        self, auth_disabled_client, auth_disabled_test_property
    ):
        """All protected Property POST endpoints must reject anonymous requests."""
        pid = auth_disabled_test_property
        property_endpoints = [
            f"/api/property/{pid}/enrich",
            f"/api/property/{pid}/set-status",
            f"/api/property/{pid}/analyze/structured",
            f"/api/property/{pid}/environment",
        ]

        for endpoint in property_endpoints:
            resp = auth_disabled_client.post(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )

    def test_anonymous_favorite_toggle_is_allowed(
        self, auth_disabled_client, auth_disabled_test_land, auth_disabled_test_property
    ):
        """Favorites are user-facing actions and should work without admin auth."""
        lid = auth_disabled_test_land
        pid = auth_disabled_test_property

        land_resp = auth_disabled_client.post(f"/api/land/{lid}/favorite")
        assert land_resp.status_code == 200
        land_data = json.loads(land_resp.data)
        assert land_data["success"] is True
        assert land_data["is_favorite"] is True

        property_resp = auth_disabled_client.post(f"/api/property/{pid}/favorite")
        assert property_resp.status_code == 200
        property_data = json.loads(property_resp.data)
        assert property_data["success"] is True
        assert property_data["is_favorite"] is True

    def test_anonymous_put_returns_401(self, auth_disabled_client):
        """PUT /api/criteria must reject anonymous requests."""
        resp = auth_disabled_client.put(
            "/api/criteria",
            data=json.dumps({"criteria": {"test": 0.5}}),
            content_type="application/json",
        )
        assert resp.status_code == 401

    def test_health_check_accessible_without_auth(self, auth_disabled_client):
        """The health check must stay public (used by Docker/monitoring)."""
        resp = auth_disabled_client.get("/api/healthz")
        assert resp.status_code == 200


class TestPropertyDataRequiresAuth:
    """Issue #30: the entire property database must not be readable or
    bulk-exportable without authentication. Every endpoint that returns
    listing data must reject anonymous requests."""

    PROTECTED_API_GET_ENDPOINTS = [
        "/api/lands",
        "/api/stats",
    ]

    def test_anonymous_get_returns_401_on_api_endpoints(self, auth_disabled_client):
        """Anonymous GET to data-dumping JSON API endpoints must return 401."""
        for endpoint in self.PROTECTED_API_GET_ENDPOINTS:
            resp = auth_disabled_client.get(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )
            data = json.loads(resp.data)
            assert data["success"] is False

    def test_anonymous_get_returns_401_on_land_scoped_endpoints(
        self, auth_disabled_client, auth_disabled_test_land
    ):
        """Anonymous GET to per-land data endpoints must return 401."""
        lid = auth_disabled_test_land
        endpoints = [
            f"/api/lands/{lid}",
            f"/api/land/{lid}/history",
            f"/api/analysis/compare/{lid}",
            f"/api/description/variants/{lid}",
        ]
        for endpoint in endpoints:
            resp = auth_disabled_client.get(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )

    def test_anonymous_get_returns_401_on_property_scoped_endpoints(
        self, auth_disabled_client, auth_disabled_test_property
    ):
        """Anonymous GET to per-property data endpoints must return 401."""
        pid = auth_disabled_test_property
        endpoints = [
            f"/api/properties/{pid}",
            f"/api/property/{pid}/analysis/compare",
        ]
        for endpoint in endpoints:
            resp = auth_disabled_client.get(endpoint)
            assert resp.status_code == 401, (
                f"{endpoint} returned {resp.status_code}, expected 401"
            )

    def test_anonymous_get_properties_returns_401(self, auth_disabled_client):
        """/api/properties (the ?full=1-capable bulk dump) must require auth."""
        resp = auth_disabled_client.get("/api/properties")
        assert resp.status_code == 401
        resp_full = auth_disabled_client.get("/api/properties?full=1")
        assert resp_full.status_code == 401

    def test_anonymous_page_access_redirects_to_login(
        self, auth_disabled_client, auth_disabled_test_land, auth_disabled_test_property
    ):
        """Anonymous requests to HTML pages rendering property data must
        redirect to the login page rather than rendering the data."""
        lid = auth_disabled_test_land
        pid = auth_disabled_test_property
        pages = [
            "/properties",
            f"/properties/{pid}",
            "/map",
            "/criteria",
            f"/lands/{lid}",
        ]
        for page in pages:
            resp = auth_disabled_client.get(page)
            assert resp.status_code in (302, 401), (
                f"{page} returned {resp.status_code}, expected a redirect to login"
            )
            if resp.status_code == 302:
                assert "/login" in resp.headers.get("Location", ""), (
                    f"{page} redirected to {resp.headers.get('Location')}, expected /login"
                )

    def test_anonymous_csv_export_denied(self, auth_disabled_client):
        """The bulk CSV export endpoints must not leak data anonymously."""
        for endpoint in ["/export.csv", "/properties/export.csv"]:
            resp = auth_disabled_client.get(endpoint)
            assert resp.status_code in (302, 401), (
                f"{endpoint} returned {resp.status_code}, expected a redirect/401, "
                "not the exported CSV"
            )
            assert "text/csv" not in resp.headers.get("Content-Type", "")

    # Sanity checks below confirm the TESTING bypass (acting as an authenticated
    # admin) can still reach every endpoint we just locked down. Split into
    # several small tests (instead of one big loop) to keep each test's request
    # count low: sqlite's in-memory test DB is prone to spurious "database is
    # locked" errors on teardown when a single test fires many full,
    # DB-heavy view renders back-to-back.
    def test_authenticated_admin_can_view_property_pages(self, client, test_property):
        pid = test_property
        for endpoint in [
            "/properties",
            f"/properties/{pid}",
            "/api/properties",
            f"/api/properties/{pid}",
        ]:
            resp = client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200 for an admin"
            )

    def test_authenticated_admin_can_view_land_pages(
        self, disposed_client, isolated_test_land
    ):
        lid = isolated_test_land
        for endpoint in ["/map", "/criteria", f"/lands/{lid}"]:
            resp = disposed_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200 for an admin"
            )

    def test_authenticated_admin_can_export_csv(
        self, disposed_client, isolated_test_land, isolated_test_property
    ):
        for endpoint in ["/export.csv", "/properties/export.csv"]:
            resp = disposed_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200 for an admin"
            )

    def test_authenticated_admin_can_read_land_apis(self, client, test_land):
        lid = test_land
        for endpoint in ["/api/lands", f"/api/lands/{lid}", "/api/stats"]:
            resp = client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200 for an admin"
            )

    def test_authenticated_admin_can_read_land_history_and_comparison(
        self, client, test_land
    ):
        lid = test_land
        for endpoint in [f"/api/land/{lid}/history", f"/api/analysis/compare/{lid}"]:
            resp = client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200 for an admin"
            )
        # /api/description/variants/<id> is intentionally not exercised here:
        # it needs a configured ANTHROPIC_API_KEY to build DescriptionService,
        # unrelated to the auth gate under test. Its 401-on-anonymous behavior
        # is covered by test_anonymous_get_returns_401_on_land_scoped_endpoints.


# ---------------------------------------------------------------------------
# Security: the full project source archive must not be downloadable
# ---------------------------------------------------------------------------
class TestSourceArchiveNotServed:
    """Issue #29: GET /api/download/project served the full project source
    archive (zip/tar.gz/txt) to anyone, unauthenticated, via
    send_from_directory. Gating that route alone is not enough: the archive
    files and the download.html page linking to them lived under static/,
    which Flask serves directly at /static/<name> regardless of any
    @admin_required decorator on an unrelated api_routes.py route. The real
    fix removes the archives (and the dead route) from the repo entirely."""

    ARCHIVE_STATIC_PATHS = [
        "/static/idealista-project-new.zip",
        "/static/idealista-project.tar.gz",
        "/static/all_code.txt",
        "/static/download.html",
    ]

    def test_archive_route_no_longer_exists(self, auth_disabled_client, client):
        """The dead /api/download/project endpoint must not come back as a
        live route, for anonymous or authenticated callers."""
        for c in (auth_disabled_client, client):
            resp = c.get("/api/download/project")
            assert resp.status_code == 404, (
                f"/api/download/project returned {resp.status_code}, expected 404 (route removed)"
            )

    def test_archive_files_not_served_from_static_anonymous(self, auth_disabled_client):
        """None of the source-archive files must be reachable under /static/,
        which Flask serves unauthenticated regardless of any route decorator."""
        for path in self.ARCHIVE_STATIC_PATHS:
            resp = auth_disabled_client.get(path)
            assert resp.status_code == 404, (
                f"{path} returned {resp.status_code}, expected 404 (file must not exist in static/)"
            )

    def test_archive_files_not_served_from_static_authenticated(self, client):
        """Same check as an authenticated admin: the files must be gone, not
        merely access-gated, since static/ bypasses admin_required entirely."""
        for path in self.ARCHIVE_STATIC_PATHS:
            resp = client.get(path)
            assert resp.status_code == 404, (
                f"{path} returned {resp.status_code}, expected 404 (file must not exist in static/)"
            )

    def test_archive_files_not_present_on_disk(self):
        """Filesystem-level guard against silently recommitting the bundles:
        regression coverage above only proves the *current* checkout is
        clean, so also assert the files aren't sitting in static/ at all."""
        import os as _os

        static_dir = _os.path.join(
            _os.path.dirname(_os.path.dirname(__file__)), "static"
        )
        for name in (
            "idealista-project-new.zip",
            "idealista-project.tar.gz",
            "all_code.txt",
            "download.html",
        ):
            assert not _os.path.exists(_os.path.join(static_dir, name)), (
                f"static/{name} must not be committed (issue #29: unauthenticated source disclosure)"
            )


# ---------------------------------------------------------------------------
# Security: /settings/properties must not expose ADMIN_API_TOKEN via a
# JS-readable cookie (issue #20)
# ---------------------------------------------------------------------------
@pytest.fixture
def token_login_app():
    """App fixture with TESTING=False and a known ADMIN_API_TOKEN, so the
    real POST /login -> session -> POST /settings/properties round trip can
    be exercised without the TESTING bypass masking auth behaviour."""
    setup_test_environment()
    orig_token = os.environ.get("ADMIN_API_TOKEN")
    os.environ["ADMIN_API_TOKEN"] = "unit-test-admin-token"
    app = create_app()
    app.config["TESTING"] = False
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()
    if orig_token is not None:
        os.environ["ADMIN_API_TOKEN"] = orig_token
    else:
        os.environ.pop("ADMIN_API_TOKEN", None)


@pytest.fixture
def token_login_client(token_login_app):
    return token_login_app.test_client()


class TestPropertySettingsNoTokenCookie:
    """Issue #20: the "Unlock" widget on /settings/properties wrote the
    master ADMIN_API_TOKEN into a non-HttpOnly, JS-readable `admin_token`
    cookie via `document.cookie`, and nothing server-side ever read that
    cookie back (check_admin_auth only looks at the Authorization header and
    the Flask session) -- so the widget didn't even authenticate the user.
    The fix removes the paste-a-token widget and points anonymous users at
    the real POST /login flow, which establishes a proper server-side
    session instead of a client-readable credential."""

    def test_anonymous_settings_page_has_no_token_widget(self, auth_disabled_client):
        """The dead cookie-writing widget and its JS must be gone entirely."""
        resp = auth_disabled_client.get("/settings/properties")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        # The widget's markup/ids must not be present.
        assert "admin-token-input" not in body
        assert "admin-token-save" not in body
        assert "admin-token-clear" not in body
        assert "Paste ADMIN_API_TOKEN" not in body

        # The cookie-writing JS must not be present.
        assert "document.cookie" not in body
        assert "admin_token=" not in body

        # Anonymous users get a real path to authenticate instead.
        assert 'href="/login' in body or 'href="/login' in body

    def test_settings_page_response_sets_no_admin_token_cookie(
        self, auth_disabled_client
    ):
        """The server itself must never set an admin_token cookie either."""
        resp = auth_disabled_client.get("/settings/properties")
        assert resp.status_code == 200
        set_cookie_headers = resp.headers.get_all("Set-Cookie")
        assert not any("admin_token" in h for h in set_cookie_headers)

    def test_login_flow_grants_access_without_client_side_token_storage(
        self, token_login_client
    ):
        """The real fix: POST /login with the correct token establishes a
        server-side session (admin_authenticated), and the settings page
        then renders as authenticated -- with no admin_token cookie ever
        set or read along the way."""
        # Anonymous: read-only, warning banner shown, no admin_token cookie.
        anon_resp = token_login_client.get("/settings/properties")
        assert anon_resp.status_code == 200
        assert "Admin authentication is required" in anon_resp.get_data(as_text=True)
        assert not any(
            "admin_token" in h for h in anon_resp.headers.get_all("Set-Cookie")
        )

        # Log in via the real, CSRF-protected, session-based flow.
        login_resp = token_login_client.post(
            "/login",
            data={"token": "unit-test-admin-token", "next": "/settings/properties"},
        )
        assert login_resp.status_code == 302
        assert "/settings/properties" in login_resp.headers.get("Location", "")
        assert not any(
            "admin_token" in h for h in login_resp.headers.get_all("Set-Cookie")
        )

        # Now authenticated via the session cookie set by Flask itself, not
        # a hand-rolled JS cookie holding the master token.
        auth_resp = token_login_client.get("/settings/properties")
        assert auth_resp.status_code == 200
        body = auth_resp.get_data(as_text=True)
        assert "Admin authentication is required" not in body
        assert "admin_token" not in body


# ---------------------------------------------------------------------------
# Security: open redirects via login `next` and Referer-based redirects
# (issue #17)
# ---------------------------------------------------------------------------
class TestOpenRedirectGuard:
    """Issue #17: the login page's `next` parameter and several admin POST
    handlers' "redirect back to where you came from" both trusted fully
    client-controlled input (a query/form value, the Referer header) and
    redirected to it verbatim. A crafted link or cross-origin form post
    could bounce an authenticated admin's browser to an attacker-controlled
    origin. Both paths must fall back to a safe same-site default whenever
    the supplied target isn't a local path / same-origin referrer."""

    @pytest.mark.parametrize(
        "malicious_next",
        [
            "https://evil.example/phish",
            "//evil.example/phish",
            "/\\evil.example/phish",
            "javascript:alert(1)",
            "//[",  # malformed: urlparse() raises ValueError("Invalid IPv6 URL")
        ],
    )
    def test_login_post_rejects_external_next(self, token_login_client, malicious_next):
        """A crafted `next` on the login POST must never redirect off-site."""
        resp = token_login_client.post(
            "/login",
            data={"token": "unit-test-admin-token", "next": malicious_next},
        )
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert location == "/lands", (
            f"expected fallback to /lands for next={malicious_next!r}, "
            f"got redirected to {location!r}"
        )

    def test_login_post_honors_safe_relative_next(self, token_login_client):
        """A legitimate same-site `next` still works after the fix."""
        resp = token_login_client.post(
            "/login",
            data={"token": "unit-test-admin-token", "next": "/settings/properties"},
        )
        assert resp.status_code == 302
        assert resp.headers.get("Location") == "/settings/properties"

    def test_login_get_already_authenticated_rejects_external_next(
        self, token_login_client
    ):
        """The already-logged-in GET branch must apply the same guard."""
        login_resp = token_login_client.post(
            "/login", data={"token": "unit-test-admin-token"}
        )
        assert login_resp.status_code == 302

        resp = token_login_client.get("/login?next=https://evil.example/phish")
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "evil.example" not in location
        assert location == "/lands"

    def test_login_get_already_authenticated_rejects_malformed_next(
        self, token_login_client
    ):
        """QA regression: `next=//[` must fail closed to `/lands`, not 500.

        `urlparse("//[")` raises `ValueError: Invalid IPv6 URL`; the guard
        must catch that and fall back rather than letting it bubble up as a
        server error for already-authenticated users clicking a crafted link.
        """
        login_resp = token_login_client.post(
            "/login", data={"token": "unit-test-admin-token"}
        )
        assert login_resp.status_code == 302

        resp = token_login_client.get("/login?next=%2F%2F%5B")
        assert resp.status_code == 302
        assert resp.headers.get("Location") == "/lands"

    def _make_property(self, token_login_app, source_email_id):
        with token_login_app.app_context():
            prop = Property(
                source_email_id=source_email_id,
                title="Redirect Guard Property",
                municipality="Valencia",
                property_category="housing",
                property_subtype="apartment",
                price=Decimal("100000.00"),
                area=Decimal("80.00"),
            )
            db.session.add(prop)
            db.session.commit()
            return prop.id

    def test_referer_redirect_rejects_cross_origin(
        self, token_login_app, token_login_client
    ):
        """A cross-origin Referer on an admin POST action must not be honored."""
        prop_id = self._make_property(token_login_app, "redirect_guard_cross_origin_1")

        login_resp = token_login_client.post(
            "/login", data={"token": "unit-test-admin-token"}
        )
        assert login_resp.status_code == 302

        resp = token_login_client.post(
            f"/properties/{prop_id}/set-status",
            data={"status": "removed"},
            headers={"Referer": "https://evil.example/steal-session"},
        )
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "evil.example" not in location
        assert location == f"/properties/{prop_id}"

    def test_referer_redirect_honors_same_origin(
        self, token_login_app, token_login_client
    ):
        """A genuine same-origin Referer is still honored after the fix."""
        prop_id = self._make_property(token_login_app, "redirect_guard_same_origin_1")

        login_resp = token_login_client.post(
            "/login", data={"token": "unit-test-admin-token"}
        )
        assert login_resp.status_code == 302

        referer = f"http://localhost/properties/{prop_id}?tab=notes"
        resp = token_login_client.post(
            f"/properties/{prop_id}/set-status",
            data={"status": "removed"},
            headers={"Referer": referer},
        )
        assert resp.status_code == 302
        assert resp.headers.get("Location") == referer


# ---------------------------------------------------------------------------
# Security: error messages don't leak internals
# ---------------------------------------------------------------------------
class TestErrorSanitization:
    """API error responses must not expose internal exception messages."""

    def test_internal_error_no_leak(self, client):
        """Simulated internal error should return generic message."""
        with patch(
            "services.scheduler_service.get_scheduler_status",
            side_effect=Exception("secret DB creds"),
        ):
            resp = client.get("/api/scheduler/status")
            assert resp.status_code == 500
            data = json.loads(resp.data)
            assert "secret" not in data.get("error", "")
            assert "creds" not in data.get("error", "")


# ---------------------------------------------------------------------------
# Scoring: Config profile weights drive calculation, DB combined weights loaded
# ---------------------------------------------------------------------------
class TestScoringWeights:
    """Scoring uses Config.SCORING_PROFILES for profile weights and
    loads legacy combined weights from DB into ScoringService.weights."""

    def test_score_uses_config_investment_profile(self, app, test_land):
        """Investment score must be driven by Config.SCORING_PROFILES['investment']."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            # Score with Config defaults
            svc.calculate_score(land)
            db.session.commit()
            default_investment = float(land.score_investment)

            # Patch Config.SCORING_PROFILES to heavily favor environment
            alt_profiles = {
                "investment": {
                    "environment": 0.90,
                    "infrastructure_basic": 0.02,
                    "transport": 0.02,
                    "legal_status": 0.02,
                    "investment_yield": 0.02,
                    "location_quality": 0.02,
                },
                "lifestyle": Config.SCORING_PROFILES.get("lifestyle", {}),
            }
            with patch.object(Config, "SCORING_PROFILES", alt_profiles):
                svc.calculate_score(land)
                db.session.commit()
                new_investment = float(land.score_investment)

            assert new_investment != default_investment, (
                f"Investment score did not change with altered Config profiles: "
                f"default={default_investment}, new={new_investment}"
            )

    def test_score_uses_config_lifestyle_profile(self, app, test_land):
        """Lifestyle score must be driven by Config.SCORING_PROFILES['lifestyle']."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            svc.calculate_score(land)
            db.session.commit()
            default_lifestyle = float(land.score_lifestyle)

            alt_profiles = {
                "investment": Config.SCORING_PROFILES.get("investment", {}),
                "lifestyle": {
                    "investment_yield": 0.90,
                    "environment": 0.02,
                    "services_quality": 0.02,
                    "transport": 0.02,
                    "infrastructure_basic": 0.02,
                    "location_quality": 0.02,
                },
            }
            with patch.object(Config, "SCORING_PROFILES", alt_profiles):
                svc.calculate_score(land)
                db.session.commit()
                new_lifestyle = float(land.score_lifestyle)

            assert new_lifestyle != default_lifestyle, (
                f"Lifestyle score did not change with altered Config profiles: "
                f"default={default_lifestyle}, new={new_lifestyle}"
            )

    def test_combined_mix_affects_total_score(self, app, test_land):
        """Changing Config.COMBINED_MIX ratio must change score_total."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            svc.calculate_score(land)
            db.session.commit()
            default_total = float(land.score_total)
            inv = float(land.score_investment)
            life = float(land.score_lifestyle)

            if abs(inv - life) < 0.01:
                pytest.skip(
                    "Investment and lifestyle scores identical; cannot test mix effect"
                )

            # Patch COMBINED_MIX to 100% investment
            with patch.object(
                Config, "COMBINED_MIX", {"investment": 1.0, "lifestyle": 0.0}
            ):
                svc.calculate_score(land)
                db.session.commit()
                inv_only_total = float(land.score_total)

            assert abs(inv_only_total - default_total) > 0.01, (
                "score_total did not change with 100% investment mix"
            )

    def test_db_combined_weights_loaded_on_init(self, app):
        """ScoringService.__init__ must load combined weights from DB."""
        with app.app_context():
            # Seed DB combined weights
            for name, weight in [("environment", 0.80), ("transport", 0.20)]:
                db.session.add(
                    ScoringCriteria(
                        criteria_name=name,
                        profile="combined",
                        weight=Decimal(str(weight)),
                        active=True,
                    )
                )
            db.session.commit()

            svc = ScoringService()
            # After normalization, these should be in self.weights
            assert "environment" in svc.weights
            assert "transport" in svc.weights

    def test_profile_weights_fallback_to_config(self, app, test_land):
        """When no DB weights exist, scoring falls back to Config defaults."""
        with app.app_context():
            svc = ScoringService()
            land = db.session.get(Land, test_land)

            svc.calculate_score(land)
            db.session.commit()

            assert float(land.score_total) > 0
            assert float(land.score_investment) > 0
            assert float(land.score_lifestyle) > 0
