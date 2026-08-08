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

### What counts as a fold fragment

Only a fold fragment is ever emptied or renamed, and a profile qualifies by
**replaying the bug** rather than by resembling its victims. For a saved search
N, profile P qualifies when:

1. P's name is not N; **and**
2. running the pre-#101 extractor over every listing of N inside P — the same
   extraction, on the stored subject, *without* unfolding, so it truncates at
   the CR exactly as it used to — returns precisely the name P carries, for all
   of them.

That is cause and effect: P's name was produced by this bug, on these rows.
Weaker signals are not enough. Two profiles holding one saved search prove
nothing — `ProfileAssignmentService` files listings by location, so "Coast" and
"City" legitimately split one subscription. A line break in the subject proves
nothing either: it can sit past the end of the name, where it truncated nothing.

There is deliberately no extra "name must be a word-boundary prefix" rule: it
approximated the replay and, kept alongside it, would only reject genuine
fragments whose fold landed on punctuation.

**One consequence, and it is the right one:** since #101 stores subjects
unfolded, this repair can only ever act on rows written **before** that fix.
Those rows are precisely the damage; anything ingested afterwards is untouchable
by construction.

### What it will never do

Anything that is not a fold fragment is a decision somebody made, and it is left
alone:

- **It never moves a listing you reassigned by hand.** A listing pinned through
  the profile-change form (`manual_override`, which `ProfileAssignmentService`
  already refuses to override) stays exactly where you put it — and because it
  stays, the profile holding it is not empty, so that profile is not deleted
  either. Pinned listings still count towards what a profile holds, so they can
  stop a rename. Pinning is re-read inside the transaction as well: a listing
  pinned or moved *after* planning aborts the repair instead of being dragged
  back.
- **It never renames a profile that is not a fold fragment**, and never renames
  one holding a second saved search, whether it already did or the plan moved
  one in.
- **It never moves listings out of a profile that is not a fold fragment** —
  they stay, and the report lists them under "left alone".
- **It never gives one profile to two saved searches.**
- **It never deletes a profile that still holds a listing**, for any reason.
- **It never deletes the default profile and never makes another profile the
  default.** One default goes in and the same one comes out, even if the repair
  empties it.
- **It never creates a profile**, and **never adopts an already-orphaned
  listing** — both are reported, neither is acted on.
- **It never touches a listing whose saved-search name cannot be recomputed.**

One thing it *does* do, so it is not mistaken for a promise: it moves listings
into the profile that already carries their name even when that profile also
holds a stray listing of another saved search. Ingestion routes those listings
to that same profile anyway, so the repair reaches no state the system would
not reach on its own — and the stray's own group is reported `BLOCKED:` rather
than hidden. What is protected is that a profile is never *renamed* out from
under what it holds.

A `BLOCKED:` group means nothing about it was changed. Read the report; the
exit code does not change for it.

One run does the whole job: planning re-examines groups until nothing more can
be repaired, so the result never depends on alphabetical order and re-running
only picks up what genuinely changed in the database since.

### Stop ingestion before applying — but know what that buys you

`properties.search_profile_id` is `ON DELETE SET NULL`, so a listing written
into a fragment while it is being removed would be silently left with a NULL
profile. The code defends against that on its own: the whole repair is one
transaction, each profile is re-counted immediately before its `DELETE`, the
deletes are flushed *inside* the transaction, and every touched profile plus
the total number of NULL-profile listings is re-counted again afterwards. A
listing that slipped in is caught there and the entire repair is rolled back.
After that flush the database closes the window itself: the pending `DELETE`
holds the profile row, so a concurrent insert referencing it waits for this
transaction and then fails its foreign key instead of being orphaned.

The plan is also re-checked against the database before any of it is applied.
Every profile it touches is re-read — by column, so a stale object from
planning cannot answer — and its name and default flag compared with what the
plan decided from; each reassignment names the profile the row is expected to
be in; and every planned row's pinned state is read again after the moves. A
profile renamed, a profile made default, a listing moved or pinned by hand in
between: each of those aborts the repair before `COMMIT`.

So stopping ingestion is not what makes the repair safe — it is what makes it
**succeed**. A concurrent write turns the run into a clean abort (exit 1, no
changes) that has to be repeated, and it would make the ingestion itself fail
on a foreign key. The scheduler runs inside the app process, so stopping the
app container is what stops ingestion; the repair command refuses to start a
scheduler of its own but cannot stop yours.

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

The exit code says exactly what is **known** about the database:

| code | status | what it means |
|------|--------|---------------|
| 0 | `clean` / `pending` / `applied` | nothing needed doing, or the repair committed and verified. A second `--apply` is a clean no-op. |
| 1 | `mismatch` | it failed **before COMMIT**. Nothing was committed, the database is untouched. |
| 2 | `applied_report_unavailable` | **the repair was committed** and only the after-report could not be read back. |
| 3 | `commit_outcome_unknown` | COMMIT itself did not complete cleanly. **The outcome is unknown** — it may or may not have been applied. |

Only **1** means the database is untouched. On 1, read the `ERROR:` lines,
confirm ingestion is really stopped, and re-run the dry run.

On **2** the destructive part is already durable — inspect the database with
the verification query below before doing anything else.

On **3** assume nothing. The server may have applied the commit and lost the
connection before acknowledging, so treat the state as unverified: run the
verification query, and only then decide. The report prints a best-effort
read-back under "read back afterwards" — that is an observation, not a
verdict. The repair is idempotent, so once you have established the actual
state a dry run followed by a re-run is safe.

A saved search reported as `BLOCKED:` was **not** repaired and nothing about it
was touched: its listings live in a profile that also holds a different saved
search, so renaming that profile would mislabel the others. This does not
change the exit code. It means something other than the fold put those rows
together — `ProfileAssignmentService` reassigns by location, and profiles can
be edited by hand — and untangling it is an owner decision. Blocking accounts
for the plan's own effects too: a profile that acquires a second saved search
part-way through the plan blocks as well, and one the plan empties out stops
blocking within the same run.

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
