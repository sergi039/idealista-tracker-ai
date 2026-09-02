-- The catch-all cannot be hidden, and the database now says so (#533).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- THE INVARIANT. The default subscription receives every email that matches
-- nothing else, so hiding it takes listings off /properties as they arrive,
-- with nothing on screen saying where they went. Until now that held in the
-- UI alone: `set_profile_hidden` (routes/main_routes.py) refuses the toggle
-- on the default profile. Nothing refused
--
--     UPDATE search_profiles SET is_hidden = TRUE WHERE is_default;
--
-- and direct SQL through `docker exec ... psql` / `curate_on_mini.sh` is a
-- supported workflow here (property 774's review data arrived that way).
-- Migration 025 gave the ROUTING half of this same rule its backstop,
-- `ck_search_profiles_catch_all_never_routes`, and left the hiding half at
-- the UI. Two halves of one rule enforced at different layers is how one of
-- them ships half-enforced (found by the adversarial verification of #502,
-- CRIT-003 in #265, promoted to #533 on the owner's request).
--
-- THE SHAPE IS 025's, deliberately. `is_default IS NOT TRUE` rather than
-- `NOT is_default`: the column is nullable (migration 008), and a NULL there
-- is "not the catch-all" everywhere the app reads it -- `get_default_profile()`
-- asks `is_default = TRUE`, and both sibling CHECKs (013's
-- default-has-no-search-key, 025's catch-all-never-routes) spell it the same
-- way. A row whose `is_default` is NULL is nobody's catch-all and may be
-- hidden. `is_hidden IS NOT TRUE` mirrors `SearchProfileService.visible_clause()`
-- (`isnot(True)`) for the same reason, even though 020 made that column
-- NOT NULL: the two readings are meant to stay each other's complement.
--
-- A CHECK on a pair refuses the pair from either side: hiding the catch-all,
-- and making a hidden subscription the catch-all. `edit_profile` refuses the
-- second the way `set_profile_hidden` refuses the first, so the UI explains
-- what the database would otherwise answer with a 500.
--
-- DROP IF EXISTS then ADD, 025's idempotency: the runner applies a file once
-- and records its checksum, but the tests re-run the file by hand the way a
-- repaired database would, and it must not fail on a constraint it already
-- created -- nor lose it.
--
-- Nothing is rewritten. Production holds no hidden catch-all (read on
-- 2026-09-01: 0 rows with is_default AND is_hidden; the six routed Galicia
-- stubs are hidden and not default), so the constraint validates against the
-- existing rows and the deploy -- `python -m migrations.runner` before
-- gunicorn -- records 028 in schema_migrations. Had there been one, the deploy
-- would have FAILED and rolled back rather than silently keep a state the rule
-- forbids, which is the point of a backstop.

ALTER TABLE search_profiles
    DROP CONSTRAINT IF EXISTS ck_search_profiles_catch_all_never_hidden;
ALTER TABLE search_profiles
    ADD CONSTRAINT ck_search_profiles_catch_all_never_hidden
        CHECK (is_hidden IS NOT TRUE OR is_default IS NOT TRUE);
