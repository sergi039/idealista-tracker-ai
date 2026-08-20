-- Documents and photos attached to a listing (issue #430).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- The ticket's worked example is the ficha catastral the agency sent by
-- WhatsApp: evidence that is not a measurement, cannot be recomputed, and had
-- nowhere to go. This table holds what is known *about* each file; the bytes
-- live under DATA_DIR, content-addressed by their sha256
-- (services/attachments.py explains why that way round).
--
-- Two things in here are the point, and both are invariants the database keeps
-- rather than checks some future writer has to remember.
--
-- **The composite foreign key.** An attachment belongs to a property and,
-- optionally, to the exchange it arrived in. Two separate references would
-- allow property_id = 1 with an activity_id belonging to property 2 -- an
-- attachment editable and visible from a page it does not belong to. The pair
-- `(activity_id, property_id)` referencing `property_activity (id,
-- property_id)` makes that row impossible to insert. The UNIQUE it needs on
-- the parent was added in migration 021 for exactly this.
--
-- MATCH SIMPLE (the default) is what makes the optional half work: a NULL
-- `activity_id` satisfies the constraint whatever `property_id` holds, which
-- is the "filed against the listing rather than against one exchange" case.
--
-- **No UNIQUE on (property_id, content_sha256).** It reads like the right
-- thing and is not: the same document may legitimately be attached to two
-- exchanges on one listing, and after a soft delete the row still occupies the
-- key, so re-uploading the same file would be refused with nothing on screen
-- to explain why. Deduplication belongs to the disk -- one file per hash --
-- and a row here is a *link*, which is cheap.
--
-- Deletion is soft, like `property_activity`: `utils/sweep_attachments.py` is
-- the only thing that unlinks bytes, and only for a hash no live row still
-- references.

CREATE TABLE IF NOT EXISTS property_attachment (
    id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    -- The exchange it arrived in, when there is one.
    activity_id INTEGER,
    -- sha256 of the content: the on-disk name, the dedup key, and what a later
    -- integrity pass would re-compute.
    content_sha256 VARCHAR(64) NOT NULL,
    -- Stored rather than re-derived at every read, so changing the sharding
    -- scheme later does not have to rewrite every consumer.
    storage_path VARCHAR(255) NOT NULL,
    -- What the browser called it. Display and Content-Disposition only; it
    -- never takes part in building a path.
    original_filename VARCHAR(255),
    -- The SNIFFED type. Never the client's claim -- that is a hint about what
    -- the sender believed, and this is what the bytes are.
    content_type VARCHAR(64) NOT NULL,
    -- Measured from what was actually written, not from Content-Length.
    size_bytes INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,
    uploaded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT fk_property_attachment_activity
        FOREIGN KEY (activity_id, property_id)
        REFERENCES property_activity (id, property_id)
        ON DELETE SET NULL
);

DO $$ BEGIN
    ALTER TABLE property_attachment ADD CONSTRAINT ck_property_attachment_kind
        CHECK (kind IN ('document', 'photo'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE property_attachment ADD CONSTRAINT ck_property_attachment_size
        CHECK (size_bytes > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- A hash is 64 lowercase hex characters. A row whose hash is anything else
-- names a file the storage layer cannot have written.
DO $$ BEGIN
    ALTER TABLE property_attachment ADD CONSTRAINT ck_property_attachment_sha256
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_property_attachment_property_id
    ON property_attachment(property_id);

CREATE INDEX IF NOT EXISTS ix_property_attachment_activity_id
    ON property_attachment(activity_id);

-- The sweeper's question is "does any live row still reference this hash",
-- asked once per file on disk.
CREATE INDEX IF NOT EXISTS ix_property_attachment_content_sha256
    ON property_attachment(content_sha256);
