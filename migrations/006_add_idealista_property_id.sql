-- Add a stable Idealista listing id column for fast dedup/updates.
-- PostgreSQL-only migration.

ALTER TABLE lands
    ADD COLUMN IF NOT EXISTS idealista_property_id BIGINT;

-- Backfill from existing URLs when possible.
UPDATE lands
SET idealista_property_id = substring(url from '/inmueble/([0-9]+)')::bigint
WHERE idealista_property_id IS NULL
  AND url IS NOT NULL
  AND url ~ '/inmueble/[0-9]+';

-- Speed up lookups by property id (used by IMAP ingestion and status updates).
CREATE INDEX IF NOT EXISTS ix_lands_idealista_property_id
    ON lands (idealista_property_id);

