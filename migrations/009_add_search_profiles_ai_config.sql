-- Add ai_config JSON column to search_profiles.
-- PostgreSQL-only migration.

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS ai_config JSON;

