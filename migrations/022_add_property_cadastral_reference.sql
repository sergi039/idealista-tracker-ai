-- The cadastral reference of the parcel behind a listing (issue #430).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- A cadastral reference is the single most load-bearing fact a listing can
-- carry: with it the parcel's outline, its planning class and its surface
-- become checkable, and the shape of that outline is what actually decided
-- property 774 -- it fills 0.35 of its bounding box and the owner wants a
-- regular plot. Until now the reference arrived by WhatsApp and had nowhere
-- to live.
--
-- A column and not a key inside `enrichment`, for three reasons. It is what
-- every later check keys on, so it is looked up and not just read. It is
-- typed by a human off a document, so it wants a length the database
-- enforces. And `enrichment` is one JSON column every writer read-modify-
-- writes (#339), which is the wrong place for the one field somebody types.
--
-- The measurement itself -- the outline, the metrics, the class, the
-- subparcels -- does live in `enrichment["cadastre"]`, written under the same
-- lock as this column by `services/cadastre_service.apply_to_property`, so a
-- row can never name one parcel in the column and describe another in the
-- block.
--
-- 20 characters: Catastro's services accept 14 (the parcel), 18 and 20 (one
-- unit within it), and the printed form on a ficha catastral is the 20.
-- Nothing is backfilled and nothing is inferred -- a listing whose advert
-- never named a parcel has no reference, and NULL says exactly that.

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS cadastral_reference VARCHAR(20);

-- Looked up by reference when a document arrives naming one: "which listing
-- is this parcel?" is the question a ficha catastral asks.
CREATE INDEX IF NOT EXISTS ix_properties_cadastral_reference
    ON properties(cadastral_reference);
