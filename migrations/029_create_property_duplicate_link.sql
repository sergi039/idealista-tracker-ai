-- The owner's word on a doubtful pair of listings (#633, PR 2 of 3).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- services/property_duplicates.py decides on read which publications are the
-- same home. A pair it can only call `possible`, or one it got wrong, is the
-- owner's to settle, and this table is where that is kept: "these two are the
-- same home" or "these two are different", and nothing else. No listing row is
-- merged, folded or deleted by it.
--
-- ONE ROW PER UNORDERED PAIR. `property_low_id < property_high_id` is a CHECK,
-- so (3, 7) and (7, 3) cannot both exist and the UNIQUE below really means one
-- pair; the single writer (`property_duplicates.set_link`) orders the pair
-- before it writes and takes both property rows FOR UPDATE in id order, the
-- discipline 026 made canonical for routing.
--
-- ON DELETE CASCADE: a link is a fact about two listings, and with one of them
-- gone it describes nothing. (The app cannot delete a property today; direct
-- SQL can, and must not be blocked by a decision about it.)
--
-- `evidence` is the rule's own `explain` output at decision time, so a later
-- reader can see what the owner was looking at when they overrode it,
-- including a cadastral veto they chose to outrank.
--
-- `decided_at` is UTC, written naive like the rest of the schema; the default
-- is spelled `NOW() AT TIME ZONE 'UTC'` because a bare NOW() into a WITHOUT
-- TIME ZONE column takes the SESSION's zone.
--
-- Idempotent without DO blocks: every constraint is inside the one
-- CREATE TABLE IF NOT EXISTS, which is skipped whole when the table exists.
-- The index is CREATE INDEX IF NOT EXISTS. The low id needs no index of its
-- own: it leads the UNIQUE.

CREATE TABLE IF NOT EXISTS property_duplicate_link (
    id SERIAL PRIMARY KEY,
    property_low_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    property_high_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    verdict VARCHAR(9) NOT NULL,
    source VARCHAR(8) NOT NULL DEFAULT 'owner',
    note TEXT,
    evidence JSON,
    decided_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    CONSTRAINT ck_property_duplicate_link_order
        CHECK (property_low_id < property_high_id),
    CONSTRAINT ck_property_duplicate_link_verdict
        CHECK (verdict IN ('same', 'different')),
    CONSTRAINT ck_property_duplicate_link_source
        CHECK (source IN ('owner', 'seed')),
    CONSTRAINT ux_property_duplicate_link_pair
        UNIQUE (property_low_id, property_high_id)
);

CREATE INDEX IF NOT EXISTS ix_property_duplicate_link_property_high_id
    ON property_duplicate_link(property_high_id);
