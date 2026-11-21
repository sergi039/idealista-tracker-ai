-- A listing may belong to more than one subscription (#632): the ADDITIONAL
-- memberships live here. `properties.search_profile_id` stays the row's one
-- HOME -- scoring, travel targets, criteria, taste and similarity all read
-- that column and must stay single-valued -- and a row in this table says only
-- "this subscription delivered it too".
--
-- WHY A TABLE. A portal listing is one row whichever door it comes through
-- (`<source>:<id>` is UNIQUE), so a second saved search that delivers it finds
-- the row and creates nothing. Until #632 it also recorded nothing: measured
-- on production 2026-11-21, 2501 portal rows and no way to tell from anything
-- stored how many of them a second search had delivered. A second row per
-- subscription (what the idealista path does) would split the owner's
-- verdict, favourite, timeline, attachments and paid enrichment across twins
-- that are never deleted; a JSON list could not be filtered without casting
-- stored JSON, and could not be kept on the right side of a route.
--
-- THIS MIGRATION ADDS THE TABLE AND ITS INVARIANTS AND NOTHING READS IT YET.
-- No writer exists until the next change, so the table is empty after this
-- file and every listing surface behaves exactly as before.
--
-- THE INVARIANTS ARE THE DATABASE'S, NOT A WRITER'S. Curation SQL through
-- `docker exec` is a supported workflow, and the recovery path after a bad
-- deploy is a roll-forward revert whose code may no longer know this table
-- (the runner refuses to start an image that does not know an applied
-- migration, so the previous image is never the recovery). Four rules, each
-- enforced below whoever writes:
--
-- 1. A membership never points at a routed stub. The properties trigger's own
--    function, `canonicalize_search_profile()` (migrations 025-027), runs on
--    this table unchanged: it only reads and rewrites `NEW.search_profile_id`,
--    under the same ascending FOR KEY SHARE pair, so a membership written
--    against a stub lands on the stub's target and waits for a route decision
--    in flight exactly as a listing insert does. A BEFORE INSERT trigger runs
--    before ON CONFLICT picks its arbiter row, so `ON CONFLICT DO NOTHING`
--    sees the rewritten key.
-- 2. A route moves the stub's memberships with it, in the routing statement's
--    own transaction: AFTER UPDATE OF routed_to deletes the ones the target
--    already has (as the listing's home or as a membership) and re-points the
--    rest. "A stub holds nothing" stays true for both tables.
-- 3. A membership equal to the listing's home, or naming the catch-all, is
--    not stored. An insert of one is skipped (RETURN NULL: not an error, the
--    fact is already true or names no search at all), and a home that moves
--    onto a subscription the listing was already a member of drops that
--    membership. INSERT only for the skip, on purpose: skipping an UPDATE
--    would leave the old row in place, which for the route move in rule 2
--    means leaving a membership on a stub.
-- 4. A subscription that still holds a membership cannot be deleted: the
--    profile-side foreign key is NO ACTION. The two code paths that delete a
--    profile move or count memberships first; anything else is refused loudly
--    instead of dropping a membership silently.
--
-- WHAT IS NOT CLOSED, stated so nobody assumes it: a raw
-- `UPDATE search_profiles SET routed_to` without the FOR UPDATE protocol was
-- never serialized against the canonicalizing trigger (027 says so for
-- `properties`), so an insert that read the old route can still land a
-- membership on the stub; and the "equal to home" skip reads the home
-- unlocked, so a home move racing a membership insert can leave one redundant
-- row. Both are harmless to readers, which use set semantics (EXISTS / UNION),
-- and the next home write or route removes the row.
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
    BEFORE INSERT OR UPDATE OF search_profile_id ON property_subscription
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

    SELECT search_profile_id INTO home
        FROM properties
        WHERE id = NEW.property_id;
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

-- Rule 3, the home half.
CREATE OR REPLACE FUNCTION drop_property_subscription_equal_to_home()
RETURNS trigger AS $$
BEGIN
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
    DELETE FROM property_subscription ps
        WHERE ps.search_profile_id = NEW.id
          AND (
              EXISTS (
                  SELECT 1 FROM properties p
                  WHERE p.id = ps.property_id
                    AND p.search_profile_id = NEW.routed_to
              )
              OR EXISTS (
                  SELECT 1 FROM property_subscription other
                  WHERE other.property_id = ps.property_id
                    AND other.search_profile_id = NEW.routed_to
              )
          );

    UPDATE property_subscription
        SET search_profile_id = NEW.routed_to
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
