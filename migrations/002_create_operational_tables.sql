CREATE TABLE IF NOT EXISTS scoring_criteria (
    id SERIAL PRIMARY KEY,
    criteria_name VARCHAR(100) NOT NULL,
    profile VARCHAR(20) DEFAULT 'combined',
    weight NUMERIC(3, 2) DEFAULT 1.0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_criteria_profile UNIQUE (criteria_name, profile)
);

CREATE TABLE IF NOT EXISTS sync_history (
    id SERIAL PRIMARY KEY,
    sync_type VARCHAR(20) NOT NULL,
    backend VARCHAR(20) NOT NULL,
    total_emails_found INTEGER DEFAULT 0,
    new_properties_added INTEGER DEFAULT 0,
    price_updated_count INTEGER DEFAULT 0,
    expired_count INTEGER DEFAULT 0,
    sync_duration INTEGER,
    status VARCHAR(20) DEFAULT 'completed',
    error_message TEXT,
    started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITHOUT TIME ZONE
);

-- Upgrade databases created before sync result counters were introduced.
ALTER TABLE sync_history
    ADD COLUMN IF NOT EXISTS price_updated_count INTEGER DEFAULT 0;
ALTER TABLE sync_history
    ADD COLUMN IF NOT EXISTS expired_count INTEGER DEFAULT 0;
UPDATE sync_history SET price_updated_count = 0 WHERE price_updated_count IS NULL;
UPDATE sync_history SET expired_count = 0 WHERE expired_count IS NULL;

CREATE TABLE IF NOT EXISTS land_history (
    id SERIAL PRIMARY KEY,
    land_id INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
    snapshot_date TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    price NUMERIC(10, 2),
    title TEXT,
    description TEXT,
    area NUMERIC(10, 2),
    land_type VARCHAR(20),
    url TEXT,
    change_type VARCHAR(50) NOT NULL,
    price_previous NUMERIC(10, 2),
    price_change_amount NUMERIC(10, 2),
    price_change_percentage NUMERIC(5, 2)
);

CREATE INDEX IF NOT EXISTS ix_land_history_land_id
    ON land_history (land_id);
CREATE INDEX IF NOT EXISTS ix_land_history_snapshot_date
    ON land_history (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS ix_land_history_change_type
    ON land_history (change_type);
