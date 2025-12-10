-- Migration: Create land_history table for tracking changes to favorite properties
-- Run this SQL on your database to add history tracking functionality

CREATE TABLE IF NOT EXISTS land_history (
    id SERIAL PRIMARY KEY,
    land_id INTEGER NOT NULL REFERENCES lands(id) ON DELETE CASCADE,
    snapshot_date TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Tracked fields
    price NUMERIC(10, 2),
    title TEXT,
    description TEXT,
    area NUMERIC(10, 2),
    land_type VARCHAR(20),
    url TEXT,

    -- Change metadata
    change_type VARCHAR(50) NOT NULL,
    -- Types: 'added_to_favorites', 'price_change', 'description_change', 'title_change', 'removed_from_listing', 'periodic_snapshot'

    -- Price change details
    price_previous NUMERIC(10, 2),
    price_change_amount NUMERIC(10, 2),
    price_change_percentage NUMERIC(5, 2)
);

-- Create indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_land_history_land_id ON land_history(land_id);
CREATE INDEX IF NOT EXISTS idx_land_history_snapshot_date ON land_history(snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_land_history_change_type ON land_history(change_type);
