-- Migration: Add price_updated_count and expired_count to sync_history
-- Date: 2024-12-11

-- Add price_updated_count column
ALTER TABLE sync_history ADD COLUMN IF NOT EXISTS price_updated_count INTEGER DEFAULT 0;

-- Add expired_count column
ALTER TABLE sync_history ADD COLUMN IF NOT EXISTS expired_count INTEGER DEFAULT 0;

-- Update existing records to have 0 values
UPDATE sync_history SET price_updated_count = 0 WHERE price_updated_count IS NULL;
UPDATE sync_history SET expired_count = 0 WHERE expired_count IS NULL;
