CREATE TABLE IF NOT EXISTS app_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(120) NOT NULL UNIQUE,
    value JSON NOT NULL DEFAULT '{}'::json,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_app_settings_key
    ON app_settings (key);

CREATE TABLE IF NOT EXISTS market_settings (
    id SERIAL PRIMARY KEY,
    construction_basic_min INTEGER DEFAULT 1100,
    construction_basic_avg INTEGER DEFAULT 1300,
    construction_basic_max INTEGER DEFAULT 1500,
    construction_premium_min INTEGER DEFAULT 1500,
    construction_premium_avg INTEGER DEFAULT 1800,
    construction_premium_max INTEGER DEFAULT 2200,
    purchase_costs_ratio NUMERIC(4, 3) DEFAULT 0.10,
    urban_vacancy_rate NUMERIC(4, 3) DEFAULT 0.05,
    urban_operating_expenses NUMERIC(4, 3) DEFAULT 0.15,
    urban_management_fee NUMERIC(4, 3) DEFAULT 0.00,
    suburban_vacancy_rate NUMERIC(4, 3) DEFAULT 0.08,
    suburban_operating_expenses NUMERIC(4, 3) DEFAULT 0.15,
    suburban_management_fee NUMERIC(4, 3) DEFAULT 0.00,
    rural_vacancy_rate NUMERIC(4, 3) DEFAULT 0.20,
    rural_operating_expenses NUMERIC(4, 3) DEFAULT 0.18,
    rural_management_fee NUMERIC(4, 3) DEFAULT 0.10,
    urban_rental_min INTEGER DEFAULT 9,
    urban_rental_avg INTEGER DEFAULT 11,
    urban_rental_max INTEGER DEFAULT 13,
    suburban_rental_min INTEGER DEFAULT 7,
    suburban_rental_avg INTEGER DEFAULT 9,
    suburban_rental_max INTEGER DEFAULT 11,
    rural_rental_min INTEGER DEFAULT 5,
    rural_rental_avg INTEGER DEFAULT 7,
    rural_rental_max INTEGER DEFAULT 9,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
