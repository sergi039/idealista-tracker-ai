-- Drop the `profile_assignment` key from every property's enrichment JSON.
-- PostgreSQL-only migration, like every migration in this directory.
--
-- The key recorded which mechanism last set `properties.search_profile_id`:
-- `nearest_custom_target` when the geo heuristic refiled the row, or
-- `manual_override` when the owner pinned it through the profile form. Both
-- writers were removed on 2026-08-09 -- ingestion, reading the saved-search
-- URL out of the alert email, is the only writer left -- so every stored value
-- is now a claim about code that no longer exists.
--
-- `manual_override` is the one that matters: `search_profile_repair_service`
-- still refuses to move a row carrying it, and with nothing able to clear that
-- flag through the UI any more, a stale pin would freeze a listing in a
-- fragmented profile forever. Clearing the key is what lets the repair reach
-- those rows.
--
-- Why a loop, and why the loop only carries ids
-- ---------------------------------------------
-- `enrichment` is a `json` column: PostgreSQL stores the document as text and
-- validates only its syntax, so it can hold documents that abort a set-based
-- rewrite -- and with it the deploy, having cleared nothing:
--
--   * `enrichment::jsonb - 'key'` parses numbers into `numeric`, so a literal
--     like `1e1000000` raises numeric_value_out_of_range (22003);
--   * `json_each()` decodes object keys into `text`, which cannot hold NUL, so
--     an escaped-NUL key raises untranslatable_character (22P05).
--
-- Neither can be produced by this application's own writers, and a predicate
-- per known-bad shape would only postpone the next one, so each row is
-- rewritten in its own subtransaction. Only those two SQLSTATEs are caught:
-- a lock timeout, a deadlock or a disk error must still abort the migration
-- rather than be recorded as applied over rows it never touched.
--
-- The cursor selects ids only, and the new value is recomputed from
-- `p.enrichment` inside the UPDATE rather than from a value read earlier.
-- The UPDATE takes the row lock and re-reads the current document, so a
-- concurrent writer cannot have its commit overwritten by a stale snapshot;
-- the guards are re-evaluated there too, so a row that lost the key in the
-- meantime is left alone. Rows inserted while this runs are written by the
-- new code and never carry the key.
--
-- `json_object_agg` over an object whose only key was removed aggregates an
-- empty set and returns NULL, which would turn a document into SQL NULL;
-- COALESCE keeps it an empty object. Re-running the file is a no-op.
--
-- The one race this cannot close, and why it does not arise here
-- --------------------------------------------------------------
-- Row locking stops a writer that commits *before* this UPDATE. It cannot stop
-- one that read the document earlier and commits *after*: that writer restores
-- whatever it read, key included. No data migration can, short of taking the
-- table offline -- it is the application's own read-modify-write, not this
-- statement's.
--
-- It does not arise on this deploy path. `Dockerfile` runs
-- `python -m migrations.runner && exec gunicorn ...`, so migrations execute in
-- the new container before it serves anything, and `docker compose up -d`
-- has already stopped the old one. There is no process holding an older
-- snapshot. Beyond that, the only code that ever wrote this key is deleted in
-- the same commit, so a writer that could restore it would have to be running
-- the previous image.
--
-- Should someone run the runner by hand against a live old container anyway,
-- the damage is bounded and self-correcting: a row keeps a stale
-- `manual_override`, which blocks nothing but a repair-time move, and running
-- this migration again clears it.
--
-- RAISE uses the `USING MESSAGE =` form deliberately. The ordinary
-- format-string form needs a percent sign as its placeholder, and psycopg2
-- reads a lone percent sign anywhere in migration SQL -- comments included --
-- as a parameter marker, which fails the statement before PostgreSQL sees it.
-- For the same reason this file contains no percent sign at all.

DO $$
DECLARE
    target_id integer;
BEGIN
    FOR target_id IN
        SELECT id
        FROM properties
        WHERE enrichment IS NOT NULL
          AND json_typeof(enrichment) = 'object'
        ORDER BY id
    LOOP
        BEGIN
            UPDATE properties AS p
            SET enrichment = COALESCE(
                    (
                        SELECT json_object_agg(entry.key, entry.value)
                        FROM json_each(p.enrichment) AS entry
                        WHERE entry.key <> 'profile_assignment'
                    ),
                    '{}'::json
                )
            WHERE p.id = target_id
              AND p.enrichment IS NOT NULL
              AND json_typeof(p.enrichment) = 'object'
              AND EXISTS (
                    SELECT 1
                    FROM json_each(p.enrichment) AS entry
                    WHERE entry.key = 'profile_assignment'
              );
        EXCEPTION
            WHEN untranslatable_character OR numeric_value_out_of_range THEN
                RAISE WARNING USING MESSAGE =
                    'migration 014 could not decode enrichment for property id='
                    || target_id::text
                    || ', profile_assignment left in place ('
                    || SQLERRM
                    || ')';
        END;
    END LOOP;
END
$$;
