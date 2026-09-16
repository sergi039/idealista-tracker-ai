# Standing backlog

Findings that are real but were not fixed where they were found. One entry
each: a searchable anchor (never a line number), what breaks and for whom if
it is never fixed, and the measurement behind it. The owner decides what
becomes an issue; an entry is not a task list.

This is a tracked file because the issue that used to hold it, #265
("Standing backlog (permanent — do not close)"), is closed — and a closed
issue's comments are where findings go to be lost. A file cannot be closed out
from under the rule that points at it. A `fixed:` note names the commit, which
must be an ancestor of `main`.

## Open

### 46 houses in subscription 21 are filed as bare land

- **Anchor:** `utils/import_research_sheet.py` (`classify_sources` fed the
  sheet's `Type` column); `PropertyScoringService.scorer_for`;
  `subscription_criteria.effective_figures` / `listing_kind`.
- **Measured 2026-09-08, re-measured 2026-09-16:** subscription 21
  "Oriente · casas con finca ≥1000 m²" holds 46 rows with
  `property_category='land'` and `area_type='built'`, every one from
  `research_sheet` (sheet `oriente_houses_2026-08-22`). None carries
  finca/terreno/parcela/solar in its title, the research notes read "Готов к
  жилью" / "Реформа", and 7 state a built area beside a separate plot
  (868: 400 m² on 13,500). Table-wide the mismatch is 52 rows; the other 6
  (subscription 24, all "Finca rústica") really are land and read correctly
  since #557.
- **What breaks, for whom:** nothing today — the subscription is inactive and
  hidden, so no screen shows these rows. The day it is shown, two things are
  wrong at once: `scorer_for` picks the scorer by category, so all 46 were
  scored by the LAND scorer (0–100, mean 51.3); and since #554/#557 a house
  requirement reads `listing_kind == land` as a measured fail, so adding
  criteria to a "casas con finca" subscription would hide every one of them.
- **Not a repair to run blind:** `utils/repair_portal_plot_classification.py`
  is keyed to fotocasa `/comprar/terreno/` paths and finds 0 of these. A fix
  re-files the category, not `area_type` (which is right here), behind a
  snapshot, and rescores in the same transaction.

### The property page badges a rotated rectangle red

- **Anchor:** `templates/property_detail.html`, the cadastral card's badge on
  `_cad_geom.get('bbox_fill_ratio')` (≥ 0.7 success, ≥ 0.5 warning, else
  danger).
- **Measured 2026-09-16:** property 969 — the owner's own approved reference,
  a clean 26.6 × 63.9 m rectangle — has `bbox_fill_ratio` 0.447 and
  `polsby_popper` 0.657, so its fill badge is red.
- **What breaks, for whom:** the owner reads their approved parcel as
  irregular on the one criterion they call decisive. The bounding box is
  axis-aligned, so any parcel lying diagonally fills little of it; `#547`
  stopped feeding that ratio to the taste model as "the shape" for exactly
  this reason (see `docs/rules/taste.md`), but the page still colours it as a
  verdict.
