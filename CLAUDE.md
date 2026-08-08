# IdealistaRank — Idealista Watch & Analyze

Guidance for AI agents and contributors. Deeper contracts: CONTRIBUTING.md
(setup, style, PR checklist), docs/DEV_RULES.md (dual-build ports and
isolation), MIGRATION_RUNBOOK.md, docs/STATE.md.

## What this is

Self-hosted Flask app that ingests Idealista saved-search alert emails
(Gmail IMAP), stores listings in PostgreSQL, classifies and scores them
(investment / lifestyle / combined), enriches them via Google APIs
(Places, Distance Matrix), runs paid AI analysis (Anthropic Claude,
OpenAI), and tracks listing status by scraping idealista.com behind a
deliberate throttle. Personal tool, single owner, runs in local Docker.
English/Spanish UI.

## Stack

Python 3.11+ · Flask 3 + Flask-SQLAlchemy (SQLAlchemy 2) · PostgreSQL ·
gunicorn · APScheduler · Flask-Limiter (redis) · Flask-WTF (CSRF) ·
imapclient · anthropic SDK · Google API clients · uv (`uv.lock`) · pytest.

## Run

```bash
docker compose up -d --build                 # app on http://localhost:5001/
docker compose -f docker-compose.dev.yml up  # dev variant with --reload
pytest tests/ -v                             # test suite
pytest tests/ --cov=app --cov-report=html    # coverage report
```

### Local CI gate

`tools/ci/local_ci.sh` runs the same checks as `.github/workflows/ci.yml`
(ruff check, ruff format --check, no-source-bundles, `uv run pytest tests/
-q`) locally, standalone or as a git `pre-push` hook — a red run costs agent
cycles even though the repo's Actions minutes are free (issue #74). Enable it
once per clone:

```bash
tools/ci/install_hooks.sh    # git config core.hooksPath .githooks
```

Bypass a single push with `SKIP_LOCAL_CI=1 git push`. `.github/workflows/
ci.yml` stays the merge gate for autopilot; since issue #81 it runs the same
ruff commands, so the two really are in sync.

Ruff itself is a locked dev dependency (`uv.lock`), so `uv run ruff` is one
pinned version for CI, the hook and you. Rule selection is explicit in
`pyproject.toml` (`[tool.ruff.lint] select`) because ruff's *default* set is
not stable across releases — do not delete it expecting the default to be
equivalent.

**Working UI is `/lands`** (owner decision, 2026-08-07): the lands view —
cards/list with Land Type, Sea View and to-beach filters — is what the app
serves from `/`. The universal `/properties` page and its search-profile
model still exist and stay wired up, but they are an **archived view**
(linked in the navbar as "Universal (archive)"); do not build new work on
them without asking. Both read the same database: `lands` holds the 168
land rows, `properties` mirrors them under the "Legacy Lands" profile.

The dual-build isolation contract
from the transition — legacy on 5001 vs universal on 5050, unique Docker
names, separate IMAP labels and cookie names — lives in docs/DEV_RULES.md
and TODO.md; respect it if you ever run both side by side.

## Layout

- `app.py` — Flask application factory (validates required secrets at
  startup and fails fast); `main.py` — entrypoint
- `routes/` — `main_routes` (pages), `api_routes` (JSON API),
  `language_routes` (i18n switching)
- `services/` — business logic: email ingestion (`property_imap_service`,
  `imap_service`), classification (`property_classification_service`),
  scoring (`property_scoring_service`, `scoring_service`), enrichment
  (`property_enrichment_service`, `enrichment_service`), travel times
  (`travel_time_service`, `property_travel_service`), listing status
  scraping with throttle (`listing_status_service`), market analysis, AI
  (`anthropic_service`, `openai_service`, `property_ai_service`),
  `scheduler_service`, `background_jobs`
- `models.py` — SQLAlchemy models; `migrations/` — schema migrations
- `utils/` — `auth` (admin_required, rate limits), `security`, `cache`,
  `email_parser`, `idealista_extractors`, bulk tools
  (`bulk_ai_analysis`, `recalc_travel_times`)
- `templates/`, `static/` — Jinja2, Bootstrap, minimal vanilla JS/HTMX
- `tests/` — pytest suite; external APIs are mocked
- `docs/` — DEV_RULES.md, STATE.md, UNIVERSAL_PROPERTIES.md,
  PROPERTY_TYPES.md

## Hard rules

- Never read or echo `.env` — it holds IMAP and API credentials. Required
  config is validated at startup and fails fast; do not add silent
  fallbacks around it.
- External APIs cost real money (Anthropic, OpenAI, Google Places /
  Distance Matrix). Never run bulk backfills (`utils/bulk_ai_analysis.py`,
  `utils/recalc_travel_times.py`) without an explicit ticket saying so.
- Preserve the scraping throttle in `services/listing_status_service.py`
  (randomized sleeps between listing fetches). No bulk re-scrape loops.
- **There is no authentication** (owner decision, 2026-08-08): the admin
  login, `ADMIN_API_TOKEN` and `utils/auth.py` were removed, so every page
  and endpoint is open. This holds only because `docker-compose.yml`
  publishes the app on `127.0.0.1:5001` — never widen that binding, and
  never expose the app through a tunnel or reverse proxy without putting
  authentication back first.
- The rest of the security posture is hard-won and still applies: CSRF
  protection on form POSTs, rate limiting, parameterized queries only.
  Treat any weakening as a defect. Note that the JSON API blueprints are
  CSRF-exempt (`app.py`) because they used to be token-gated — with the
  token gone, a browser page can reach them; do not add new state-changing
  JSON endpoints without thinking about that. An open security/bug audit
  lives in GitHub issues #16–#30.
- Never change existing primary key types. Schema changes go through
  migrations (see MIGRATION_RUNBOOK.md).
- Mock external API calls in tests. Suites needing live services or
  credentials are reported as skipped, never as passed.

## Workflow

- GitHub issues are the source of truth for tasks:
  https://github.com/sergi039/idealista-tracker-ai/issues. An AgentsRoom
  board card, when used, is only a launch wrapper titled `#NN: …`.
- Branch from `main` (agent branches use `claude/**` or `codex/**`
  prefixes), merge back via PR. `main` is protected — no force-push, no
  deletion, changes only through a PR, admins included — so a direct
  push is rejected.
- CI exists (`.github/workflows/ci.yml`; issue #31 closed 2026-08-07):
  it runs on every PR and on push to `main`, with actions pinned by SHA.
  Three jobs — `pytest` does `uv sync --frozen` + `uv run pytest tests/ -v`
  on Python 3.11; `no-source-bundles` fails when an archive or source dump
  is tracked (issue #29); `ruff` runs `ruff check .` and `ruff format
  --check .` on the same uv setup (issue #81). `pytest` and
  `no-source-bundles` are *required* status checks on `main` (strict: the
  branch must be up to date); `ruff` is not required unless the owner adds
  the context to branch protection, so read the run before merging.
- Run `uv run pytest tests/ -q` locally and paste the real output before
  claiming done. That is a standing owner requirement in its own right,
  not a stand-in for CI.
- A local PostToolUse hook auto-runs `ruff check --fix` and `ruff format`
  on edited Python files — do not fight it. It calls whatever `ruff` is on
  PATH, which is not necessarily the version in `uv.lock`; the CI job and
  the pre-push gate use the locked one, so `uv run ruff` is the verdict
  that counts.
- `tools/autopilot/` runs the loop unattended: issue → PR → CI →
  independent review → squash-merge, with a LaunchAgent that redeploys
  main and rolls back on a red `/api/healthz`. A PR merges only on green
  CI *and* a reviewer PASS; `UNAVAILABLE` is not a pass. One agent per
  issue — see tools/autopilot/README.md before pointing a second one at
  an issue that already has a branch.
