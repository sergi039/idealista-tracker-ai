# Project Status — IdealistaRank Universal

## Description
Universal property tracker for Idealista sale listings across Spain. Replaces the legacy land-only tracker with multi-category support (housing, land, garage, commercial, building).

## Architecture
- **Branch**: `feature/properties-universal` (worktree of IdealistaRank)
- **Port**: 5050 (app), 5434 (DB dev)
- **DB**: `idealista_universal` (PostgreSQL 15)
- **Remote**: `origin` = `sergi039/idealista-tracker-ai.git`

## Kanban

### TODO
- [ ] Expand classification rules from more email samples (garage, commercial, building types)
- [ ] Collect real email samples per deferred property type
- [ ] Optional: migrate scoring from Config.SCORING_PROFILES to DB-driven weights

### IN PROGRESS
- [ ] CUTOVER-01/02: Prepare branch merge strategy and migration runbook

### DONE (Production Hardening — Feb 2026)
- [x] INTEG-01: Diff-map between Legacy and Universal
- [x] SEC-01: @admin_required on all 22 POST/PUT endpoints
- [x] SEC-02: Flask-WTF CSRF protection (exempted for JSON API)
- [x] SEC-03: 80+ str(e) leaks fixed across routes and services
- [x] SEC-04: Flask-Limiter replacing in-memory rate limiter
- [x] SEC-05: _validate_config() fail-fast + SecurityValidator
- [x] REL-01: Scheduler lock lifecycle fix (atexit before init, handle cleanup)
- [x] REL-02: Dead code removal validation
- [x] REL-03: Verified retry/backoff on all external HTTP calls
- [x] REL-04: Distance Matrix element validation (missing duration/distance)
- [x] DATA-01: CHECK constraints on Land + Property models
- [x] DATA-02: Timezone-aware datetime verified (all datetime.now(timezone.utc))
- [x] OPS-01: Enhanced /healthz with DB ping + scheduler status (503 on failure)
- [x] OPS-02: Verified docker-compose dev/prod separation
- [x] TEST-01: Ported Phase A-D test suites (61 new tests, 216 total)

## Test Status
- 216 tests passing, 0 failures
- Coverage: security, scoring, reliability, ops, data integrity, CHECK constraints

## Notes
- Universal uses `utils.http.request_with_retries` (not Legacy's `utils.http_retry`)
- Anthropic SDK handles its own retry; no wrapper needed
- Scoring profiles: Config-driven (not DB-driven like Legacy's `_load_combined_mix`)
- Lock file: `idealista_universal_scheduler.lock` (unique, not shared with Legacy)
