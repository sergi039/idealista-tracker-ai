-- Create search_profiles table and link properties -> search_profiles.
-- PostgreSQL-only migration.

CREATE TABLE IF NOT EXISTS search_profiles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    is_default BOOLEAN DEFAULT FALSE,

    email_matchers JSON,
    classification_rules JSON,
    travel_targets JSON,
    ui_config JSON,
    scoring_config JSON,

    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_search_profiles_name
    ON search_profiles (name);

CREATE INDEX IF NOT EXISTS ix_search_profiles_is_active
    ON search_profiles (is_active);

CREATE INDEX IF NOT EXISTS ix_search_profiles_is_default
    ON search_profiles (is_default);

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS search_profile_id INTEGER;

CREATE INDEX IF NOT EXISTS ix_properties_search_profile_id
    ON properties (search_profile_id);

