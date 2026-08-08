-- Add CHECK constraints to existing lands and properties tables.
-- Idempotent: uses DO $$ blocks to skip if constraint already exists.
-- PostgreSQL-only migration.

-- ============================================================
-- LANDS table constraints
-- ============================================================

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_land_type_enum
        CHECK (land_type IS NULL OR land_type IN ('developed', 'buildable'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_price_non_negative
        CHECK (price IS NULL OR price >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_area_non_negative
        CHECK (area IS NULL OR area >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_lat_range
        CHECK (location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_lon_range
        CHECK (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_score_total_range
        CHECK (score_total IS NULL OR (score_total >= 0 AND score_total <= 100));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_score_investment_range
        CHECK (score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_score_lifestyle_range
        CHECK (score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_oviedo
        CHECK (travel_time_oviedo IS NULL OR travel_time_oviedo >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_gijon
        CHECK (travel_time_gijon IS NULL OR travel_time_gijon >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_beach
        CHECK (travel_time_nearest_beach IS NULL OR travel_time_nearest_beach >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_airport
        CHECK (travel_time_airport IS NULL OR travel_time_airport >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_train
        CHECK (travel_time_train_station IS NULL OR travel_time_train_station >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_hospital
        CHECK (travel_time_hospital IS NULL OR travel_time_hospital >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_police
        CHECK (travel_time_police IS NULL OR travel_time_police >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE lands ADD CONSTRAINT ck_lands_listing_status_enum
        CHECK (listing_status IN ('active', 'removed', 'sold', 'unknown'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- PROPERTIES table constraints
-- ============================================================

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_price_non_negative
        CHECK (price IS NULL OR price >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_area_non_negative
        CHECK (area IS NULL OR area >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_lat_range
        CHECK (location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_lon_range
        CHECK (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_score_total_range
        CHECK (score_total IS NULL OR (score_total >= 0 AND score_total <= 100));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_score_investment_range
        CHECK (score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_score_lifestyle_range
        CHECK (score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE properties ADD CONSTRAINT ck_properties_listing_status_enum
        CHECK (listing_status IN ('active', 'removed', 'sold', 'unknown'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
