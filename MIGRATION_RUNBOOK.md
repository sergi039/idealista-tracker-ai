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

Do not run `db.create_all()` or individual migration files in production. The
container runs `python -m migrations.runner` before gunicorn and stops before
starting the application if migration validation fails.

### Normal automatic path

- A new empty database receives every numbered migration in order. Each
  applied version, name, and file checksum is stored in `schema_migrations`.
- A database with a ledger validates every recorded name and checksum, applies
  only pending migrations, and otherwise starts normally.
- Any unknown, renamed, or changed recorded migration is a hard error. Fix the
  migration history; do not edit ledger rows to bypass it.

### Automatic baseline for an exact historical database

A pre-ledger database with application tables is compared with the frozen
historical fingerprint. If its tables, columns, `id` primary keys, and ID types
match exactly, the runner automatically records migrations 000–012 as a
metadata-only baseline. It does not execute those migration files or rewrite
application data. Migrations after 012 remain pending and run normally.

If the fingerprint differs, automatic startup fails closed and leaves the
database without a ledger. This strict check must not be weakened to accept
drift automatically.

### Manual verified baseline for drifted databases

Use this escape hatch only when an operator has established that a no-ledger
database already represents the application schema through migration 012 and
the reported drift is intentional, such as an extra ad-hoc table or column.
Before continuing:

1. Take and verify a restorable database backup.
2. Review the runner's complete fingerprint mismatch and inspect a schema-only
   dump (`pg_dump --schema-only`).
3. Compare the application tables, columns, primary keys, constraints, and
   indexes with `models.py` and migrations 000–012. Confirm that no schema or
   data operation from those migrations still needs to run.
4. Record why every difference is safe. This command marks all migrations
   000–012 as applied, so it must not be used to conceal missing migration work.

After that verification, run the explicit one-off command:

```bash
docker compose run --rm app python -m migrations.runner --baseline-existing --yes
```

The exact runner command is
`python -m migrations.runner --baseline-existing --yes`. It creates only the
`schema_migrations` ledger and records the version, name, and actual SHA-256
checksum of each tracked migration from 000 through 012. It prints every row it
records, executes no migration SQL, and exits. It refuses to run without
`--yes`, when a ledger already exists, or when no recognized application table
exists. There is deliberately no environment-variable switch for this mode.

Review the printed ledger entries, then start the normal stack again:

```bash
docker compose up -d
```

The ordinary runner now validates the recorded baseline, applies any migration
after 012, and starts gunicorn only if the database is current.

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

## Repairing search profiles fragmented by folded subjects (#103)

One-off data repair, not part of the cutover. A long `Subject` header arrives
folded (RFC 5322 2.2.3) and the saved-search extractor used to stop at the
line break, so a single Idealista subscription accumulated four
`search_profiles` rows with progressively truncated names. #101 fixed the
cause; `services/search_profile_repair_service.py` merges the rows already in
the database. It recomputes each listing's saved-search name from its own
stored `properties.email_subject` — same unfolding, same extractor as
ingestion — and never guesses from name similarity.

**Stop ingestion before applying.** `properties.search_profile_id` is
`ON DELETE SET NULL`: a listing written into a fragment between the
zero-check and the `DELETE` is not rejected, it is silently left with a NULL
profile. The scheduler runs inside the app process, so stopping the app
container is what stops ingestion. The repair aborts on any count mismatch
and commits nothing, but it cannot prevent a concurrent writer — only notice
one.

```bash
# 1. Back up first (the repair deletes profile rows).
docker exec idealista-db pg_dump -U idealista -d idealista > ~/backups/idealista_pre_repair_$(date +%Y%m%d).sql

# 2. Stop ingestion. This stops the in-process scheduler with the app.
docker compose stop app

# 3. Dry run. Writes nothing; prints the plan and the per-profile counts.
docker compose run --rm app python -m services.search_profile_repair_service

# 4. Read the report. Expect one saved search, one survivor, the fragments
#    listed for deletion, and "listings to move" matching the fragment totals.
#    Anything under "listings whose saved-search name could not be recomputed"
#    stays where it is and keeps its fragment alive - that is intended.

# 5. Apply.
docker compose run --rm app python -m services.search_profile_repair_service --apply

# 6. Restart the app.
docker compose up -d app
```

The exit code says exactly what happened to the database:

| code | status | what it means |
|------|--------|---------------|
| 0 | `clean` / `pending` / `applied` | nothing needed doing, or the repair committed and verified. A second `--apply` is a clean no-op. |
| 1 | `mismatch` | **nothing was committed.** A count disagreed, the transaction rolled back, the database is untouched. |
| 2 | `applied_report_unavailable` | **the repair was committed** and only the after-report could not be read back. |

On **1**, do not rerun blindly: read the `ERROR:` lines, confirm ingestion is
really stopped, and re-run the dry run first. On **2** the destructive part is
already durable — inspect the database (the verification query below) before
doing anything else; re-running is safe only once you have confirmed the state,
since the repair is idempotent.

A saved search reported as `BLOCKED:` was **not** repaired and nothing about it
was touched: its listings live in a profile that also holds a different saved
search, so renaming that profile would mislabel the others. This does not
change the exit code. It means something other than the fold put those rows
together — `ProfileAssignmentService` reassigns by location, and profiles can
be edited by hand — and untangling it is an owner decision. Once the profile
holds only one saved search, a later run repairs it normally.

Verify afterwards:

```bash
docker exec idealista-db psql -U idealista -d idealista -c \
  "select search_profile_id, count(*) from properties group by 1 order by 1;"
# No new NULL bucket, and the fragment ids are gone.

curl -s http://localhost:5001/api/healthz
```

Rollback is the backup from step 1.

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
