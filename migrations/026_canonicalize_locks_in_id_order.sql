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
-- WHAT THIS DOES NOT CLOSE — narrowed, not removed, and the difference is
-- the whole point of writing it down. The Tier 2 independent review found the
-- window and named the interleaving; it was then built, on 15.18, and it
-- deadlocks. Ids new-target=4, old-target=5, stub=6:
--
--   1. B begins an INSERT on the stub; its trigger reads routed_to = 5.
--   2. A route writer commits 6 -> 4 inside B's read-to-lock gap.
--   3. An ascending locker takes 4.
--   4. B locks {5, 6} ascending, re-reads 4, and its FK waits on 4.
--   5. The ascending locker asks for 6 and the cycle closes.
--
-- So the honest claim is NOT "cannot deadlock against an ascending locker".
-- It is: the inversion that needed no special conditions at all — every
-- insert against every route change — is gone, and what remains needs a route
-- change to land inside a gap that is microseconds wide AND a second
-- concurrent route writer. The natural-scheduling attempt did not reproduce;
-- only forcing the order with pg_sleep inside a copy of this body did.
--
-- It is not closed here because ordering cannot close it. The trigger's
-- stub-then-target order is forced by causality — the route cannot be trusted
-- until the stub is held, so the target lock is always second — which means
-- no ordering scheme resolving the route from inside the insert is fully
-- correct. Closing it needs a serialization primitive outside row locks, with
-- a granularity decision that trades the constant path's throughput against a
-- pair of writers that does not exist yet: `route_profile()` has no caller
-- outside the tests today. That decision is issue #513, by owner decision of
-- 2026-08-31 to ship the narrowing now and close the window separately.

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
