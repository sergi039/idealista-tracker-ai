-- Baseline for the legacy lands model. Keep the existing INTEGER/SERIAL primary
-- key type: deployed databases and foreign keys depend on it.
CREATE TABLE IF NOT EXISTS lands (
    id SERIAL PRIMARY KEY,
    source_email_id VARCHAR(255) NOT NULL UNIQUE,
    idealista_property_id BIGINT,
    email_subject TEXT,
    email_sender VARCHAR(255),
    title TEXT,
    url TEXT,
    price NUMERIC(10, 2),
    area NUMERIC(10, 2),
    municipality VARCHAR(255),
    location_lat NUMERIC(10, 7),
    location_lon NUMERIC(10, 7),
    location_accuracy VARCHAR(20) DEFAULT 'unknown',
    land_type VARCHAR(20),
    description TEXT,
    infrastructure_basic JSON,
    infrastructure_extended JSON,
    transport JSON,
    environment JSON,
    neighborhood JSON,
    services_quality JSON,
    legal_status VARCHAR(50),
    property_details JSON,
    ai_analysis JSON,
    enhanced_description JSON,
    score_total NUMERIC(5, 2),
    score_investment NUMERIC(5, 2),
    score_lifestyle NUMERIC(5, 2),
    travel_time_oviedo INTEGER,
    travel_time_gijon INTEGER,
    travel_time_nearest_beach INTEGER,
    nearest_beach_name VARCHAR(255),
    travel_time_airport INTEGER,
    travel_time_train_station INTEGER,
    travel_time_hospital INTEGER,
    travel_time_police INTEGER,
    distance_airport INTEGER,
    distance_train_station INTEGER,
    distance_hospital INTEGER,
    distance_police INTEGER,
    previous_price NUMERIC(10, 2),
    price_change_amount NUMERIC(10, 2),
    price_change_percentage NUMERIC(5, 2),
    price_changed_date TIMESTAMP WITHOUT TIME ZONE,
    is_favorite BOOLEAN DEFAULT FALSE,
    listing_status VARCHAR(20) DEFAULT 'active',
    listing_removed_date TIMESTAMP WITHOUT TIME ZONE,
    listing_last_checked TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    email_date TIMESTAMP WITHOUT TIME ZONE,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_lands_land_type_enum
        CHECK (land_type IS NULL OR land_type IN ('developed', 'buildable')),
    CONSTRAINT ck_lands_price_non_negative
        CHECK (price IS NULL OR price >= 0),
    CONSTRAINT ck_lands_area_non_negative
        CHECK (area IS NULL OR area >= 0),
    CONSTRAINT ck_lands_lat_range
        CHECK (location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)),
    CONSTRAINT ck_lands_lon_range
        CHECK (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180)),
    CONSTRAINT ck_lands_score_total_range
        CHECK (score_total IS NULL OR (score_total >= 0 AND score_total <= 100)),
    CONSTRAINT ck_lands_score_investment_range
        CHECK (score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100)),
    CONSTRAINT ck_lands_score_lifestyle_range
        CHECK (score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100)),
    CONSTRAINT ck_lands_tt_oviedo
        CHECK (travel_time_oviedo IS NULL OR travel_time_oviedo >= 0),
    CONSTRAINT ck_lands_tt_gijon
        CHECK (travel_time_gijon IS NULL OR travel_time_gijon >= 0),
    CONSTRAINT ck_lands_tt_beach
        CHECK (travel_time_nearest_beach IS NULL OR travel_time_nearest_beach >= 0),
    CONSTRAINT ck_lands_tt_airport
        CHECK (travel_time_airport IS NULL OR travel_time_airport >= 0),
    CONSTRAINT ck_lands_tt_train
        CHECK (travel_time_train_station IS NULL OR travel_time_train_station >= 0),
    CONSTRAINT ck_lands_tt_hospital
        CHECK (travel_time_hospital IS NULL OR travel_time_hospital >= 0),
    CONSTRAINT ck_lands_tt_police
        CHECK (travel_time_police IS NULL OR travel_time_police >= 0),
    CONSTRAINT ck_lands_listing_status_enum
        CHECK (listing_status IN ('active', 'removed', 'sold', 'unknown'))
);

-- These columns originally lived in unnumbered manual scripts. Keep the
-- baseline safe for legacy databases where the lands table predates them.
ALTER TABLE lands ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE;
ALTER TABLE lands ADD COLUMN IF NOT EXISTS listing_status VARCHAR(20) DEFAULT 'active';
ALTER TABLE lands ADD COLUMN IF NOT EXISTS listing_removed_date TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE lands ADD COLUMN IF NOT EXISTS listing_last_checked TIMESTAMP WITHOUT TIME ZONE;
