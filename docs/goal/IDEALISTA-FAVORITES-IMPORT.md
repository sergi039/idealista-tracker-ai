# Idealista.pt favorites → Property import

One-shot importer for the Portugal Compose app (`idealista-pt-app` /
`idealista_pt`). Reads the owner's Idealista favourites and upserts
`properties` with `is_favorite=True`.

Tool: `tools/pt_ops/import_idealista_favorites.py`

Decision: [ADR-0002](../adr/0002-import-favorites-from-logged-in-dumps.md).

## Capture a dump (preferred)

Idealista sits behind DataDome. Do **not** try to defeat it. Save the page
while logged in:

1. Open `https://www.idealista.pt/en/utilizador/favoritos/` (or the PT
   locale equivalent) in a normal browser session.
2. Scroll until every favourite card you care about is on the page
   (pagination: save each page, or capture the XHR below).
3. **Save As → Webpage, HTML Only** into a private path outside git, e.g.
   `/Users/baf/Backups/idealista-tracker/private/favorites-page.html`.
4. Or DevTools → Network → filter XHR → the favorites list response →
   **Copy response** into a `.json` file (same private directory).
5. Or paste the private helper
   `/Users/baf/Backups/idealista-tracker/private/capture-favorites-console.js`
   into the DevTools console on the favourites tab (scroll fully first), then
   save the printed JSON. That shape (`listings[].listing_id` / `url` /
   `price_hint` / `photo_urls`) is accepted by the importer.

Listing photographs come from the dump only: card `<img>` / `data-ondemand-img`
on `img*.idealista.pt`, XHR `thumbnail` / `multimedia`, or console `photo_urls`.
They are stored as `enrichment.import.photos` (same block as mail). The importer
does not fetch listing pages. Re-run `--apply` on a dump that includes images to
backfill rows that were imported without photos; existing mail photos are left
alone. A credential-bearing URL is refused.

**Card root.** Capture the whole `article.item[data-element-id]`.
`closest('.item')` / `[class*="item"]` matches the title block (`item-info-container`)
and drops the gallery — the dump then parses with `photos=0`. The console helper
under Backups uses the article node. Paginate `/favoritos/pagina-2` etc. and merge.
`--help` on the tool repeats this. A dry-run prints `photos_in_dump=N/M` and a
HINT when photographs are missing. `--fetch` is a DataDome dead end.

Sanitized fixtures used by tests live under
`tests/fixtures/idealista_favorites/` (synthetic ids only).

## Optional cookies file (secondary fetch)

Only if you insist on `--fetch` instead of a dump:

1. From the logged-in browser, copy the `Cookie` request header for
   `idealista.pt` (DevTools → Network → document request → Request Headers).
2. Write **only** that Cookie header (or a Netscape cookie jar) to:

   `/Users/baf/Backups/idealista-tracker/private/idealista-favorites.cookies`

3. `chmod 600` the file. Override path with env `IDEALISTA_FAVORITES_COOKIES`
   or `--cookies PATH`.
4. Never commit the file. The tool never prints cookie values, Authorization
   headers, or passwords.

If the fetch returns DataDome / 403 / captcha, the tool exits `2` and asks
for a saved dump — do not retry into the wall.

## Dry-run (default)

Parse-only (no DB):

```bash
uv run python tools/pt_ops/import_idealista_favorites.py \
  --dump /path/to/favorites-page.html \
  --parse-only
```

Against the live PT app DB (still no writes):

```bash
docker exec -i idealista-pt-app \
  python tools/pt_ops/import_idealista_favorites.py \
  --dump /path/to/favorites-page.html
```

(Copy the dump into the container first, or mount/private path as you already
do for other `pt_ops` inputs.)

## Apply

Creates missing rows (`source_email_id = idealista:favorite:<listing_id>`,
canonical URL `https://www.idealista.pt/imovel/<id>/`, `is_favorite=True`)
and marks already-known rows (matched by `idealista_property_id`) as
favorite. No Google enrichment. No Telegram sends.

```bash
docker exec -i idealista-pt-app \
  python tools/pt_ops/import_idealista_favorites.py \
  --dump /path/to/favorites-page.html \
  --apply
```

Optional: `--profile-id N` for new rows (default: first active visible
subscription). `--url` only matters with `--fetch`.

## Readiness

| Gate | Status |
| --- | --- |
| Parser + fixtures + unit tests | yes |
| Dry-run / `--parse-only` on fixtures | yes |
| Private dump or cookies on disk | **owner action** — not present yet |
| `--apply` on live `idealista_pt` | blocked until a real dump/cookies path exists |

Do not run `--apply` until a real favorites dump (or cookies file) is in
place and a dry-run against that dump looks right.
