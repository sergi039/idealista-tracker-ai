-- What price an AI analysis was computed from (issue #235).
-- PostgreSQL-only migration, like every migration in this directory.
--
-- The prompt is built from the listing's price, and on 2026-08-11 twenty-two
-- prices were corrected (the €/m² defect, #220). Their stored analyses had been
-- computed against the wrong number -- both providers said so in their own
-- text -- and the page had no way to tell an analysis computed at €309 from one
-- computed at €99,000. Re-running costs real money and stays the owner's
-- decision; saying which price was used costs nothing.
--
-- NULL means "not recorded", which is every variant stored before this. The
-- page reads that as unknown and compares nothing: an invented comparison would
-- be the same defect this exists to remove.

ALTER TABLE property_ai_analysis_variants
    ADD COLUMN IF NOT EXISTS price_at_analysis NUMERIC(10, 2);

ALTER TABLE ai_analysis_variants
    ADD COLUMN IF NOT EXISTS price_at_analysis NUMERIC(10, 2);
