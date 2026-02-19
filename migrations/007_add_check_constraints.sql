-- Migration 007: Add CHECK constraints for data integrity
-- Safe to run on existing data: constraints use OR NULL pattern.
-- Run against the live PostgreSQL database:
--   psql -h localhost -p 5433 -U idealista -d idealista -f migrations/007_add_check_constraints.sql

-- Price and area
ALTER TABLE lands ADD CONSTRAINT ck_lands_price_non_negative CHECK (price IS NULL OR price >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_area_non_negative CHECK (area IS NULL OR area >= 0);

-- Coordinates (WGS84 bounds)
ALTER TABLE lands ADD CONSTRAINT ck_lands_lat_range CHECK (location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90));
ALTER TABLE lands ADD CONSTRAINT ck_lands_lon_range CHECK (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180));

-- Scores [0, 100]
ALTER TABLE lands ADD CONSTRAINT ck_lands_score_total_range CHECK (score_total IS NULL OR (score_total >= 0 AND score_total <= 100));
ALTER TABLE lands ADD CONSTRAINT ck_lands_score_investment_range CHECK (score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100));
ALTER TABLE lands ADD CONSTRAINT ck_lands_score_lifestyle_range CHECK (score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100));

-- Travel times >= 0
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_oviedo CHECK (travel_time_oviedo IS NULL OR travel_time_oviedo >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_gijon CHECK (travel_time_gijon IS NULL OR travel_time_gijon >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_beach CHECK (travel_time_nearest_beach IS NULL OR travel_time_nearest_beach >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_airport CHECK (travel_time_airport IS NULL OR travel_time_airport >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_train CHECK (travel_time_train_station IS NULL OR travel_time_train_station >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_hospital CHECK (travel_time_hospital IS NULL OR travel_time_hospital >= 0);
ALTER TABLE lands ADD CONSTRAINT ck_lands_tt_police CHECK (travel_time_police IS NULL OR travel_time_police >= 0);

-- Listing status enum
ALTER TABLE lands ADD CONSTRAINT ck_lands_listing_status_enum CHECK (listing_status IN ('active', 'removed', 'sold', 'unknown'));
