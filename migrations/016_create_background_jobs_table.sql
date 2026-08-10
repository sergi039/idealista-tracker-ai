-- Persist background job state so a redeploy does not lose it (issue #176).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- services/background_jobs.py used to keep every queued/running/finished job
-- in a process-local dict. tools/autopilot/deploy_watcher.sh recreates the
-- app container on every new main -- as often as every 300 s -- so a job in
-- flight at that moment was abandoned: /api/jobs/<id> answered 404, the AI
-- analysis result (already paid for) was thrown away, and nothing recorded
-- that the run was ever attempted.
--
-- This table is written on enqueue, on start and on completion. `dedupe_key`
-- plus the partial unique index below is the idempotency guard for
-- acceptance criterion 4: re-running an interrupted AI analysis must not
-- leave two writers racing for the same (property, provider). The index
-- only covers rows still 'queued' or 'running', so a terminal row never
-- blocks a legitimate retry -- only a second concurrently active job for the
-- same key does.

CREATE TABLE IF NOT EXISTS background_jobs (
    id VARCHAR(32) PRIMARY KEY,
    job_type VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    dedupe_key VARCHAR(255),
    meta JSON NOT NULL DEFAULT '{}'::json,
    result JSON,
    error TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE
);

DO $$ BEGIN
    ALTER TABLE background_jobs ADD CONSTRAINT ck_background_jobs_status_enum
        CHECK (status IN ('queued', 'running', 'success', 'error', 'interrupted'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_background_jobs_job_type
    ON background_jobs (job_type);

CREATE INDEX IF NOT EXISTS ix_background_jobs_status
    ON background_jobs (status);

-- Only one active (queued/running) row may hold a given dedupe_key. A
-- terminal row (success/error/interrupted) is invisible to this index, so a
-- retry after interruption is never blocked by the run it is replacing.
CREATE UNIQUE INDEX IF NOT EXISTS ux_background_jobs_active_dedupe_key
    ON background_jobs (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
