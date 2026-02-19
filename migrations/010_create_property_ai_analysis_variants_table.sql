-- Create property_ai_analysis_variants table (universal Property AI variants).
-- PostgreSQL-only migration.

CREATE TABLE IF NOT EXISTS property_ai_analysis_variants (
    id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    model VARCHAR(128),
    analysis JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_property_ai_analysis_variants_property_id
    ON property_ai_analysis_variants(property_id);

CREATE INDEX IF NOT EXISTS ix_property_ai_analysis_variants_provider
    ON property_ai_analysis_variants(provider);

CREATE INDEX IF NOT EXISTS ix_property_ai_analysis_variants_property_provider
    ON property_ai_analysis_variants(property_id, provider);

