-- One subscription on screen, criteria enforced app-side (issue #498 follow-up,
-- owner request 2026-08-31). PostgreSQL-only migration, like every one here.
--
-- THE ROUTE. `routed_to` sends a subscription's listings to another
-- subscription: the six idealista Galicia alerts each create a per-search
-- profile (the #102 identity design — one source_search_key per profile), the
-- owner wants ONE chip, and hiding alone is not enough because listings keep
-- landing on the stubs. The stub keeps its saved-search identity; its rows
-- live on the target.
--
-- Enforcement is a BEFORE trigger on `properties`, not a rule in every
-- writer, because writers are legion: resolve_profile, the paste-links
-- import, three portal email doors, run_full_sync, the Land mirror, and
-- curation SQL through docker exec. The trigger is the one layer they all
-- share. It reads the route under FOR KEY SHARE so a transaction holding the
-- profile row FOR UPDATE while setting a route serializes with every
-- concurrent listing insert: the insert waits for the route decision, then
-- canonicalizes (the plan-gate reviewer's own alternative, round 3).
-- Exactly ONE hop: chains are refused at write time by the route writer
-- (services/search_profile_service.route_profile), in both directions.
--
-- The CHECKs are the same philosophy as ck_search_profiles_default_has_no_
-- search_key: enforced in the database because read-side discipline does not
-- survive the fifth writer. A profile may not route to itself; the catch-all
-- may never route (it receives everything that matches nothing else, and
-- redirecting it would silently move ALL unmatched mail); a routed stub may
-- not carry an auto-route pattern (a pattern on a stub would chain).
--
-- `auto_route_from_pattern` lives on the TARGET: when ingestion auto-creates
-- a profile (identity or label path) whose name matches the pattern, the new
-- profile is born routed and hidden — this is what keeps the four Galicia
-- alerts that have not delivered yet from each putting a chip back on the
-- screen at first email.
--
-- `criteria` holds the owner's app-side requirements ({"min_house_m2": 150,
-- "min_plot_m2": 700}) — the portals cannot encode a plot filter, so the
-- filter lives here. `properties.plot_area` is a real column, not a JSON
-- key, because the criteria verdict filters on it in SQL and a cast into
-- hand-editable JSON is the hazard-service lesson. NULL means nobody
-- measured it (#98); the fotocasa payload carries it, others mostly cannot.

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS routed_to INTEGER
        REFERENCES search_profiles(id);

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS auto_route_from_pattern VARCHAR(120);

ALTER TABLE search_profiles
    ADD COLUMN IF NOT EXISTS criteria JSON;

ALTER TABLE search_profiles
    DROP CONSTRAINT IF EXISTS ck_search_profiles_route_not_self;
ALTER TABLE search_profiles
    ADD CONSTRAINT ck_search_profiles_route_not_self
        CHECK (routed_to IS NULL OR routed_to <> id);

ALTER TABLE search_profiles
    DROP CONSTRAINT IF EXISTS ck_search_profiles_catch_all_never_routes;
ALTER TABLE search_profiles
    ADD CONSTRAINT ck_search_profiles_catch_all_never_routes
        CHECK (routed_to IS NULL OR is_default IS NOT TRUE);

ALTER TABLE search_profiles
    DROP CONSTRAINT IF EXISTS ck_search_profiles_stub_has_no_pattern;
ALTER TABLE search_profiles
    ADD CONSTRAINT ck_search_profiles_stub_has_no_pattern
        CHECK (routed_to IS NULL OR auto_route_from_pattern IS NULL);

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS plot_area NUMERIC(10,2)
        CONSTRAINT ck_properties_plot_area_nonnegative
        CHECK (plot_area IS NULL OR plot_area >= 0);

CREATE OR REPLACE FUNCTION canonicalize_search_profile() RETURNS trigger AS $$
DECLARE
    target integer;
BEGIN
    IF NEW.search_profile_id IS NULL THEN
        RETURN NEW;
    END IF;
    -- FOR KEY SHARE: blocks while a route writer holds the row FOR UPDATE,
    -- so a listing being inserted during a re-route waits for the decision
    -- instead of racing past it. One hop only — the route writer refuses
    -- chains, and a single hop here means a hand-made chain misroutes one
    -- listing rather than looping the database.
    SELECT routed_to INTO target
        FROM search_profiles
        WHERE id = NEW.search_profile_id
        FOR KEY SHARE;
    IF target IS NOT NULL THEN
        NEW.search_profile_id := target;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_properties_canonical_profile ON properties;
CREATE TRIGGER trg_properties_canonical_profile
    BEFORE INSERT OR UPDATE OF search_profile_id ON properties
    FOR EACH ROW
    EXECUTE FUNCTION canonicalize_search_profile();
