-- A listing may belong to more than one subscription (#632): the ADDITIONAL
-- memberships live here. `properties.search_profile_id` stays the row's one
-- HOME -- scoring, travel targets, criteria, taste and similarity all read
-- that column and must stay single-valued -- and a row in this table says only
-- "this subscription delivered it too".
--
-- WHY A TABLE. A portal listing is one row whichever door it comes through
-- (`<source>:<id>` is UNIQUE), so a second saved search that delivers it finds
-- the row and creates nothing. A second row per subscription (what the
-- idealista path does) would split the owner's verdict, favourite, timeline,
-- attachments and paid enrichment across twins that are never deleted; a JSON
-- list could not be filtered without casting stored JSON, and could not be
-- kept on the right side of a route.
--
-- THIS MIGRATION ADDS THE TABLE AND ITS INVARIANTS AND NOTHING READS IT YET.
-- No writer exists until the next change, so the table is empty after this
-- file and every listing surface behaves exactly as before.
--
-- THE INVARIANTS ARE THE DATABASE'S, NOT A WRITER'S. Curation SQL through
-- `docker exec` is a supported workflow, and the recovery path after a bad
-- deploy is a roll-forward revert whose code may no longer know this table
-- (the runner refuses to start an image that does not know an applied
-- migration, so the previous image is never the recovery).
--
-- A MEMBERSHIP IS INSERTED OR DELETED, NEVER RE-POINTED. An UPDATE of either
-- key column is refused outright. That is what keeps the rules below to one
-- door: every membership that exists got there through the INSERT triggers,
-- the route's own move included.
--
-- 1. A membership never points at a routed stub. The properties trigger's own
--    function, `canonicalize_search_profile()` (migrations 025-027), runs on
--    every INSERT here unchanged: it only reads and rewrites
--    `NEW.search_profile_id`, under the same ascending FOR KEY SHARE pair, so
--    a membership written against a stub lands on the stub's target and waits
--    for a route decision in flight exactly as a listing insert does. A BEFORE
--    INSERT trigger runs before ON CONFLICT picks its arbiter row, so
--    `ON CONFLICT DO NOTHING` sees the rewritten key.
-- 2. A route moves the stub's memberships with it, in the routing statement's
--    own transaction: AFTER UPDATE OF routed_to re-INSERTS each of them
--    against the target -- through rules 1 and 3, `ON CONFLICT DO NOTHING`
--    for the ones the target already has -- and deletes the stub's rows. "A
--    stub holds nothing" stays true for both tables.
-- 3. A membership is never the listing's own home, and never the catch-all.
--    The insert is skipped (RETURN NULL: not an error, the fact is already
--    true or names no search at all), and a home that moves onto a
--    subscription the listing already belonged to drops that membership.
--
--    The home is read UNDER A ROW LOCK on the listing, `FOR SHARE`, and the
--    class is the whole point. The routing trigger takes FOR KEY SHARE on
--    `search_profiles` because the writer it must wait for, `route_profile()`,
--    announces itself with FOR UPDATE. Nothing announces a change of home: it
--    is a plain `UPDATE properties`, which locks the row FOR NO KEY UPDATE --
--    and FOR KEY SHARE does not conflict with that. FOR SHARE does. So:
--      * home change first, membership insert second: the insert waits on the
--        listing's row, and READ COMMITTED then hands it the row as committed
--        -- the new home -- so a membership equal to it is skipped;
--      * membership insert first, home change second: the UPDATE waits on the
--        same row, and its AFTER trigger then sees the committed membership
--        and deletes it.
--    Without the lock both orders commit a membership equal to the home
--    (review of #705). Lock order, for whoever adds a writer: a membership
--    insert takes `search_profiles` (KEY SHARE, ascending pair) and then the
--    listing's `properties` row (SHARE); a home change takes the `properties`
--    row (NO KEY UPDATE), then `search_profiles` (KEY SHARE), then deletes
--    membership rows. The two meet on one conflicting object, the listing's
--    row: KEY SHARE does not conflict with KEY SHARE, and the insert's own
--    row does not exist for the other side to wait on. One of each cannot
--    deadlock. A route in flight is a third party and keeps the cycle it
--    already had with a concurrent home change onto its pair (027): loud,
--    detected, never a misfile.
-- 4. A subscription that still holds a membership cannot be deleted: the
--    profile-side foreign key is NO ACTION. The two code paths that delete a
--    profile move or count memberships first; anything else is refused loudly
--    instead of dropping a membership silently.
--
-- EVERY STATEMENT THAT COULD BREAK RULE 3, and what answers it:
--   INSERT membership                 -> skipped under FOR SHARE (trigger b)
--   INSERT racing a home change       -> the row lock above, both orders
--   INSERT for a listing not yet committed -> refused like the foreign key:
--                                        trigger b cannot read its home, and
--                                        the FK check runs after the statement,
--                                        when the listing may be committed
--   UPDATE membership key columns     -> refused
--   UPDATE properties.search_profile_id -> the home trigger deletes the twin;
--                                        a home MOVE outside READ COMMITTED is
--                                        refused, its snapshot can miss one
--   UPDATE search_profiles.routed_to  -> re-insert through trigger b
--   INSERT properties                 -> no membership can exist yet: trigger
--                                        b refuses one this side cannot see
--   DELETE on either table            -> cannot create the equality
--   COPY / multi-row INSERT           -> row triggers fire per row
--
-- WHAT IS NOT CLOSED, stated so nobody assumes it: a raw
-- `UPDATE search_profiles SET routed_to` without the FOR UPDATE protocol was
-- never serialized against the canonicalizing trigger (027 says so for
-- `properties`), so an insert that read the old route can still land a
-- membership on the stub; making another subscription the catch-all does not
-- remove the memberships that already name it; and a session that disables
-- triggers (`session_replication_role`) is outside every rule here. Readers
-- use set semantics (EXISTS / UNION), so such a row is counted once.
--
-- Triggers on one table and event fire in name order, which is why the two
-- BEFORE INSERT triggers below are named `..._a_...` and `..._b_...`: the
-- skip must see the canonicalized subscription.
--
-- No backfill: nothing stored says which listings a second search delivered.

CREATE TABLE IF NOT EXISTS property_subscription (
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    search_profile_id INTEGER NOT NULL REFERENCES search_profiles(id),
    first_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT (NOW() AT TIME ZONE 'UTC'),
    source VARCHAR(16) NOT NULL DEFAULT 'ingest',
    PRIMARY KEY (property_id, search_profile_id),
    CONSTRAINT ck_property_subscription_source
        CHECK (source IN ('ingest', 'import', 'journal'))
);

CREATE INDEX IF NOT EXISTS ix_property_subscription_search_profile_id
    ON property_subscription(search_profile_id);

-- Rule 1.
DROP TRIGGER IF EXISTS trg_property_subscription_a_canonical_profile
    ON property_subscription;
CREATE TRIGGER trg_property_subscription_a_canonical_profile
    BEFORE INSERT ON property_subscription
    FOR EACH ROW EXECUTE FUNCTION canonicalize_search_profile();

-- Rule 3, the insert half.
CREATE OR REPLACE FUNCTION skip_redundant_property_subscription()
RETURNS trigger AS $$
DECLARE
    catch_all boolean;
    home integer;
BEGIN
    SELECT is_default INTO catch_all
        FROM search_profiles
        WHERE id = NEW.search_profile_id;
    IF catch_all IS TRUE THEN
        RETURN NULL;
    END IF;

    -- FOR SHARE, not FOR KEY SHARE: see rule 3 in the header.
    SELECT search_profile_id INTO home
        FROM properties
        WHERE id = NEW.property_id
        FOR SHARE;
    IF NOT FOUND THEN
        -- A listing this statement cannot see has no home to compare with,
        -- and the foreign key is checked only after the statement, by when
        -- another transaction may have committed it (review of #705). The
        -- refusal the foreign key would give, given here.
        RAISE EXCEPTION USING
            ERRCODE = 'foreign_key_violation',
            MESSAGE = 'property_subscription: listing ' || NEW.property_id
                || ' is not visible to this transaction (not committed yet, '
                || 'deleted, or never existed)';
    END IF;
    IF home IS NOT DISTINCT FROM NEW.search_profile_id THEN
        RETURN NULL;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_property_subscription_b_skip_redundant
    ON property_subscription;
CREATE TRIGGER trg_property_subscription_b_skip_redundant
    BEFORE INSERT ON property_subscription
    FOR EACH ROW EXECUTE FUNCTION skip_redundant_property_subscription();

-- A membership is never re-pointed.
CREATE OR REPLACE FUNCTION refuse_property_subscription_repoint()
RETURNS trigger AS $$
BEGIN
    -- The USING MESSAGE form, as in migration 014: the ordinary form takes
    -- percent placeholders, and a lone percent sign does not survive the
    -- driver.
    RAISE EXCEPTION USING
        ERRCODE = 'restrict_violation',
        MESSAGE = 'property_subscription (' || OLD.property_id || ', '
            || OLD.search_profile_id
            || ') cannot be re-pointed: a membership is inserted or deleted, '
            || 'never updated';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_property_subscription_key_is_never_updated
    ON property_subscription;
CREATE TRIGGER trg_property_subscription_key_is_never_updated
    BEFORE UPDATE OF property_id, search_profile_id ON property_subscription
    FOR EACH ROW EXECUTE FUNCTION refuse_property_subscription_repoint();

-- Rule 3, the home half.
CREATE OR REPLACE FUNCTION drop_property_subscription_equal_to_home()
RETURNS trigger AS $$
BEGIN
    -- The DELETE below sees what the statement's snapshot sees. Under READ
    -- COMMITTED that is everything committed before it, the membership the
    -- row lock made it wait for included. Under REPEATABLE READ or
    -- SERIALIZABLE it is the transaction's first snapshot, which can miss a
    -- membership committed since (review of #705), so a home MOVE is refused
    -- there instead of leaving that membership behind.
    IF NEW.search_profile_id IS DISTINCT FROM OLD.search_profile_id
       AND current_setting('transaction_isolation')
           NOT IN ('read committed', 'read uncommitted') THEN
        RAISE EXCEPTION USING
            ERRCODE = 'object_not_in_prerequisite_state',
            MESSAGE = 'properties ' || NEW.id || ': a home change runs at '
                || 'READ COMMITTED, not '
                || current_setting('transaction_isolation')
                || ' (property_subscription would miss a committed membership)';
    END IF;
    IF NEW.search_profile_id IS NOT NULL THEN
        DELETE FROM property_subscription
            WHERE property_id = NEW.id
              AND search_profile_id = NEW.search_profile_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_properties_home_drops_redundant_subscription
    ON properties;
CREATE TRIGGER trg_properties_home_drops_redundant_subscription
    AFTER UPDATE OF search_profile_id ON properties
    FOR EACH ROW EXECUTE FUNCTION drop_property_subscription_equal_to_home();

-- Rule 2.
CREATE OR REPLACE FUNCTION move_property_subscriptions_on_route()
RETURNS trigger AS $$
BEGIN
    INSERT INTO property_subscription
            (property_id, search_profile_id, first_seen_at, source)
        SELECT ps.property_id, NEW.routed_to, ps.first_seen_at, ps.source
            FROM property_subscription ps
            WHERE ps.search_profile_id = NEW.id
            ORDER BY ps.property_id
        ON CONFLICT DO NOTHING;

    DELETE FROM property_subscription
        WHERE search_profile_id = NEW.id;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_search_profiles_route_moves_subscriptions
    ON search_profiles;
CREATE TRIGGER trg_search_profiles_route_moves_subscriptions
    AFTER UPDATE OF routed_to ON search_profiles
    FOR EACH ROW
    WHEN (NEW.routed_to IS NOT NULL
          AND NEW.routed_to IS DISTINCT FROM OLD.routed_to)
    EXECUTE FUNCTION move_property_subscriptions_on_route();
