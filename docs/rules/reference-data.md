# Committed reference data

Moved verbatim from `CLAUDE.md` (lines 1830–1861 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**Four reference files are committed on purpose, and `.gitignore` re-includes
them one at a time.** `data/*` excludes the runtime artifacts — backfill
snapshots, ledgers, logs — and `!data/ine_municipal.json`,
`!data/hospitals_cnh.json`, `!data/sepe_unemployment.json` bring back the small,
reviewed, importer-generated files that the QoL card and `/municipalities` read;
`!data/top_agencies.json` is the odd one out, curated by hand rather than by an
importer (`/agencies` above), so nothing can regenerate it and re-measuring is a
person's job.
It is `data/*` and not `data/` because git cannot re-include a file whose parent
*directory* is excluded: the bare-directory form makes every negation
silently dead. Regenerate with `utils/import_ine_data.py`,
`utils/import_cnh_hospitals.py` and `utils/import_sepe_unemployment.py`; a
missing or unreadable file reads as `no_reference_data`, never as an empty
landscape. Filling the card itself is free and scoped —
`utils/backfill_quality_of_life.py`, INE and CNH from those local files and the
supermarket reach through the shared Overpass gate — so it belongs with
`backfill_sea_view` and `backfill_osm_amenities` under the exception in the hard
rules below, not with the paid backfills.

**SEPE's `<5` is suppression, not zero** — #98's rule inside a national dataset.
A withheld count is recorded as `unemployed_total: null` with `suppressed: true`
(June 2026: one municipality in the five watched provinces, 33048 Pesoz);
reading it as 0, or as 5, fabricates a figure, and dropping the row claims the
municipality is absent from the dataset. Two more properties of that source are
measured rather than assumed, and each cost an afternoon: the CSV declares
`charset=UTF-8` in the HTTP header and is actually **ISO-8859-1**, and its
header row sits under a banner with stray spaces inside the column names, so the
header is *found* and stripped rather than indexed at a fixed offset. The newest
month on sepe.es exists only as legacy OLE2 `.xls`, which openpyxl refuses and
xlrd is not a dependency of this project — so the annual open-data CSV is the
newest *machine-readable* month, and the period actually parsed is recorded in
the output instead of being assumed by whatever renders it.
