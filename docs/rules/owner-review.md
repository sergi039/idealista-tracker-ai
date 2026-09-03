# The owner's review, timeline, cadastre and attachments

Moved verbatim from `CLAUDE.md` (lines 1437–1630 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**The conversation that decides a purchase has a home now, and it is not
`enrichment`** (#430, five PRs on 2026-08-20). Everything the app stored about a
listing had been *measured*; what it never held was the part a person produces
-- what the agency answered, what is still owed, and what the owner concluded.
On 2026-08-20 property 774 collected all four kinds in one day (a cadastral
document by WhatsApp, a promise with a date, two verbal answers, a rejection)
and every piece went in by hand through `docker exec` as JSON, because there was
nowhere else. `property_activity` (migration 021), `property_attachment`
(migration 023) and six columns on `properties` are that place.

**The decision and the outstanding action are two independent readings, not
one.** `services/owner_review.py` owns both, each in Python and in SQL, branch
for branch, the `advertiser.py` contract -- and `tests/test_owner_review.py`
runs one matrix through both, because a count that disagrees with the badges
under it is a third wrong number. The decision is `interested` / `waiting` /
`rejected`, with **`undecided` as what NULL reads as** rather than a fourth
stored value: it is its own filter option with its own count and is never folded
into `rejected` (#98, in the column the owner filters on most). The action is
`none` / `pending` / `overdue` and is legal under *any* decision -- "interested;
call the architect on Friday" is an ordinary state, and hanging the reminder off
`waiting` loses it. Nothing writes `overdue`; it is derived, so the badge and
the column cannot drift.

Four things about that module are load bearing, and three of them were found by
mutation rather than by review:

* **One date per request, and it is Madrid's.** A due date is a calendar date
  somebody reads off a calendar in Spain, so `owner_review.today()` is
  `Europe/Madrid` -- the one place in this application that is not UTC, and the
  docstring says why. Every collection endpoint computes it **once** and threads
  it into the filter, the counts, the badge, the CSV and **both** API
  serializers. The compact `/api/properties` response is hand-built and is the
  *default* one, so a field added to `to_dict` alone is missing exactly where
  most consumers look. Testing this needs a clock that *moves*: a frozen `today`
  cannot tell "the request's date was threaded through" from "every consumer
  recomputed it", and a mutation removing `review_today=` from the serializer
  stayed green until the second call answered differently.
* **`set_review` owns its transaction.** It takes the row `FOR UPDATE` before
  reading the old state, because two presses on four gunicorn threads otherwise
  append two contradictory transitions, each atomic and both wrong -- #339's
  shape, one column over. There is deliberately **no `commit=False`**: a lock
  whose release the callee cannot see is worse than the race, which is what
  `services/enrichment_write.py` already says. SQLite has no row lock to
  observe, so the test asserts the *call and its position* and says in its
  docstring what it therefore does not prove.
* **A verdict event carries the whole review state**, not a from/to pair: a
  changed reason or a moved due date under an unchanged decision is a real
  change and a pair loses it. Pressing Save twice writes one entry -- and
  `was_edited` compares `created_at` to `updated_at` **exactly**, which is only
  honest because both writers stamp them from one value. A tolerance was tried
  and is the wrong shape: wide enough to swallow two column defaults and narrow
  enough to notice a typo corrected three seconds later is not a number that
  exists.
* **`history_out_of_sync` is a disclosure, not a guarantee** -- detail page
  only, one query, comparing the whole snapshot. Direct SQL is a supported
  workflow here (`curate_on_mini.sh`, `docker exec … psql` -- 774's own data
  arrived that way), and a column written that way leaves no entry behind. It
  can see that the newest entry no longer describes the row; it cannot see a
  transition nobody recorded, and it does not claim to. The row readers stay
  **pure** -- no session, no query -- because the list calls them once per row.

**The timeline is one feed, ordered by when things happened.** Notes, exchanges
and verdict changes share `property_activity` because they share one screen: the
material is causally ordered (asked, answered, promised, decided) and two lists
make the reader re-interleave by date what a feed already says. `happened_at` is
the owner's and editable; `created_at` is when the row was typed, and a feed
ordered by the second tells the story in the order somebody sat down to write
it. **A verdict entry is not a note**: `edit_entry` and `soft_delete_entry`
refuse any other kind and *the route refuses before reaching them* -- hiding the
control in the template is not the guard, and the test posts at one directly.
Deletion is soft throughout: everything else here can be recomputed, and a
sentence the owner typed cannot.

**The cadastral parcel is fetched from two free, keyless endpoints**
(`services/cadastre_service.py`), both verified live on 2026-08-20 at 0.15-0.28 s
against `33016A003001530001HQ`. The INSPIRE WFS `GetParcel` stored query gives
the outline -- the parameter really is spelled `STOREDQUERIE_ID`, which is
Catastro's typo and not one here -- and `Consulta_DNPRC` gives the class, the
polígono/parcela locator, the paraje and the rustic subparcels. Its parameter is
`RefCat`; the older ASMX endpoint spells it `RC`, and the wrong name returns a
200 carrying an error.

Five rules there, each measured:

* **Nothing trusts an HTTP status.** Every Catastro error arrives as `200 OK`
  with the failure in the body; both real refusals are committed under
  `tests/data/`. Only `not_found` is a measured negative -- `refused`,
  `unavailable`, `malformed` and `unsupported_metric_crs` are absences of
  measurement, never cached and never written over an answer somebody has, per
  source rather than per run (#153's shape: the metric outline is decisive, the
  map outline and the attributes advisory).
* **Three requests per press, exactly, because there are no retries.**
  `max_attempts=1`: a press is one attempt per endpoint and the retry is the
  owner pressing again, which they can see the result of. Catastro publishes no
  numeric rate limit and does publish an **~10-day IP ban** for abuse, so the
  arithmetic has to be exact rather than approximately bounded; the route's
  `@limiter.limit("5 per minute")` then caps it at fifteen a minute. Proving
  that needs a test *under* the client -- mocking `_get` proves only what the
  mock does, and a mutation restoring `max_attempts=3` stayed green until
  `requests.get` itself was watched.
* **The zone comes from the parcel's own `cp:referencePoint`**, and the EPSG
  code is stored beside the metrics. The WFS reprojects into whatever zone it is
  asked for, silently and wrongly: 25831 on an Asturian parcel returns a
  negative easting and an area 1.17% out. Bayas sits at -6.027, three kilometres
  west of the 29/30 meridian, so its zone is genuinely **25829** even though
  Asturias is spoken of as 30 -- the first fixture assumed otherwise and the
  `srsName` check caught it.
* **`srsName` checks the CRS and `areaValue` checks the parse -- not the other
  way round.** The declared area catches a dropped ring, a truncated `posList`
  or the wrong units at `max(1 m², 1%)`, and it cannot see a wrong projection at
  all: measured, the same parcel computes 6193.5 m² in 25830 and 6192.8 in
  25829 against a declared 6193. One fixture in the tree is deliberately the
  *neighbouring* zone, to keep that distinction from being re-collapsed.
* **The largest inscribed square is deliberately absent.** A grid over the
  axis-aligned case underestimates a diagonal parcel by an amount nobody has
  bounded, and a number that decides a purchase must not be an unlabelled
  approximation. 774's own 27×27 m figure stays a sentence in the rejection
  reason, which is what it was.

**Attachments put bytes on disk and metadata in the row**
(`services/attachments.py`, `data/attachments/`). `./data` is the one bind
mount, so a file written there survives the `COPY . .` rebuild; the database is
17 MB and photos are megabytes each, so `bytea` would carry every one of them
through every `pg_dump`. Four rules:

* **Content-addressed**, sha256, two-level shards -- so no path is ever built
  from anything a client sent. Measured: a PDF uploaded as
  `../../../../etc/passwd` lands at `75/c0/75c0….pdf` with the name kept as
  text and nothing written outside the root.
* **Write, fsync, `os.replace`, THEN commit the row.** The two systems share no
  transaction and the failure directions are not equal: an orphan *file* is
  inert and the sweeper reclaims it, an orphan *row* is a download that 404s and
  is indistinguishable from a sweep that has not run.
* **The type is what the bytes say.** `puremagic` reads the signature and a
  short allowlist narrows it -- the narrowing is the boundary, since "puremagic
  identified something" is not "we accept this". **SVG is not on the list at
  all**: it is XML, it can carry `<script>`, and `nosniff` does not help a
  document the browser is right to render. Measured through the real form: an
  SVG named `photo.jpg`, an HTML file named `plan.pdf`, a zip named `photo.png`
  and an empty file are all refused, **and the entry the owner typed survives
  the refusal**. The download route passes the *stored, sniffed* `mimetype`
  explicitly -- left to Werkzeug it guesses from the client's own filename --
  sets `nosniff`, and serves `as_attachment` for everything but the raster
  formats a browser really draws. A row whose bytes are gone answers **410 and
  logs**, because 404 reads as "no such attachment".
* **The composite foreign key is the invariant**: `(activity_id, property_id)`
  references `property_activity (id, property_id)`, which is what migration
  021's `UNIQUE (id, property_id)` exists for. An attachment on one property can
  therefore never name another property's exchange. There is deliberately **no**
  unique constraint on `(property_id, content_sha256)`: the same document may be
  attached to two exchanges, and a soft-deleted row would otherwise hold the key
  against re-uploading the file it refers to. Dedup is on disk; a row is a link,
  which also means a file has no single owner -- so `utils/sweep_attachments.py`
  keeps any hash **any live row** references, keeps anything younger than 48 h
  (the fsync-to-commit window every upload passes through), skips `tmp/`, and
  *moves* rather than deletes. `tools/backup_attachments.sh` exists because
  "back up this app" stopped meaning one `pg_dump`: the dump goes first and the
  bytes second, since the other order puts a row in the backup whose file is in
  no archive.

**774's own thread was converted on production on 2026-08-20**
(`utils/import_review_notes.py`, snapshot at
`data/review_import_774_20260820.json` on the mini). It **copies** rather than
moves -- `enrichment` still holds the review and the cadastral block, because
nothing here deletes a measurement -- **refuses a property that already carries
entries** under `FOR UPDATE` (that, and not a marker column, is its
idempotency), and writes the verdict **through `set_review`** so the columns and
the verdict entry land in one transaction. Its `restore` is compare-and-swap: it
touches only the rows its snapshot names and *stops* rather than deleting one
that was edited afterwards. The ficha catastral PDF itself is **not** in the
app: it arrived in the owner's WhatsApp, so the converted entry says a document
was received and names it. Writing it as a stored attachment because its name is
known would be this ticket's own defect, relocated.

Three things this feature cost that are not about the feature:

* **Two defects were found by looking at the page and could not have been found
  otherwise.** The cadastral block rendered `None × None m` (the template read
  `bbox`, the service writes `bbox_m`) and then `measured in EPSG:None` (774's
  hand-written block predates the feature and carries no `epsg`). Both passed
  every unit test, because a test asserting the section is *present*, and that
  the reference and the paraje appear in it, is satisfied by a block full of
  `None`s. Assert the numbers **by value**.
* **The property page has exactly one `<script>` element**, and
  `tests/test_issue_23_xss_and_prompt_injection.py` extracts it *by index*. A
  second `<script>` beside a form made that harness read the wrong one and
  failed eleven tests across three files; page-specific JavaScript goes at the
  end of the existing block, and guards **both** elements it looks up, because
  those harnesses run it in node against a DOM stub where one lookup answers and
  the other does not.
* **An i18n key ending in `_other` is read as a plural form.**
  `tests/test_subscription_copy_is_translated.py` then demands a `_one` beside
  it, which is why the channel labels carry a `_label` suffix.
