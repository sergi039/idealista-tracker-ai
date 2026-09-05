# IdealistaRank — Codex entrypoint

Flask application for saved-search email ingestion, property scoring and
enrichment. GitHub issues in `sergi039/idealista-tracker-ai` define the work.
This is a compact routing guide; [CLAUDE.md](CLAUDE.md) holds the detailed
contracts and incident evidence. Search its headings and task terms, then read
the relevant passages before changing that area. Do not load the entire history
as a substitute for inspecting the code.

## Two-machine topology

The owner's 2026-08-31 topology in `CLAUDE.md` takes precedence over older
dual-stack examples in `CONTRIBUTING.md` and `docs/DEV_RULES.md`.

- The Mac mini is the only server: the app, PostgreSQL, Redis and OSRM run
  there in Docker. The MacBook is a coding/test/browser client; its
  `http://127.0.0.1:5001/` tunnel opens the mini's app.
- Use that existing app. Do not start another app stack, scheduler, ingester
  or PostgreSQL server on the laptop, including for migration tests.
- The mini's deploy watcher owns rebuilds and `data/.deployed_sha`. A PR or
  merge is not proof that the container is current. Before an authorized
  operational action, read the matching `CLAUDE.md` deployment contract and
  [tools/backfill_status.sh](tools/backfill_status.sh). A deployment check must
  include a rendered page returning 200 as well as healthz.
- Migrations require PostgreSQL 15 via
  [tools/ci/migration_test_db.sh](tools/ci/migration_test_db.sh): an isolated
  throwaway container on the mini, tunneled to 55432. Never use a laptop
  database or production's 5434. If the mini is unavailable, use CI;
  SQLite does not validate migration SQL.

## Find the task boundary

- Pages/API: [routes/](routes/), [templates/](templates/), [static/](static/).
- Ingestion and enrichment: [services/](services/); reuse
  [services/ingest_policy.py](services/ingest_policy.py) and
  [services/enrichment_write.py](services/enrichment_write.py).
- Paid Google requests: [utils/google_spend.py](utils/google_spend.py), the
  single billed-call boundary. Paid enrichment needs the owner's explicit
  authorization for this run, with module/endpoint, rows and cost announced
  before execution. A previous run does not authorize a new scope.
- Schema/data: [models.py](models.py), [migrations/](migrations/),
  [MIGRATION_RUNBOOK.md](MIGRATION_RUNBOOK.md). Bulk tools in [utils/](utils/)
  need the long-job/backfill protocol in `CLAUDE.md` before execution.
- Regression evidence: [tests/](tests/). Exercise the failing input at the
  real behavior boundary; a pass count alone does not prove the fix.

## Work and verification

Use an isolated `codex/**` worktree from `origin/main` and publish a PR.
Preserve shared checkouts, other sessions' files and existing feature branches.
Stage explicit paths; never sweep unrelated changes into a commit. Read
[tools/autopilot/README.md](tools/autopilot/README.md) before joining an issue
already being worked, and the `CLAUDE.md` Workflow section before merging,
including its owner-designated orchestrator contract.

[tools/ci/local_ci.sh](tools/ci/local_ci.sh) is the canonical local gate:
locked Ruff checks, source-bundle hygiene and `uv run pytest tests/ -q`.
The standing workflow requires that pytest command and its real output before
claiming done; never describe an unrun check as green. Required PR checks and
review remain required. For a documentation-only task, follow the owner's
authorized verification scope and check references plus `git diff --check`.

Never read or echo `.env`, copy credentials between devices, or commit local
data, settings, source archives or generated dumps.
