# Project Status

## Description
Idealista Land Tracker AI — automated ingestion, enrichment, scoring, and tracking of Spanish land listings from Idealista via IMAP email parsing. Flask web application with PostgreSQL (Docker), dual investment/lifestyle MCDM scoring, Google Maps/Places enrichment, AI analysis (Claude/OpenAI).

## Production Readiness Plan

### DONE

- [x] **Phase A (P0)** — Security & Auth
  - SEC-01: Session + token auth, @admin_required fail-closed
  - SEC-02: Flask-WTF CSRF (forms protected, API exempted)
  - SEC-03: Login/logout, session management
  - SEC-04: Flask-Limiter (Redis backend, per-endpoint limits)
  - SEC-05: Error sanitization (no str(e) leaks to users)
  - COR-01: Scoring reads DB weights (not hardcoded Config)
  - TEST-01: 9 tests (auth, CSRF, scoring DB weights)

- [x] **Phase B (P1 core)** — Correctness & Reliability
  - COR-02/03: Defensive price parsing (float coercion, type safety)
  - COR-04: Score clamping [0, 100] at aggregation
  - COR-05: Pagination boundary validation
  - REL-01: HTTP retry with exponential backoff (utils/http_retry.py)
  - REL-02/05: Per-email transaction isolation (savepoints)
  - REL-03: Scheduler lock file handle leak fix
  - REL-04: Distance Matrix element validation
  - TEST-02: 21 tests (retry, clamping, pagination, lock, distance matrix, price parsing)

- [x] **Phase C (P1 ops/data)** — Data Integrity & Operations
  - DATA-01: Timezone-aware datetimes (datetime.utcnow() eliminated)
  - DATA-02: CHECK constraints (price, area, coords, scores, travel times, listing_status)
  - OPS-01: Config validation at startup (fail-fast)
  - OPS-02: Enhanced /healthz (DB ping, scheduler status, 503 on failure)
  - TEST-03: 19 tests (datetimes, CHECK constraints, config validation, healthz)

- [x] **Phase D (Stabilization)** — Code Quality
  - STAB-01: SQLAlchemy 2.0 migration (.query.get() -> db.session.get())
  - STAB-02: exc_info=True on all logger.error() in except blocks (~50 calls fixed)
  - STAB-03: Unused import removed (anthropic_service.py)
  - Full test suite: 150 passed, 0 failed

### Backlog (not blocking production)
- [ ] Convert ~196 f-string logger calls to %-formatting (CPU efficiency)
- [ ] Flask-Migrate (Alembic) for migration management
- [ ] JSON schema validation for model JSON fields
- [ ] Enrichment retry queue (track failed enrichments)
- [ ] Docker secrets instead of env vars

## Test Suite
- **150 tests** across 9 test files
- test_api_routes.py (25), test_email_parser.py (4), test_enrichment_service.py (18)
- test_imap_service_*.py (5), test_market_analysis_service.py (11), test_models.py (15)
- test_scoring_service.py (12), test_security_and_scoring.py (9)
- test_phase_b_reliability.py (21), test_phase_c_ops_data.py (19)

## Notes
- SQL migration 007_add_check_constraints.sql is not idempotent (apply once)
- Port: 5001, DB port: 5433 (Docker)
- LegacyAPIWarning in test files only (production code migrated)
