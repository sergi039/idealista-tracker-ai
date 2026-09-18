# ADR-0002: Import Idealista.pt favorites from logged-in dumps

Date: 2026-09-18
Status: Accepted

## Context

Portugal mail ingest creates `Property` rows only for `listed` events. Favorite
photo/relist mails and search price-drops for listings that never arrived as
"new in your search" stay `target_not_found`. Idealista sits behind DataDome;
this machine must not fetch listing pages or retry into a wall. The owner
already has a logged-in browser session. Mail already stores photographs in
`enrichment.import.photos` via `portal_photos`; a second photo store would
split the PT UI.

## Decision

We will import favorites with a one-shot operator tool,
`tools/pt_ops/import_idealista_favorites.py`, from a dump captured in a
logged-in browser (saved HTML, XHR JSON, or console JSON). New rows get
`source_email_id=idealista:favorite:<id>` and `is_favorite=True`; existing
rows matched by `idealista_property_id` are only starred. Photographs come
from the dump's card gallery (`article.item[data-element-id]`, `photo_urls`,
or XHR `thumbnail` / `multimedia`) and are written to
`enrichment.import.photos`. Mail-captured photographs are never overwritten.
The tool does not fetch listing pages. `--fetch` with cookies is secondary
and fail-closed on DataDome / 403 / captcha. Dry-run is the default;
`--apply` writes. Dumps stay outside git.

## Consequences

- Favorites appear in the PT catalog without a billed Google path or a
  listing scrape.
- Queue rows for favorite photo/relist events can resolve after import plus
  silent retry; search-only price-drops still need a `listed` mail or a seed.
- The operator must capture the whole card and paginate (`/pagina-2`, …).
  `closest('.item')` yields `photos=0`. The CLI prints `photos_in_dump=N/M`
  and a HINT when photographs are missing.
- A dump without images cannot be healed by `--apply`; recapture is required.
- Favorite `email_date` is "now", so older price-change mail against those
  rows records history and does not rewind the asking price.

## Alternatives considered

- **Scrape each `/imovel/<id>/` page** — DataDome; refused.
- **Idealista partner / RapidAPI / Apify** — extra credentials the owner
  does not want for this tool.
- **Cookie `--fetch` as the primary path** — measured to hit DataDome;
  kept as an explicit dead-end, not the default.
- **A second photo column** — the PT page already reads
  `enrichment.import.photos`.
