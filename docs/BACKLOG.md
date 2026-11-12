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

### Yaencontre rows have no plot source except the per-press cadastre

- **Anchor:** `services/plot_text.py` (`apply_to_property`); `docs/rules/portal-network-safety.md`.
- **Measured 2026-11-12** on subscription 24: 1622 of the 1764 rows passing
  price and house with an unknown plot are yaencontre, whose alert card has no
  description and whose pages may not be fetched.
- **What breaks, for whom:** the plot criterion stays `unknown` for them, so
  #634's text reading cannot move most of the gap.
- **Check:** `python -m utils.backfill_plot_from_text` prints the per-portal split.

### `utils/backfill_plot_area.py` skips rows filled from text

- **Anchor:** `utils/backfill_plot_area.py` (`Property.plot_area.is_(None)`).
- **What breaks, for whom:** a fotocasa row whose plot was read from its text
  (#634) is out of that backfill's scope, so a later page-stated plot (stronger
  evidence) never replaces the text reading. Matters only if the two differ.

### An explicit negation before the plot noun that introduces the figure is not read

- **Anchor:** `services/plot_text.py` (`_scan`, `_PLOT_MENTION`).
- **What breaks, for whom:** "No tiene parcela de 300 m²" and "Sin parcela de
  300 m²" write 300, so a row may move from `unknown` to `pass`/`fail` on a
  plot the text denies. Found by merge_bot on #661
  (https://github.com/sergi039/idealista-tracker-ai/pull/661#issuecomment-6828734743),
  merged as is on the owner's command 2026-11-12; not measured on production.

### A second m² figure without its own plot noun is not counted as a competing reading

- **Anchor:** `services/plot_text.py` (`_scan`, the FORWARD match).
- **What breaks, for whom:** "Parcela de 500 m² o de 900 m²" writes 500
  although the text states two values, so the criteria verdict may rest on the
  wrong figure. Found by merge_bot on #661
  (https://github.com/sergi039/idealista-tracker-ai/pull/661#issuecomment-6828734743),
  merged as is on the owner's command 2026-11-12; not measured on production.

### A plot figure introduced by a plot noun is trusted to be this home's

- **Anchor:** `services/plot_text.py` (`_PLOT_MENTION`, `_scan`).
- **What breaks, for whom:** the reading requires every plot noun to carry the
  figure read, so negations and a second plot named without a figure refuse.
  What it cannot see is a figure whose noun describes ANOTHER plot ("finca
  colindante de 5.000 m² en venta" in an otherwise silent text): that figure
  is written, and a row could move from `unknown` to `pass`/`fail` on it.
  `jardín` / `huerta` are deliberately not plot nouns (part of a plot, not a
  statement of one), and a plot equal to the house figure ("casa de 300 m² en
  parcela de 300 m²") is written as stated. Every write carries its snippet in
  `enrichment["plot_from_text"]`, so a wrong one is findable. Not measured on
  production (third review of #661, 2026-11-12).

### A dimension declared above the `posList` is not read

- **Anchor:** `services/cadastre_service.py` `_ring_points` (reads
  `srsDimension` from the `posList` alone, never from an enclosing
  `LinearRing` / `Polygon` / `Surface` / `MultiSurface`).
- **Found 2026-11-12** by merge_bot on PR #662
  (https://github.com/sergi039/idealista-tracker-ai/pull/662#issuecomment-6829017199),
  the third review round; merged as is at the owner's command the same day.
  Input: `<LinearRing srsDimension="3"><posList>0 0 0 2 2 2 2 0 2 0 0 0</posList>`
  — the triples are read as 2D pairs and close into a valid ring of area 4.
- **What breaks, for whom:** a Catastro answer that declares three
  dimensions above the `posList` is measured as a shape nobody sent. A parcel
  outline is almost always caught by `area_agrees` against `areaValue` (no
  guarantee: only a coincidental area passes); a neighbouring building's
  outline has no such check, so the owner would get an `ok` measurement in
  `enrichment["neighbours"]`, cached 30 days (`neighbours_buildings_v1`) and
  fed to the taste prompt through `taste_descriptors`. It never reaches
  `score_total`. **Not reproduced on live answers:** in all four fixtures
  (`tests/data/neighbours/969_*`, `1282_*`) `srsDimension="2"` sits only on
  `posList`. Before #662 the card did not read `srsDimension` at all.
- **Check:** `rg -n 'srsDimension' services/cadastre_service.py` — a fix
  takes the nearest declared dimension on the ring's ancestors and refuses
  anything but 2 as `MALFORMED`.

### Four input shapes take the Jev route out instead of to the bridge

- **Anchor:** `services/typesafe_transport.py` (`_KEY_SHAPE`, `_origin`,
  `connection.connect`, `watchdog.start`); `services/sea_view_service.py`
  (`jev_text_sha256`).
- **Found 2026-09-19** by the Tier 2 reviewer (`rx`, Codex) on PR #574, one
  pass after the six it had already produced. Each is real and each is
  narrower than the last; the review loop was stopped at the owner's call
  under `rules/common/workflow-risk.md` (an attack-fix loop over one diff does
  not converge), and these four were parked rather than fixed in the same PR.
- **What breaks, for whom:** in four input shapes the sea-view text claim
  raises instead of falling back to the subscription bridge, so one Enrich
  press ends with an error rather than a verdict. Nobody is affected today:
  three of the four need a hand-edited `.env` or a hostile endpoint, and the
  fourth needs an advert whose text carries a lone UTF-16 surrogate.
  1. `TYPESAFE_API_URL` with a space in the host (`https://bad host`):
     `HTTPSConnection` raises `InvalidURL` before the `try`.
  2. A resolver that blocks for a minute: the watchdog closes no socket
     because none exists yet, so `connect()` outlives the allowance. Only a
     resolution off the calling thread bounds this; the phase-boundary check
     stops the request going out, it does not shorten the wait.
  3. Listing text containing a lone surrogate (`\ud800`): hashing it for
     `jev_text_sha256` raises `UnicodeEncodeError`.
  4. Thread exhaustion: `watchdog.start()` raises `RuntimeError`.
- **Check:** `rg -n 'except TypeSafeTransportError' services/typesafe_transport.py`
  — a fix wraps connection construction, resolution and the watchdog in the
  same failure path, and hashes with `errors="replace"`.
- **Cost of not fixing:** one failed Enrich press, retried by hand. No data is
  written wrong: every one of these raises before a verdict is stored.

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

### A `mode=ro` SQLite URI inside the protected directory can still create WAL sidecars

- **Anchor:** `tests/data_dir_guard.py` (`_sqlite_places`, the
  `query == "mode=ro"` branch of the `file:` URI reading).
- **Found 2026-11-12** by merge_bot on PR #666
  (https://github.com/sergi039/idealista-tracker-ai/pull/666#issuecomment-6831831977),
  the third review round; merged as is on the owner's command 2026-11-12.
  Input: a WAL-mode database in the protected directory with no sidecar
  files, then `sqlite3.connect("file:<dir>/db.sqlite?mode=ro", uri=True)` and
  a `SELECT`. Reproduced on a temporary directory (Python 3.11.17, SQLite
  3.53.1): `db.sqlite-shm` (32768 bytes) and `db.sqlite-wal` (0 bytes) appear
  and stay after the connection closes, the database file is unchanged, and
  the guard records no refusal.
- **What breaks, for whom:** the guard reads that open as a read, and SQLite
  creates the two files from C, past the audit hook. A test doing this against
  the deployment's state directory would leave two files there that the run
  was supposed to be unable to write. **Not reachable today, not reproduced
  against the repository's own tests:** the production `data/` holds no SQLite
  file (817 files checked by header and by name on the mini, 2026-11-12), no
  tracked file is one, and nothing outside the guard and its tests opens
  SQLite by `file:` URI.
- **Check:** `git grep -nE 'uri=True|mode=ro|sqlite3\.connect\(' -- '*.py' ':!tests/data_dir_guard.py' ':!tests/test_data_dir_guard.py'`
  prints nothing, and
  `find "$(tools/autopilot/lib/data_dir.sh "$PWD")" -type f -exec sh -c 'head -c 15 "$1" | grep -q "^SQLite format 3" && echo "$1"' _ {} \;`
  prints nothing in the main checkout on the mini. A fix treats every `file:`
  URI naming a path inside a protected directory as a write, `mode=ro` included.
