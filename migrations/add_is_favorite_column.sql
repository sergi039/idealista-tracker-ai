-- Migration: Add is_favorite column to lands table
-- Run this SQL on your database to add favorites functionality

ALTER TABLE lands ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE;

-- Create index for faster filtering by favorites
CREATE INDEX IF NOT EXISTS idx_lands_is_favorite ON lands(is_favorite) WHERE is_favorite = TRUE;
