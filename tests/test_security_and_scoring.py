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
def non_testing_app():
    """App fixture with TESTING=False, i.e. the app as it actually runs.

    The admin login was removed on 2026-08-08 (owner decision: the app is a
    single-owner tool bound to 127.0.0.1), so this fixture no longer differs
    from `app` in access terms -- it exists to exercise the non-TESTING code
    path, where the old auth gate used to live.

    DATABASE_URL is overridden *before* create_app(), unlike the `app` fixture
    above which sets SQLALCHEMY_DATABASE_URI after db.init_app() already bound
    the engine to the suite-wide sqlite file. Tests here render heavy pages
    (land detail, map, CSV export) instead of bouncing off a 401, and that
    shared file is contended enough late in a run to fail drop_all() with
    'database is locked'. A private in-memory DB avoids it."""
    setup_test_environment()
    orig_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    try:
        app = create_app()
        app.config["TESTING"] = False
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
def non_testing_client(non_testing_app):
    return non_testing_app.test_client()


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
def non_testing_test_land(non_testing_app):
    """Create a land in the non-TESTING app context."""
    with non_testing_app.app_context():
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
def non_testing_test_property(non_testing_app):
    """Create a property in the non-TESTING app context."""
    with non_testing_app.app_context():
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
# Access: no login gate (owner decision, 2026-08-08)
# ---------------------------------------------------------------------------
class TestNoLoginGate:
    """The admin login was removed: this is a single-owner tool published only
    on 127.0.0.1 (see docker-compose.yml), and the owner wanted direct access.
    These tests pin that decision so the gate does not silently come back --
    and so anyone reintroducing it does it deliberately, not by accident.

    The paired risk is recorded in README: with no token gate, the JSON API is
    reachable by anything that can talk to localhost, and api_bp is CSRF-exempt."""

    def test_login_route_is_gone(self, non_testing_client):
        """/login and /logout must not exist as routes any more."""
        for path in ("/login", "/logout"):
            resp = non_testing_client.get(path)
            assert resp.status_code == 404, (
                f"{path} returned {resp.status_code}, expected 404 (route removed)"
            )

    def test_favorite_toggle_is_allowed(
        self, non_testing_client, non_testing_test_land, non_testing_test_property
    ):
        """Favorites are user-facing actions and must keep working."""
        lid = non_testing_test_land
        pid = non_testing_test_property

        land_resp = non_testing_client.post(f"/api/land/{lid}/favorite")
        assert land_resp.status_code == 200
        land_data = json.loads(land_resp.data)
        assert land_data["success"] is True
        assert land_data["is_favorite"] is True

        property_resp = non_testing_client.post(f"/api/property/{pid}/favorite")
        assert property_resp.status_code == 200
        property_data = json.loads(property_resp.data)
        assert property_data["success"] is True
        assert property_data["is_favorite"] is True

    def test_health_check_accessible(self, non_testing_client):
        """The health check must stay public (used by Docker/monitoring)."""
        resp = non_testing_client.get("/api/healthz")
        assert resp.status_code == 200


class TestPropertyDataReadableWithoutLogin:
    """Counterpart of the removed issue-#30 gate: these endpoints used to
    answer 401 (or redirect to /login) without a token. After the owner
    removed the login on 2026-08-08 they must serve data directly, with no
    redirect to a login page that no longer exists."""

    OPEN_API_GET_ENDPOINTS = [
        "/api/lands",
        "/api/stats",
    ]

    def test_api_endpoints_serve_data(self, non_testing_client):
        """The JSON list/stats endpoints answer without any credential."""
        for endpoint in self.OPEN_API_GET_ENDPOINTS:
            resp = non_testing_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200"
            )
            data = json.loads(resp.data)
            assert data["success"] is True

    def test_land_scoped_endpoints_serve_data(
        self, non_testing_client, non_testing_test_land
    ):
        """Per-land data endpoints answer without any credential."""
        lid = non_testing_test_land
        endpoints = [
            f"/api/lands/{lid}",
            f"/api/land/{lid}/history",
            f"/api/analysis/compare/{lid}",
        ]
        # /api/description/variants/<id> is left out on purpose: it builds
        # DescriptionService and needs a configured AI key, which is unrelated
        # to the access question under test here.
        for endpoint in endpoints:
            resp = non_testing_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200"
            )

    def test_property_scoped_endpoints_serve_data(
        self, non_testing_client, non_testing_test_property
    ):
        """Per-property data endpoints answer without any credential."""
        pid = non_testing_test_property
        endpoints = [
            f"/api/properties/{pid}",
            f"/api/property/{pid}/analysis/compare",
        ]
        for endpoint in endpoints:
            resp = non_testing_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200"
            )

    def test_properties_bulk_dump_serves_data(self, non_testing_client):
        """/api/properties, including the ?full=1 bulk dump, is open."""
        resp = non_testing_client.get("/api/properties")
        assert resp.status_code == 200
        resp_full = non_testing_client.get("/api/properties?full=1")
        assert resp_full.status_code == 200

    def test_pages_render_without_login(
        self, non_testing_client, non_testing_test_land, non_testing_test_property
    ):
        """HTML pages render the data instead of redirecting to /login."""
        lid = non_testing_test_land
        pid = non_testing_test_property
        pages = [
            "/properties",
            f"/properties/{pid}",
            "/map",
            "/criteria",
            f"/lands/{lid}",
        ]
        for page in pages:
            resp = non_testing_client.get(page)
            assert resp.status_code == 200, (
                f"{page} returned {resp.status_code}, expected 200"
            )

    def test_csv_export_serves_the_file(self, non_testing_client):
        """The bulk CSV exports download directly."""
        for endpoint in ["/export.csv", "/properties/export.csv"]:
            resp = non_testing_client.get(endpoint)
            assert resp.status_code == 200, (
                f"{endpoint} returned {resp.status_code}, expected 200"
            )
            assert "text/csv" in resp.headers.get("Content-Type", "")


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

    def test_archive_route_no_longer_exists(self, non_testing_client, client):
        """The dead /api/download/project endpoint must not come back as a
        live route, in either app configuration."""
        for c in (non_testing_client, client):
            resp = c.get("/api/download/project")
            assert resp.status_code == 404, (
                f"/api/download/project returned {resp.status_code}, expected 404 (route removed)"
            )

    def test_archive_files_not_served_from_static(self, non_testing_client):
        """None of the source-archive files must be reachable under /static/,
        which Flask serves directly regardless of any route decorator."""
        for path in self.ARCHIVE_STATIC_PATHS:
            resp = non_testing_client.get(path)
            assert resp.status_code == 404, (
                f"{path} returned {resp.status_code}, expected 404 (file must not exist in static/)"
            )

    def test_archive_files_not_served_from_static_testing_app(self, client):
        """Same check on the TESTING app: the files must be gone from the
        repository, not merely gated behind a route."""
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
class TestPropertySettingsNoTokenCookie:
    """Issue #20: the "Unlock" widget on /settings/properties wrote the
    master ADMIN_API_TOKEN into a non-HttpOnly, JS-readable `admin_token`
    cookie via `document.cookie`, and nothing server-side ever read that
    cookie back -- so the widget didn't even authenticate the user.

    The login itself is gone since 2026-08-08, but the widget must not come
    back: writing a credential into a JS-readable cookie would be wrong again
    the moment any authentication is reintroduced."""

    def test_settings_page_has_no_token_widget(self, non_testing_client):
        """The dead cookie-writing widget and its JS must be gone entirely."""
        resp = non_testing_client.get("/settings/properties")
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

    def test_settings_page_response_sets_no_admin_token_cookie(
        self, non_testing_client
    ):
        """The server itself must never set an admin_token cookie either."""
        resp = non_testing_client.get("/settings/properties")
        assert resp.status_code == 200
        set_cookie_headers = resp.headers.get_all("Set-Cookie")
        assert not any("admin_token" in h for h in set_cookie_headers)

    def test_settings_page_saves_without_any_credential(self, non_testing_client):
        """The settings form posts through and takes effect with no token."""
        resp = non_testing_client.post(
            "/settings/properties",
            data={"action": "save_ingestion_settings", "sale_only": "on"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Admin authentication is required" not in body
        assert "Unauthorized" not in body


# ---------------------------------------------------------------------------
# Security: Referer-based open redirects (issue #17)
# ---------------------------------------------------------------------------
class TestOpenRedirectGuard:
    """Issue #17: several POST handlers redirect back to "where you came
    from" using the Referer header, which is fully client-controlled. A
    cross-origin form post could set it to an attacker's origin and bounce
    the browser there right after the action completed. The handler must
    fall back to a safe same-site default whenever the referrer is not
    same-origin.

    The sibling guard on the login page's `next` parameter went away with the
    login itself (2026-08-08); `safe_referrer_redirect` moved to
    utils/redirects.py and still applies to every POST handler here."""

    def _make_property(self, non_testing_app, source_email_id):
        with non_testing_app.app_context():
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
        self, non_testing_app, non_testing_client
    ):
        """A cross-origin Referer on a POST action must not be honored."""
        prop_id = self._make_property(non_testing_app, "redirect_guard_cross_origin_1")

        resp = non_testing_client.post(
            f"/properties/{prop_id}/set-status",
            data={"status": "removed"},
            headers={"Referer": "https://evil.example/steal-session"},
        )
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "evil.example" not in location
        assert location == f"/properties/{prop_id}"

    def test_referer_redirect_honors_same_origin(
        self, non_testing_app, non_testing_client
    ):
        """A genuine same-origin Referer is still honored after the fix."""
        prop_id = self._make_property(non_testing_app, "redirect_guard_same_origin_1")

        referer = f"http://localhost/properties/{prop_id}?tab=notes"
        resp = non_testing_client.post(
            f"/properties/{prop_id}/set-status",
            data={"status": "removed"},
            headers={"Referer": referer},
        )
        assert resp.status_code == 302
        assert resp.headers.get("Location") == referer


# ---------------------------------------------------------------------------
# Security: session cookie must be Secure/SameSite (issue #16)
# ---------------------------------------------------------------------------
class TestSessionCookieFlags:
    """Issue #16: SESSION_COOKIE_SECURE/SAMESITE were never set anywhere.
    The admin login is gone (#62), but the Flask session cookie is still
    used -- to store the Flask-WTF CSRF token, flash messages and the
    language preference -- so it still needs Secure/SameSite instead of
    relying solely on browsers' Lax-by-default heuristics and riding along
    over plain HTTP."""

    def test_session_cookie_secure_and_samesite_outside_dev_mode(self):
        setup_test_environment()
        os.environ.pop("DEV_MODE", None)
        try:
            app = create_app()
            assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
            assert app.config["SESSION_COOKIE_SECURE"] is True
        finally:
            os.environ.pop("DEV_MODE", None)

    def test_session_cookie_not_secure_under_dev_mode(self):
        """DEV_MODE serves plain HTTP with no TLS-terminating proxy, so a
        Secure cookie would never round-trip back to the server."""
        setup_test_environment()
        os.environ["DEV_MODE"] = "true"
        os.environ["AUTO_CREATE_DB"] = "false"
        os.environ["AUTO_START_SCHEDULER"] = "false"
        try:
            app = create_app()
            assert app.config["SESSION_COOKIE_SECURE"] is False
            assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        finally:
            os.environ.pop("DEV_MODE", None)
            os.environ.pop("AUTO_CREATE_DB", None)
            os.environ.pop("AUTO_START_SCHEDULER", None)


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
