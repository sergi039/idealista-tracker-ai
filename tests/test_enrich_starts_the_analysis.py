"""One Enrich press produces an analysis, with or without the tab (#434).

The defect this pins, measured on production 2026-08-20 against property 793:
the owner pressed **Enrich** four times and **not one** `property_ai_analysis`
row was ever created. The AI step existed only as a link in a browser promise
chain -- `templates/property_detail.html::runEnrichAndAnalyze` awaited the
enrichment poller and only then POSTed `/analyze/structured` -- so every
reload, every impatient re-press and every closed tab discarded it. The server
never knew there was a sequel.

What is asserted here is therefore not "the analysis works" (other suites own
that) but the four properties that make the press keep its promise:

* the enrichment job starts the analyses itself;
* it starts them only when they can succeed, rather than queueing jobs that
  are certain to fail;
* it starts them *after* its own commit, so they read what it wrote;
* and a page that also asks joins those jobs instead of paying twice.
"""

import json

import pytest

from app import create_app, db
from config import Config
from models import BackgroundJob, Property, PropertyAiAnalysisVariant
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def prop(app):
    with app.app_context():
        row = Property(
            source_email_id="enrich_chain_1",
            title="Plot in Xivares",
            municipality="Carreño",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


@pytest.fixture
def bridge(monkeypatch):
    """The subscription bridge configured, as it is on the deployment.

    `monkeypatch` and not a bare assignment: `tests/conftest.py` fails the
    session when a test leaves `Config` mutated.
    """
    monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "test-bridge-token")


def _analysis_ok(model="test-model"):
    def _fake(self, prop_local, provider="claude"):
        return {
            "status": "success",
            "model": f"{model}-{provider}",
            "structured_analysis": {"verdict": provider},
        }

    return _fake


def _ai_jobs():
    return (
        BackgroundJob.query.filter_by(job_type="property_ai_analysis")
        .order_by(BackgroundJob.created_at)
        .all()
    )


def _enrich_returns(value=True):
    def _fake(self, prop_local, refresh_coords=False, recalc_scoring=True):
        return value

    return _fake


def test_enrich_starts_both_analyses(app, client, prop, bridge, monkeypatch):
    """The press starts them; nothing in a browser is involved."""
    monkeypatch.setattr(
        "services.property_enrichment_service.PropertyEnrichmentService."
        "enrich_property",
        _enrich_returns(True),
    )
    monkeypatch.setattr(
        "services.property_ai_service.PropertyAIService.analyze_property_structured",
        _analysis_ok(),
    )

    with app.app_context():
        resp = client.post(f"/api/property/{prop}/enrich", json={})
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["success"] is True

        started = body["analysis_jobs"]
        assert set(started) == {"claude", "openai"}, started

        jobs = _ai_jobs()
        assert len(jobs) == 2
        assert {j.meta["provider"] for j in jobs} == {"claude", "openai"}
        assert {j.dedupe_key for j in jobs} == {
            f"property_ai_analysis:{prop}:claude",
            f"property_ai_analysis:{prop}:openai",
        }

        # And the analyses really ran: both variants are stored, and Claude's
        # is mirrored onto the property the way the endpoint does it.
        variants = PropertyAiAnalysisVariant.query.filter_by(property_id=prop).all()
        assert {v.provider for v in variants} == {"claude", "openai"}
        assert db.session.get(Property, prop).ai_analysis == {"verdict": "claude"}


def test_without_the_bridge_it_starts_nothing(app, client, prop, monkeypatch):
    """No token means no jobs -- not two jobs that can only fail.

    `AI_BRIDGE_TOKEN` gates *both* providers (`services/anthropic_service.py`
    and `services/openai_service.py` each refuse without it), so queueing them
    anyway would put two failures on the page dressed as pending work.
    """
    monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", None)
    monkeypatch.setattr(
        "services.property_enrichment_service.PropertyEnrichmentService."
        "enrich_property",
        _enrich_returns(True),
    )

    with app.app_context():
        resp = client.post(f"/api/property/{prop}/enrich", json={})
        assert resp.status_code == 200
        assert json.loads(resp.data)["analysis_jobs"] == {}
        assert _ai_jobs() == []


def test_the_analysis_reads_what_the_enrichment_wrote(
    app, client, prop, bridge, monkeypatch
):
    """Ordering, not timing: the chain starts after `enrich_property` commits.

    Starting the analysis any earlier analyses the previous state of the row,
    which is worse than not analysing it -- the numbers would be stale and
    nothing on the page would say so.
    """
    written = "enriched-title"

    def _enrich(self, prop_local, refresh_coords=False, recalc_scoring=True):
        prop_local.title = written
        db.session.commit()
        return True

    seen = {}

    def _analysis(self, prop_local, provider="claude"):
        seen[provider] = prop_local.title
        return {
            "status": "success",
            "model": "m",
            "structured_analysis": {"verdict": provider},
        }

    monkeypatch.setattr(
        "services.property_enrichment_service.PropertyEnrichmentService."
        "enrich_property",
        _enrich,
    )
    monkeypatch.setattr(
        "services.property_ai_service.PropertyAIService.analyze_property_structured",
        _analysis,
    )

    with app.app_context():
        resp = client.post(f"/api/property/{prop}/enrich", json={})
        assert resp.status_code == 200

    assert seen == {"claude": written, "openai": written}


def test_a_failing_analysis_does_not_fail_the_enrichment(
    app, client, prop, bridge, monkeypatch
):
    """The measurement is the run's job; the analysis is its sequel.

    An enrichment reported as failed because its optional sequel raised is the
    #153 mistake -- a run's verdict must describe what the run was asked for.
    """
    monkeypatch.setattr(
        "services.property_enrichment_service.PropertyEnrichmentService."
        "enrich_property",
        _enrich_returns(True),
    )

    def _boom(self, prop_local, provider="claude"):
        raise RuntimeError("the bridge is down")

    monkeypatch.setattr(
        "services.property_ai_service.PropertyAIService.analyze_property_structured",
        _boom,
    )

    with app.app_context():
        resp = client.post(f"/api/property/{prop}/enrich", json={})
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["success"] is True
        # The jobs were started and failed on their own; the enrichment's own
        # verdict is untouched by that.
        assert [j.status for j in _ai_jobs()] == ["error", "error"]


def test_a_failed_enrichment_starts_nothing(app, client, prop, bridge, monkeypatch):
    """Nothing to analyse: `enrich_property` returning False means the
    coordinate could not be established, so the numbers an analysis would
    read were never written."""
    monkeypatch.setattr(
        "services.property_enrichment_service.PropertyEnrichmentService."
        "enrich_property",
        _enrich_returns(False),
    )

    with app.app_context():
        resp = client.post(f"/api/property/{prop}/enrich", json={})
        assert resp.status_code == 200
        assert json.loads(resp.data)["success"] is False
        assert _ai_jobs() == []


def test_the_chain_claims_the_key_the_page_claims(
    app, client, prop, bridge, monkeypatch
):
    """Both writers of one (property, provider) analysis claim one key.

    This is what makes the server-side chain safe to add without touching the
    template: the page goes on POSTing for itself, and while the chain's job
    is still live the dedupe key answers that POST with *this* job instead of
    a second subscription call. The mechanism is the shared key, so the key is
    what is asserted -- the timing that exercises it (an async job still
    running when the page asks) cannot be staged here, because under `TESTING`
    every job runs inline and is finished before the page could ask.
    """
    from routes.api_routes import _property_ai_dedupe_key

    monkeypatch.setattr(
        "services.property_enrichment_service.PropertyEnrichmentService."
        "enrich_property",
        _enrich_returns(True),
    )
    monkeypatch.setattr(
        "services.property_ai_service.PropertyAIService.analyze_property_structured",
        _analysis_ok(),
    )

    with app.app_context():
        client.post(f"/api/property/{prop}/enrich", json={})
        from_chain = {j.meta["provider"]: j.dedupe_key for j in _ai_jobs()}

        # The endpoint, asked directly, claims that same key -- not a second
        # spelling of it.
        client.post(
            f"/api/property/{prop}/analyze/structured", json={"provider": "claude"}
        )
        from_page = {j.dedupe_key for j in _ai_jobs() if j.meta["provider"] == "claude"}

    assert from_chain["claude"] == _property_ai_dedupe_key(prop, "claude")
    assert from_chain["openai"] == _property_ai_dedupe_key(prop, "openai")
    assert from_page == {_property_ai_dedupe_key(prop, "claude")}


def test_a_live_analysis_is_joined_not_restarted(app, prop, bridge, monkeypatch):
    """`JobAlreadyActive` is an outcome, not an error.

    In production the enrichment job is async and still running when the page
    POSTs, so one of the two loses the race for the key. Losing it must return
    the winner's job id -- the page then polls the analysis that is already
    being paid for. Swallowing it and reporting nothing would send the page
    off to start a third.
    """
    from routes.api_routes import _start_property_ai
    from services.background_jobs import JobAlreadyActive

    def _already(job_fn, **kwargs):
        raise JobAlreadyActive("live-job-id")

    monkeypatch.setattr("routes.api_routes._run_sync", _already)

    with app.app_context():
        started = _start_property_ai(prop)

    assert started == {"claude": "live-job-id", "openai": "live-job-id"}


def test_the_analysis_closure_needs_no_request(app, prop, bridge, monkeypatch):
    """The regression guard for the extraction itself.

    The closure used to be defined inside the request handler and closed over
    `request`-scoped values. A background worker has no request, so anything
    that reaches for one there raises outside a test client -- which is why
    this runs it with no request context at all.
    """
    from routes.api_routes import _property_ai_job

    monkeypatch.setattr(
        "services.property_ai_service.PropertyAIService.analyze_property_structured",
        _analysis_ok(),
    )

    with app.app_context():
        result = _property_ai_job(prop, "openai")()

    assert result["success"] is True
    assert result["provider"] == "openai"
    assert result["analysis"] == {"verdict": "openai"}
