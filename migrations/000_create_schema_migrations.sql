-- Migration ledger used by migrations.runner. This is intentionally the only
-- bootstrap object the runner knows by name; all application schema is SQL.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(3) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
