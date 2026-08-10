-- Enforce one AI-analysis variant per (property, provider) (#190 review,
-- blocker 3). PostgreSQL-only migration, like every migration in this
-- directory.
--
-- routes/api_routes.py's property_ai_analysis writer was query-then-insert
-- against a non-unique index (migration 010): find a row for
-- (property_id, provider), update it if found, else insert. An interrupted
-- job's async retry racing a `?sync=1` request -- which bypasses
-- background_jobs' dedupe_key entirely, since it never goes through
-- enqueue_job -- could both see "no row" and both insert, leaving two
-- variants racing for the same pair. Nothing in the database stopped it.
--
-- Deduplicate existing rows first (keep the newest per pair -- ties broken
-- by the higher id, i.e. whichever was written last), then replace the old
-- non-unique index with an actual UNIQUE constraint so the database itself
-- refuses a second row for a pair that already has one. The application-side
-- fix (routes/api_routes.py) turns the writer into an update-or-insert that
-- recovers from a lost insert race instead of assuming "no row" means it is
-- safe to add one.

DELETE FROM property_ai_analysis_variants
WHERE id NOT IN (
    SELECT DISTINCT ON (property_id, provider) id
    FROM property_ai_analysis_variants
    ORDER BY property_id, provider, created_at DESC, id DESC
);

DROP INDEX IF EXISTS ix_property_ai_analysis_variants_property_provider;

-- A UNIQUE constraint's backing index shares its name, so PostgreSQL raises
-- duplicate_table (42P07) on a repeat, not duplicate_object (42710) the way
-- a repeated CHECK constraint elsewhere in this directory does -- caught the
-- hard way by test_017_deduplicates_existing_rows_and_adds_the_unique_constraint
-- re-running this file and getting exactly that.
DO $$ BEGIN
    ALTER TABLE property_ai_analysis_variants
        ADD CONSTRAINT ux_property_ai_analysis_variants_property_provider
        UNIQUE (property_id, provider);
EXCEPTION WHEN duplicate_table THEN NULL;
END $$;
