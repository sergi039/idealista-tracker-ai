-- Where a listing's status came from (owner decision, 2026-08-10).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- idealista answers this machine with a DataDome block: every "Check status"
-- comes back as `error`, and the stored status is deliberately left alone
-- (issue #136). What still works is idealista's own removal email, which the
-- ingester already reads, and the owner setting a status by hand. Those three
-- are not equally trustworthy and the page had no way to say which one it was
-- looking at -- a `removed` from idealista's own mail and a `removed` somebody
-- typed read identically.
--
-- Values the app writes: 'ingest' (the default a row is created with, never
-- verified), 'email' (idealista's removal notice), 'check' (the scraper read
-- the listing page) and 'manual' (the owner). NULL means the row predates this
-- column. Nothing is backfilled: no stored row records how its status was
-- decided, and a guess would be exactly the false confirmation the column
-- exists to prevent.

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS listing_status_source VARCHAR(16);

ALTER TABLE lands
    ADD COLUMN IF NOT EXISTS listing_status_source VARCHAR(16);

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_listing_status_source_enum
        CHECK (
            listing_status_source IS NULL
            OR listing_status_source IN ('ingest', 'email', 'check', 'manual')
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_listing_status_source_enum
        CHECK (
            listing_status_source IS NULL
            OR listing_status_source IN ('ingest', 'email', 'check', 'manual')
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
