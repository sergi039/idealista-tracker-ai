-- The old unnumbered manual migrations used idx_* names for indexes that are
-- already provided by the tracked ix_* indexes. Remove only those duplicates.
DROP INDEX IF EXISTS idx_lands_is_favorite;
DROP INDEX IF EXISTS idx_lands_listing_status;
DROP INDEX IF EXISTS idx_land_history_land_id;
DROP INDEX IF EXISTS idx_land_history_snapshot_date;
DROP INDEX IF EXISTS idx_land_history_change_type;
