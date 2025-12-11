-- Migration: Add performance indexes
-- Date: 2025-12-11
-- Description: Add indexes for frequently queried columns to improve performance

-- Indexes on lands table
CREATE INDEX IF NOT EXISTS ix_lands_land_type ON lands(land_type);
CREATE INDEX IF NOT EXISTS ix_lands_municipality ON lands(municipality);
CREATE INDEX IF NOT EXISTS ix_lands_listing_status ON lands(listing_status);
CREATE INDEX IF NOT EXISTS ix_lands_is_favorite ON lands(is_favorite);
CREATE INDEX IF NOT EXISTS ix_lands_created_at ON lands(created_at);
CREATE INDEX IF NOT EXISTS ix_lands_score_total ON lands(score_total);
CREATE INDEX IF NOT EXISTS ix_lands_score_investment ON lands(score_investment);
CREATE INDEX IF NOT EXISTS ix_lands_score_lifestyle ON lands(score_lifestyle);
