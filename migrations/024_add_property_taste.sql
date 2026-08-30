-- The owner's taste: a versioned profile and a per-listing score (issue #498).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- `taste_profile` is an INSERT-ONLY ledger. The version IS the primary key:
-- SERIAL assigns it transactionally, so four gunicorn threads and a CLI
-- container cannot mint the same version twice — the file-based design this
-- replaced could not say that. "Current" is the row with the greatest id; a
-- failed rebuild inserts nothing and therefore leaves the current profile
-- exactly where it was. Prior versions are retained by construction, which
-- is what makes a stored score's `profile_version` readable forever.
--
-- `taste_score` is a real column and not a key inside JSON because the list
-- SORTS on it: an ORDER BY casting into stored JSON is the hazard-service
-- lesson (a hand-edited value raises on PostgreSQL and takes /properties
-- down with it). NUMERIC(5,2) with a range CHECK, matching the three score
-- columns beside it. NULL means nobody scored the row (#98) — a bridge
-- refusal writes nothing, so NULL also covers "somebody tried and the
-- bridge said no", which keeps the row in the backfill's scope.
--
-- `taste` is the evidence beside the number: status, reasons (RU), matched
-- traits, the closest reference listing, confidence, provider+model, the
-- `profile_version` it was scored against and a fingerprint of the facts it
-- was scored on. A score whose profile has moved on presents as stale
-- rather than silently wrong.
--
-- Nothing is backfilled here: `utils/backfill_taste.py` is what fills it,
-- on the owner's explicit request.

CREATE TABLE IF NOT EXISTS taste_profile (
    id SERIAL PRIMARY KEY,
    built_at TIMESTAMP NOT NULL,
    provider VARCHAR(16) NOT NULL,
    model VARCHAR(120),
    signals_fingerprint VARCHAR(64) NOT NULL,
    source JSON NOT NULL,
    profile JSON NOT NULL
);

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS taste_score NUMERIC(5,2)
        CONSTRAINT ck_properties_taste_score_range
        CHECK (taste_score IS NULL OR (taste_score >= 0 AND taste_score <= 100));

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS taste JSON;

-- The list sorts on it with NULLS LAST; an index keeps that sort off a
-- sequential scan once the column is mostly filled.
CREATE INDEX IF NOT EXISTS ix_properties_taste_score
    ON properties(taste_score);
