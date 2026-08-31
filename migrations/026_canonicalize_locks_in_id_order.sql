-- The insert path takes its profile locks in ascending id order, like every
-- other writer of `search_profiles` (owner request 2026-08-31).
--
-- WHAT 025 SHIPPED. `canonicalize_search_profile()` locked the STUB
-- (`FOR KEY SHARE`), rewrote `NEW.search_profile_id` to the route target, and
-- then the insert's own foreign-key check locked the TARGET. Two rows, in
-- stub-then-target order. Everything else that locks this table does so by
-- ascending id and says so in its own words: `lock_profiles_statement()`
-- ("the ORDER BY is the part that makes two concurrent runs safe"),
-- `_lock_and_regroup()`, `route_profile()` ("both rows are locked FOR UPDATE
-- in ascending id order"), and the repair service. When the stub's id is
-- HIGHER than its target's — which is the ordinary shape, since a stub is
-- created after the subscription it is routed into — stub-then-target IS
-- descending, and the two conventions are exact opposites.
--
-- MEASURED, not argued, on PostgreSQL 15.18 — the version `idealista-db`
-- runs, checked with `SHOW server_version` rather than assumed. The
-- reproduction is deterministic rather than load-based, so it answers "can
-- this cycle form" instead of "did it happen to today":
--
--   A: SELECT id = target FOR UPDATE     -- holds the LOW id
--   B: INSERT a listing on the stub      -- trigger takes the HIGH id, then
--                                        -- the FK wants the LOW one: B waits
--   A: SELECT id = stub   FOR UPDATE     -- wants the HIGH one: cycle closes
--
-- Every run: `A` dies with `deadlock detected`. With stub.id < target.id the
-- identical choreography never deadlocks, which is what pins the defect to
-- the ordering rather than to load.
--
-- THE FIX IS TO CONFORM, NOT TO INVENT A SECOND CONVENTION. Making
-- `route_profile()` lock stub-then-target instead would also remove this
-- pair's cycle, with no migration at all — and it would break the invariant
-- the rest of the table depends on, moving the deadlock to
-- `_lock_and_regroup()` instead. The constant path is the one that must
-- conform.
--
-- So: read the route WITHOUT a lock, take both rows `FOR KEY SHARE` in one
-- statement `ORDER BY id`, then re-read the route under that lock and use
-- what it says. The re-read is not decoration — the first read is a snapshot,
-- and a route writer may commit between the two statements. It cannot commit
-- after them: `FOR KEY SHARE` conflicts with `FOR UPDATE`, so once the stub
-- is held the route is frozen for the rest of this transaction. That is the
-- same serialization 025 wanted, obtained in an order that agrees with
-- everyone else.
--
-- WHAT THIS DOES NOT CLOSE, stated because a guard presented as complete is
-- worse than one known to be partial. If a route writer commits a DIFFERENT
-- target in the window between the unlocked read and the ordered lock, the
-- re-read names a row this statement did not lock, and the foreign key will
-- lock it afterwards — out of order if it sorts below the stub. Closing that
-- would mean holding a lock before knowing which row to take. It needs two
-- concurrent route writers to become a cycle, and `route_profile()` has no
-- caller outside the tests today; the systematic inversion on the path that
-- runs on every ingested listing is what this removes.

CREATE OR REPLACE FUNCTION canonicalize_search_profile() RETURNS trigger AS $$
DECLARE
    target integer;
BEGIN
    IF NEW.search_profile_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Unlocked, and only to learn which second row this write will need.
    SELECT routed_to INTO target
        FROM search_profiles
        WHERE id = NEW.search_profile_id;

    -- Both rows, one statement, ascending id: the convention the rest of the
    -- table already keeps. FOR KEY SHARE still blocks a route writer holding
    -- the row FOR UPDATE, so a listing inserted during a re-route waits for
    -- the decision exactly as it did before.
    PERFORM 1
        FROM search_profiles
        WHERE id = NEW.search_profile_id
           OR (target IS NOT NULL AND id = target)
        ORDER BY id
        FOR KEY SHARE;

    -- Under the lock the route can no longer move; the snapshot above could
    -- have been stale, so the value that decides is this one.
    SELECT routed_to INTO target
        FROM search_profiles
        WHERE id = NEW.search_profile_id;

    IF target IS NOT NULL THEN
        NEW.search_profile_id := target;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

-- The trigger itself is unchanged and is re-declared only so a database that
-- somehow lost it is repaired by this file too.
DROP TRIGGER IF EXISTS trg_properties_canonical_profile ON properties;
CREATE TRIGGER trg_properties_canonical_profile
    BEFORE INSERT OR UPDATE OF search_profile_id ON properties
    FOR EACH ROW
    EXECUTE FUNCTION canonicalize_search_profile();
