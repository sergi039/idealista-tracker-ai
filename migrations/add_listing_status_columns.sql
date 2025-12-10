-- Migration: Add listing status tracking columns to lands table
-- Run this SQL on your database to add listing status tracking

ALTER TABLE lands ADD COLUMN IF NOT EXISTS listing_status VARCHAR(20) DEFAULT 'active';
ALTER TABLE lands ADD COLUMN IF NOT EXISTS listing_removed_date TIMESTAMP;
ALTER TABLE lands ADD COLUMN IF NOT EXISTS listing_last_checked TIMESTAMP;

-- Create index for filtering by status
CREATE INDEX IF NOT EXISTS idx_lands_listing_status ON lands(listing_status);
