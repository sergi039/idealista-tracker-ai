# The taste profile

Moved verbatim from `CLAUDE.md` (lines 1631–1700 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**The owner's taste ranks the search, and it learns only from the owner's own
words** (#498, 2026-08-30). The review reason (`owner_verdict_reason`, now a
textarea) is where the owner says WHY a listing is liked or rejected;
`services/taste_service.py` distills those reasons into a structured profile
and scores any listing 0–100 against it. Everything runs over the
subscription bridge and nothing else — **no Google request exists anywhere on
the path** (the owner's standing order of 2026-08-30: paid Google only with
their consent, for objects they approve), and `tests/test_taste_service.py`
runs every flow with `billed_get` patched to explode. A listing is scored on
what the app already measured; a fact nobody measured is *named* missing in
the prompt and lowers confidence, never the score (#98).

Six things are load bearing, most of them findings of the two codex design
reviews that preceded the code:

* **The profile is an insert-only ledger** (`taste_profile`, migration 024) —
  the version IS the primary key, so concurrent builds cannot mint the same
  version, a failed build inserts nothing, and prior versions stay readable
  forever. A malformed row reads as `no_profile`, never as an empty profile.
  With two signals the profile marks itself `provisional`; dislikes may be
  empty and dealbreakers come only from explicit owner language — two liked
  references cannot establish aversions.
* **A stale score never ranks interleaved with current ones.** The sort (page
  AND CSV, one shared expression) is a CASE that answers the score only while
  its stored `profile_version` matches the current ledger head — stale and
  unscored rows are NULL and sort last in both directions. The version is
  compared as TEXT via a cast-to-text (SQLite's json_extract answers INTEGER
  for a JSON integer and refuses to equal '3'; PostgreSQL's ->> is already
  text, and text→text cannot raise on a hand-edited value).
* **No lock is held across a bridge call** (#339's shape). The row is locked
  after the answer, re-read, and the write is discarded as `superseded` when
  the row was meanwhile scored against a newer profile, when its facts
  changed under the call, or when it already carries an `ok` score for the
  SAME version — two callers racing one row must not end with whichever call
  finished last (only the backfill's `--force` may overwrite a settled
  current score, and even it never replaces a newer version's). The build has
  the same discipline: a profile whose signals were edited mid-build is
  refused, not published. A bridge refusal writes NOTHING — the row keeps its
  old score or its NULL, which is what keeps it in the backfill's scope.
* **Staleness is the reader's verdict, and facts count** — `read_taste`
  answers `stale` for an older profile version, an older scorer rubric, AND a
  row whose facts fingerprint no longer matches the row (a price drop makes
  yesterday's judgement about a listing that no longer exists). The SQL sort
  and coverage count see the two versions but not the fingerprint (only
  Python can recompute it), so the coverage line is a disclosure, not a
  guarantee — `history_out_of_sync`'s wording. The backfill's scope IS the
  reader: a row the page calls stale is exactly a row the next run re-scores,
  and its refusal-stop counts bridge CALLS the bridge actually saw
  (`bridge_called`), never batches gated away before one.
* **A batch answer is validated whole**: a missing, duplicated or uninvited
  property id rejects the entire call, because an answer that already
  demonstrated it was not following the question must not have its plausible
  half salvaged. A row with nothing to judge (no price, no area, no text) is
  gated deterministically BEFORE any credit is spent.
* **Timeline notes are deliberately not fed to the profile** — the timeline
  is a purchase conversation, not preference statements — and `waiting` is
  excluded because it means "not decided", not "weak yes".
* **The CLIs are dry-run first** (`utils/build_taste_profile.py`,
  `utils/backfill_taste.py`): scope is explicit (`--profiles`/`--ids`/
  `--all`, no implicit default), `--apply` is the spend, three failed bridge
  CALLS in a row stop a run (calls, not rows), and `resumable` is true
  exactly when finished rows leave the scope (`not --force`). The announce
  rule for the mini applies to both.

The taste score is its own fourth display mode and its own sort — it never
enters `score_total`. The disclosure beside the result count ("K of N scored
against profile vX") reads the same predicate the sort does. The seeds are
969 and 1282 — the owner's two named tops, recorded through `set_review` with
dossier-derived reasons that name their provenance.
