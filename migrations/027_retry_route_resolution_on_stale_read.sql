-- Close the route-resolution window migration 026 narrowed (#513): when the
-- trigger discovers its unlocked route read went stale, it RELEASES the stale
-- pair and resolves afresh, instead of proceeding into a lock-order inversion.
--
-- WHAT 026 LEFT OPEN. `canonicalize_search_profile()` must read `routed_to`
-- before it can know which second row to lock, so the read is unlocked and
-- the ascending pair lock follows. A route change committing inside that
-- read-to-lock gap left the trigger holding a STALE pair while the insert's
-- foreign key locked the fresh target afterwards — out of ascending order
-- whenever the fresh target's id sorts below the stub's. Reproduced on
-- PostgreSQL 15 (issue #513, ids new-target=4, old-target=5, stub=6): B locks
-- {5,6}, re-reads 4, its FK waits on 4 held by an ascending FOR UPDATE
-- locker, the locker asks for 6, deadlock. 026's own comment records why
-- ordering alone cannot close it: the route cannot be trusted until the stub
-- is held, so any scheme that resolves the route from inside the insert
-- reads before it locks.
--
-- TWO DESIGNS DIED IN REVIEW BEFORE THIS ONE, and both kill-chains are
-- recorded here so they are not re-proposed.
--
-- Advisory locks (issue #513's candidate C, any granularity, any lock mode):
-- a BEFORE ROW UPDATE trigger fires only after the target tuple is already
-- locked (GetTupleForTrigger takes the tuple lock first), so a lock acquired
-- inside this trigger is acquired LATE — behind whatever the reassigning
-- transaction already holds. The merge and repair paths reassign
-- `search_profile_id` while holding `search_profiles` FOR UPDATE locks taken
-- earlier in the same transaction, on ingestion's own path, so an
-- advisory-first route writer would deadlock against them DETERMINISTICALLY —
-- a certain deadlock on live paths to close a microsecond window. Statement
-- triggers do not save it: any multi-statement transaction's earlier locks
-- re-form the cycle, and participation cannot be enforced at the database
-- for the same reason (`route_profile()` row-locks before its UPDATE fires
-- any trigger).
--
-- Deferring: the round-2 brief claimed row locks cannot be released
-- mid-transaction, and that claim is FALSE. PostgreSQL 15 docs, 13.3.2,
-- verbatim: "Row-level locks are released at transaction end or during
-- savepoint rollback, just like table-level locks." A PL/pgSQL EXCEPTION
-- block is a subtransaction, so a trigger CAN undo a stale acquisition.
-- That mechanism — the independent reviewer's own candidate — is this
-- migration.
--
-- HOW IT WORKS. Each attempt reads the route unlocked (a guess), then, inside
-- a sentinel-exception subtransaction, locks the pair ascending FOR KEY SHARE
-- and re-reads. Stable (re-read equals guess, NULL-safe) means the held state
-- is one ascending pair whose route is frozen against the FOR UPDATE protocol
-- writers, with the FK target inside it — 026's safe state, now guaranteed:
-- the block exits normally, its subtransaction commits, and its locks
-- TRANSFER TO THE PARENT, so 025's wait-for-the-decision serialization
-- survives to the transaction's end. Stale means RAISE the sentinel: the
-- subtransaction rolls back, the stale pair is RELEASED — which is what
-- breaks any half-formed cycle, since at that instant this transaction holds
-- no search_profiles lock THAT THIS TRIGGER TOOK — a transaction that locked
-- profiles earlier (the merge and repair paths do, FOR UPDATE) still holds
-- those, and the rollback cannot reach them — and the loop resolves afresh.
--
-- Closure is entirely READER-side. It does not matter what wrote the route:
-- `route_profile()`, a future UI writer, or raw SQL through docker exec. No
-- writer participates in anything, so there is no participation to forget.
-- And no new lock object exists, so no new cycle can either: every attempt
-- begins holding nothing on search_profiles that this trigger took (locks
-- the surrounding transaction acquired before the trigger fired are its own
-- and stay held — the release cannot reach them), the locks taken here are
-- the same KEY SHAREs 026 took at the same points, and the state held at
-- trigger exit is a strict subset of the states 026 could reach.
--
-- THE CLAIM, SCOPED HONESTLY. What is closed is the stale-pair DEADLOCK
-- window, for every writer. What is unchanged is who gets the
-- wait-for-the-decision serialization: FOR KEY SHARE conflicts only with
-- FOR UPDATE, so a bare `UPDATE search_profiles SET routed_to = ...` (which
-- takes FOR NO KEY UPDATE) was never blocked by this trigger — under 025,
-- 026, or now. A raw writer that re-points a routed stub without the
-- FOR UPDATE protocol splits that stub's listings between the old and new
-- targets; that is exactly the split `route_profile()`'s
-- `source_already_routed` refusal exists to refuse, it is that writer's own
-- choice, and this migration neither worsens nor repairs it.
--
-- EXHAUSTION FALLS THROUGH, NEVER REFUSES. Six attempts; the sixth proceeds
-- with whatever the re-read says even if the guess went stale again, which is
-- 026's exact behavior. Reaching it takes five route flips each landing
-- inside a microseconds-wide gap of one insert — and if that ever happens,
-- the failure mode is 026's loud deadlock, never a misfile, because the
-- re-read still decides where the listing lands. A new deterministic failure
-- mode (refusing the insert) would be strictly worse than the residual
-- probabilistic one being removed; do not "harden" this into a RAISE.
--
-- THE SENTINEL CATCH IS EXACT. Only SQLSTATE 'RT513' is caught. A
-- deadlock_detected, lock_timeout or anything else raised inside the block
-- propagates and aborts the insert exactly as before — a broader catch would
-- silently convert real errors into retries and hide the very signal this
-- issue is about.
--
-- WHAT IT COSTS, so nobody re-measures in surprise: locking a row assigns
-- the subtransaction an xid, so every properties insert or reassignment row
-- now spends one subtransaction and one extra xid (immeasurable at this
-- deployment's volume), and a PL/pgSQL block with an EXCEPTION clause is
-- documented as significantly more expensive to enter than one without —
-- still microseconds against the INSERT it serves. A bulk single-commit
-- transaction (the legacy Land migration, `route_profile()`'s listing move)
-- assigns one subxid per row; past 64 the backend's snapshot subxid cache
-- overflows and concurrent snapshots degrade to SLRU lookups while it runs —
-- a real pathology on a busy system, a non-event on this single-owner one,
-- named here so a future high-traffic reader is not ambushed.

CREATE OR REPLACE FUNCTION canonicalize_search_profile() RETURNS trigger AS $$
DECLARE
    target integer;
    fresh integer;
BEGIN
    IF NEW.search_profile_id IS NULL THEN
        RETURN NEW;
    END IF;

    FOR attempt IN 1..6 LOOP
        -- Unlocked, and only to learn which second row this write will need.
        SELECT routed_to INTO target
            FROM search_profiles
            WHERE id = NEW.search_profile_id;

        -- This block is a subtransaction: locks born inside it die with its
        -- rollback, which is the whole mechanism (PG docs 13.3.2).
        BEGIN
            -- Both rows, one statement, ascending id: the convention the
            -- rest of the table keeps (026). FOR KEY SHARE still blocks a
            -- route writer holding the row FOR UPDATE, so a listing inserted
            -- during a re-route waits for the decision exactly as before.
            PERFORM 1
                FROM search_profiles
                WHERE id = NEW.search_profile_id
                   OR (target IS NOT NULL AND id = target)
                ORDER BY id
                FOR KEY SHARE;

            SELECT routed_to INTO fresh
                FROM search_profiles
                WHERE id = NEW.search_profile_id;

            -- Attempt 6 proceeds regardless: 026's exact semantics, the
            -- deliberate fall-through documented above.
            IF attempt = 6 OR fresh IS NOT DISTINCT FROM target THEN
                IF fresh IS NOT NULL THEN
                    NEW.search_profile_id := fresh;
                END IF;
                RETURN NEW;
            END IF;

            -- Stale: a route change landed inside the read-to-lock gap.
            -- Rolling back this subtransaction releases the stale pair.
            RAISE EXCEPTION USING ERRCODE = 'RT513';
        EXCEPTION WHEN SQLSTATE 'RT513' THEN
            NULL;  -- pair released; loop and resolve afresh
        END;
    END LOOP;

    -- Unreachable (attempt 6 always returns); PL/pgSQL wants a terminal
    -- return anyway.
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
