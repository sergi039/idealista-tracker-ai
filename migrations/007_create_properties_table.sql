-- Create universal "properties" table (sale-first, Spain-wide).
-- PostgreSQL-only migration.

CREATE TABLE IF NOT EXISTS properties (
    id SERIAL PRIMARY KEY,

    source_email_id VARCHAR(255) NOT NULL UNIQUE,
    idealista_property_id BIGINT,
    email_subject TEXT,
    email_sender VARCHAR(255),

    title TEXT,
    url TEXT,

    deal_type VARCHAR(16) DEFAULT 'sale',
    property_category VARCHAR(32),
    property_subtype VARCHAR(64),

    price NUMERIC(10, 2),
    currency VARCHAR(3) DEFAULT 'EUR',
    area NUMERIC(10, 2),
    area_type VARCHAR(16) DEFAULT 'unknown',

    municipality VARCHAR(255),
    location_lat NUMERIC(10, 7),
    location_lon NUMERIC(10, 7),
    location_accuracy VARCHAR(20) DEFAULT 'unknown',

    description TEXT,

    attributes JSON,
    property_details JSON,
    enrichment JSON,
    travel JSON,
    scoring JSON,
    ai_analysis JSON,
    enhanced_description JSON,

    score_total NUMERIC(5, 2),
    score_investment NUMERIC(5, 2),
    score_lifestyle NUMERIC(5, 2),

    previous_price NUMERIC(10, 2),
    price_change_amount NUMERIC(10, 2),
    price_change_percentage NUMERIC(5, 2),
    price_changed_date TIMESTAMP,

    is_favorite BOOLEAN DEFAULT FALSE,

    listing_status VARCHAR(20) DEFAULT 'active',
    listing_removed_date TIMESTAMP,
    listing_last_checked TIMESTAMP,

    created_at TIMESTAMP DEFAULT now(),
    email_date TIMESTAMP,
    updated_at TIMESTAMP DEFAULT now(),

    -- Data integrity constraints
    CONSTRAINT ck_properties_price_non_negative CHECK (price IS NULL OR price >= 0),
    CONSTRAINT ck_properties_area_non_negative CHECK (area IS NULL OR area >= 0),
    CONSTRAINT ck_properties_lat_range CHECK (location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)),
    CONSTRAINT ck_properties_lon_range CHECK (location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180)),
    CONSTRAINT ck_properties_score_total_range CHECK (score_total IS NULL OR (score_total >= 0 AND score_total <= 100)),
    CONSTRAINT ck_properties_score_investment_range CHECK (score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100)),
    CONSTRAINT ck_properties_score_lifestyle_range CHECK (score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100)),
    CONSTRAINT ck_properties_listing_status_enum CHECK (listing_status IN ('active', 'removed', 'sold', 'unknown'))
);

CREATE INDEX IF NOT EXISTS ix_properties_idealista_property_id
    ON properties (idealista_property_id);

CREATE INDEX IF NOT EXISTS ix_properties_property_category
    ON properties (property_category);

CREATE INDEX IF NOT EXISTS ix_properties_property_subtype
    ON properties (property_subtype);

CREATE INDEX IF NOT EXISTS ix_properties_municipality
    ON properties (municipality);

CREATE INDEX IF NOT EXISTS ix_properties_listing_status
    ON properties (listing_status);

CREATE INDEX IF NOT EXISTS ix_properties_is_favorite
    ON properties (is_favorite);

CREATE INDEX IF NOT EXISTS ix_properties_created_at
    ON properties (created_at);

CREATE INDEX IF NOT EXISTS ix_properties_score_total
    ON properties (score_total);

CREATE INDEX IF NOT EXISTS ix_properties_score_investment
    ON properties (score_investment);

CREATE INDEX IF NOT EXISTS ix_properties_score_lifestyle
    ON properties (score_lifestyle);

