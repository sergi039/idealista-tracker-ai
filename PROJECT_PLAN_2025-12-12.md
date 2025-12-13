# Project Plan — 2025-12-12

This file captures what’s next for the Idealista Land Watch & Rank project. We’ll start implementing these items tomorrow.

## Goals (next iteration)

- Make production setup safer (secrets, admin access, scheduler, DB exposure)
- Make long-running tasks resilient (AI/enrichment) and keep UI responsive
- Improve maintainability (split large files, reduce inline JS, simplify routes)
- Align docs/config and reduce tech debt (SQLAlchemy 2.x, i18n duplicates, lint)

## Queue (prioritized)

### P0 — Production & security

- Docker/Compose hardening
  - Remove dev bind-mount `.:/app` for production
  - Remove exposed DB port `5433:5432` for production
  - Disable `AUTO_CREATE_DB` / `AUTO_START_SCHEDULER` for production
- Admin endpoints
  - Remove DEV bypass for admin endpoints (require `ADMIN_API_TOKEN` always outside tests)
- External API calls
  - Move heavy operations (enrichment, AI analyses, listing checks) to background jobs
  - Add retries + exponential backoff + timeouts at the client layer

### P1 — Correctness & data integrity

- SQLAlchemy 2.x cleanup
  - Replace legacy `Query.get()` with `db.session.get()`
- i18n correctness
  - Remove duplicate keys in `utils/i18n.py` (silent overwrite risk)
- Docs/config alignment
  - Update README env var names (`GOOGLE_MAPS_API_KEY` / `GOOGLE_PLACES_API_KEY`, etc.)
- Performance for JSON filters
  - Consider DB indexes for commonly queried JSON paths (Postgres)

### P2 — Maintainability

- Split “mega files”
  - Break `routes/api_routes.py` into multiple modules/blueprints
  - Split `templates/land_detail.html` and `templates/lands.html` into partials/components
  - Reduce inline JS in templates; move to `static/js/` modules
- Rate limiting
  - Replace in-memory limiter with Redis-backed (multi-worker safe) if needed
- Caching
  - Cache data payloads instead of Flask Response objects

### P3 — Quality, docs, CI

- Add “Ops” documentation
  - Where `.env` lives, required/optional keys, how to run bulk scripts safely
- CI
  - Add GitHub Actions for `pytest` (+ optionally `ruff` progressively)

