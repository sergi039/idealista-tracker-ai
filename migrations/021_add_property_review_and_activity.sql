-- The owner's own decision about a listing, the action still outstanding on it,
-- and the log that records how either came about (issue #430).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- Everything measured about a listing already has a home. What decides whether
-- to buy one does not: on 2026-08-20 property 774 collected a cadastral
-- document, a promise with a date, three verbal answers and a rejection, and
-- all of it went in by hand through `docker exec` into `enrichment`, where it
-- renders nowhere and filters nowhere.
--
-- Two things are stored here and they are deliberately independent.
--
--   * The DECISION -- interested / waiting / rejected -- is what the owner
--     concluded. NULL is not a fourth value: it is "nobody decided yet", and
--     `services/owner_review.py` presents it as `undecided`, never folded into
--     `rejected`. That is #98's rule: an absence of a decision is not a
--     negative one.
--   * The NEXT ACTION is what is still outstanding, with the date it is due.
--     It is legal under any decision, because "interested; call the architect
--     on Friday" is an ordinary state and tying the reminder to `waiting`
--     would lose it. `overdue` is derived from the due date and is never
--     stored, so the column and the badge cannot drift apart.
--
-- `listing_status` is NOT touched. It answers "is the advert still live on the
-- portal", which is a different question, and writing an owner's verdict into
-- it is STATUS-002 again (see utils/repair_import_status_source.py).
--
-- The CHECK constraints below are the backstop for a writer that never reaches
-- Flask -- a hand-run script through `docker exec`, which is how 774's own data
-- arrived. Two details in them are measured rather than assumed, both against a
-- throwaway PostgreSQL 15:
--
--   * a CHECK passes on NULL, so `kind <> 'contact' OR channel IN (...)` lets a
--     contact through carrying no channel at all. Each conditional check
--     therefore asserts NOT NULL explicitly;
--   * `BTRIM` strips spaces and not tabs or newlines, so
--     `NULLIF(BTRIM(E'\n'), '')` is not NULL and a note holding a single
--     newline passed. The test is `~ '[^[:space:]]'` -- at least one character
--     that is not whitespace. (PostgreSQL's `[[:space:]]` does not cover
--     U+00A0; a note made of one non-breaking space would still pass. That is a
--     visible character pasted from a chat window, not blank-looking text.)
--
-- Those two interact, and the interaction is the trap. `NULL ~ '...'` is NULL,
-- not FALSE, so swapping `NULLIF(BTRIM(x), '') IS NOT NULL` (which yields
-- FALSE) for the regex re-opened the NULL hole one clause deeper: a contact
-- carrying only a channel, and a note with no body at all, were both accepted
-- until `COALESCE(..., FALSE)` went round each disjunct. That was caught by
-- running the inserts against a real server, which is the only place a CHECK
-- can be measured -- SQLite executes the model's copy, not this file.

CREATE TABLE IF NOT EXISTS property_activity (
    id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    kind VARCHAR(16) NOT NULL,
    -- When the exchange happened, which is not when the row was typed: an
    -- answer given on the phone yesterday is recorded today.
    happened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    channel VARCHAR(24),
    counterpart VARCHAR(160),
    asked TEXT,
    body TEXT,
    -- A verdict event carries the whole review state it recorded, not a
    -- from/to pair: a change of reason or of due date under an unchanged
    -- decision is a real change and a pair loses it.
    snapshot JSON,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    -- Soft delete. Everything else in this application can be recomputed; a
    -- sentence the owner typed cannot, so a mis-tap must not be the end of it.
    deleted_at TIMESTAMP WITHOUT TIME ZONE
);

DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT ck_property_activity_kind
        CHECK (kind IN ('note', 'contact', 'verdict'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- A verdict event and its snapshot are inseparable, in both directions.
DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT ck_property_activity_verdict_snapshot
        CHECK (kind <> 'verdict' OR snapshot IS NOT NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT ck_property_activity_snapshot_is_verdict
        CHECK (kind = 'verdict' OR snapshot IS NULL);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- The contact-only columns belong to contacts and to nothing else.
DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT ck_property_activity_contact_columns
        CHECK (kind = 'contact' OR (channel IS NULL AND counterpart IS NULL AND asked IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- And a contact really is one: a known channel, and something actually
-- exchanged or somebody named. A visit with nothing written down is a real
-- entry as long as it says who was met; an entirely empty row is not.
DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT ck_property_activity_contact_content
        CHECK (kind <> 'contact' OR (
            channel IS NOT NULL
            AND channel IN ('whatsapp', 'email', 'portal', 'phone', 'visit', 'other')
            AND (COALESCE(asked ~ '[^[:space:]]', FALSE)
                 OR COALESCE(body ~ '[^[:space:]]', FALSE)
                 OR COALESCE(counterpart ~ '[^[:space:]]', FALSE))
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- A note is its text.
DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT ck_property_activity_note_body
        CHECK (kind <> 'note' OR COALESCE(body ~ '[^[:space:]]', FALSE));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- An attachment (issue #430, PR3) will point at a property AND, optionally, at
-- the exchange it arrived in. This unique pair is what lets that table carry a
-- composite foreign key, so an attachment on one property can never reference
-- another property's exchange -- an invariant the database keeps rather than a
-- check some future writer has to remember.
DO $$ BEGIN
    ALTER TABLE property_activity ADD CONSTRAINT uq_property_activity_id_property
        UNIQUE (id, property_id);
-- Both, and `duplicate_table` is the one that actually fires: a UNIQUE
-- constraint is backed by an index of the same name, so a second run raises
-- 42P07 (relation already exists) rather than the 42710 every CHECK above
-- raises. Measured -- the re-run assertion in tests/test_postgres_migrations.py
-- failed on exactly this, which is what that assertion is for.
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_property_activity_property_id
    ON property_activity(property_id);

-- The timeline reads one property newest-first; the index is that query.
CREATE INDEX IF NOT EXISTS ix_property_activity_property_happened
    ON property_activity(property_id, happened_at DESC);

CREATE INDEX IF NOT EXISTS ix_property_activity_kind
    ON property_activity(kind);

-- ============================================================
-- The review columns on properties
-- ============================================================

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS owner_verdict VARCHAR(16);

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS owner_verdict_reason TEXT;

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS owner_verdict_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS next_action TEXT;

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS next_action_due_on DATE;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_owner_verdict_enum
        CHECK (owner_verdict IS NULL OR owner_verdict IN ('interested', 'waiting', 'rejected'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- A due date with nothing due is a reminder about nothing.
DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_due_needs_action
        CHECK (next_action_due_on IS NULL OR COALESCE(next_action ~ '[^[:space:]]', FALSE));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_properties_owner_verdict
    ON properties(owner_verdict);

CREATE INDEX IF NOT EXISTS ix_properties_next_action_due_on
    ON properties(next_action_due_on);
