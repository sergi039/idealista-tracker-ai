# Migration Runbook: Legacy → Universal

## Overview
Merge `feature/properties-universal` into `main` and promote Universal as the primary deployment.

## Pre-requisites

- [ ] All 216 tests passing (`pytest tests/ -q`)
- [ ] Production hardening tasks complete (SEC-01..05, REL-01..04, DATA-01..02, OPS-01..02)
- [ ] Docker images build cleanly (`docker compose build`)
- [ ] `.env` has all required vars: `DATABASE_URL`, `SESSION_SECRET`

## Step 1: Backup Legacy

```bash
# 1a. Backup Legacy database
docker exec idealista-db pg_dump -U idealista -d idealista > ~/backups/idealista_legacy_$(date +%Y%m%d).sql

# 1b. Tag Legacy state
cd /Users/ss/IdealistaRank
git tag legacy-final-$(date +%Y%m%d)
git push origin legacy-final-$(date +%Y%m%d)
```

## Step 2: Merge Universal into Main

```bash
cd /Users/ss/IdealistaRank

# 2a. Ensure main is clean
git checkout main
git pull origin main

# 2b. Merge the feature branch
git merge feature/properties-universal --no-ff -m "Merge Universal property tracker"

# 2c. Run tests on merged code
cd /Users/ss/IdealistaRank
.venv/bin/python -m pytest tests/ -q

# 2d. Push
git push origin main
```

## Step 3: Update Docker (Production)

```bash
# 3a. Stop Legacy containers
docker compose -f /Users/ss/IdealistaRank/docker-compose.yml down

# 3b. Rebuild with Universal code
docker compose -f /Users/ss/IdealistaRank/docker-compose.yml build --no-cache

# 3c. Start with production compose
docker compose -f /Users/ss/IdealistaRank/docker-compose.yml up -d

# 3d. Verify health
curl -s http://localhost:5001/api/healthz | python3 -m json.tool
# Expected: {"ok": true, "checks": {"database": "ok", "scheduler": "..."}}
```

## Step 4: Database Migration

Universal uses the same DB tables as Legacy (lands, scoring_criteria, etc.) plus new tables (properties, search_profiles, property_ai_analysis_variants, app_settings).

```bash
# 4a. New tables are auto-created by AUTO_CREATE_DB=true (dev) or need manual migration in prod

# 4b. Create new tables (db.create_all won't add CHECK constraints to existing tables)
docker exec idealista-app python -c "
from app import create_app, db
app = create_app()
with app.app_context():
    db.create_all()
    print('Tables created/verified')
"

# 4c. Apply CHECK constraints to existing tables (idempotent)
docker exec idealista-db psql -U idealista -d idealista -f /app/migrations/011_add_check_constraints.sql

# 4d. Verify new tables exist
docker exec idealista-db psql -U idealista -d idealista -c "\dt"
# Should show: properties, search_profiles, property_ai_analysis_variants, app_settings, market_settings
```

## Step 5: Port Mapping Update

After merge, Universal runs on Legacy's port (5001). Update CLAUDE.md port table if needed.

| Before | After |
|--------|-------|
| Legacy 5001 | Universal 5001 (same port) |
| Universal 5050 | Removed (worktree deleted) |

```bash
# 5a. Remove the worktree (no longer needed)
cd /Users/ss/IdealistaRank
git worktree remove /Users/ss/IdealistaRank-properties-universal

# 5b. Delete the feature branch (merged)
git branch -d feature/properties-universal
```

## Rollback Plan

If issues arise after cutover:

```bash
# Roll back to Legacy tag
cd /Users/ss/IdealistaRank
git checkout legacy-final-YYYYMMDD
docker compose build --no-cache
docker compose up -d

# Restore Legacy database if needed
docker exec -i idealista-db psql -U idealista -d idealista < ~/backups/idealista_legacy_YYYYMMDD.sql
```

## Verification Checklist

After cutover, verify:

- [ ] `curl http://localhost:5001/api/healthz` returns `{"ok": true}`
- [ ] Dashboard loads at `http://localhost:5001/`
- [ ] Properties page loads at `http://localhost:5001/properties`
- [ ] Legacy lands page loads at `http://localhost:5001/` (lands tab)
- [ ] Manual sync works (POST /api/ingest/email/run with auth)
- [ ] Scheduler is running (GET /api/scheduler/status)
- [ ] Favorites work (POST /api/property/{id}/favorite)
- [ ] AI analysis works (POST /api/property/{id}/analyze/structured)

## Timeline

1. Backup + tag: ~5 min
2. Merge + test: ~10 min
3. Docker rebuild: ~5 min
4. DB migration: ~2 min
5. Verification: ~10 min
6. **Total: ~30 min**
