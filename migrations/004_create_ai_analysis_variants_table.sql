CREATE TABLE IF NOT EXISTS ai_analysis_variants (
    id SERIAL PRIMARY KEY,
    land_id INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    model VARCHAR(128),
    analysis JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_ai_analysis_variants_land_id
    ON ai_analysis_variants (land_id);
CREATE INDEX IF NOT EXISTS ix_ai_analysis_variants_provider
    ON ai_analysis_variants (provider);
CREATE INDEX IF NOT EXISTS ix_ai_analysis_variants_land_provider
    ON ai_analysis_variants (land_id, provider);
