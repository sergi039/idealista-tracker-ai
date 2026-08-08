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

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_info
        WHERE constraint_info.conrelid = 'properties'::regclass
          AND constraint_info.confrelid = 'search_profiles'::regclass
          AND constraint_info.contype = 'f'
          AND constraint_info.confdeltype = 'n'
          AND constraint_info.conkey = ARRAY[
              (
                  SELECT attribute_info.attnum
                  FROM pg_attribute AS attribute_info
                  WHERE attribute_info.attrelid = 'properties'::regclass
                    AND attribute_info.attname = 'search_profile_id'
              )
          ]::smallint[]
    ) THEN
        ALTER TABLE properties
            ADD CONSTRAINT fk_properties_search_profile_id
            FOREIGN KEY (search_profile_id)
            REFERENCES search_profiles(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_properties_search_profile_id
    ON properties (search_profile_id);
