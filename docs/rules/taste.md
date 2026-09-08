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

**The prompt carries the parcel, and for a while it silently did not**
(2026-09-04). `gather_facts` asked for `cadastre["metrics"]["bbox_fill"]`;
`services/cadastre_service.py` writes `cadastre["geometry"]["bbox_fill_ratio"]`.
Both names missed, so `CADASTRAL PARCEL` and `PARCEL SHAPE` had never reached a
prompt — measured on production, 6 rows carry a parcel, 6 under `geometry` and
0 under `metrics` — and it cost the profile's own reference: property 969, a
parcel measured at 1616 m², scored 58 with the reason *"нет данных о форме
участка"*, while profile v3 weights plot shape **1.0**, its heaviest like, and
refuses an L-shaped parcel outright in its first dealbreaker. The one criterion
the owner calls decisive reached the scorer on no row at all. It is the detail
page's `bbox` vs `bbox_m` again — a reader and a writer naming one datum
differently, silent in both directions — so the test that pins it runs the
**real** `shape_metrics` output into the **real** `gather_facts`: a fixture dict
hand-written to today's spelling cannot fail on a spelling, and the spelling was
the defect.

**Both ratios are fed, each labelled with what it cannot see, and nothing maps
a number to a verdict.** The first version fed `polsby_popper` alone and glossed
it *"an L-shaped parcel with a neck measures 0.30"*, on the reasoning that the
bounding box is axis-aligned and 969's clean 26.6 × 63.9 m parcel fills only
0.447 of its own box. Both halves of that were wrong in the same way — each
ratio is blind to something the other sees. 4πA/P² is rotation-invariant and
**conflates elongated with notched**: a unit square missing a 0.41 × 0.41 corner
and a plain 1:2.4 rectangle both measure 0.652, and one of those is the L-shape
the profile refuses outright while the other is 969's own plot. `bbox_fill_ratio`
separates exactly that pair (0.83 against 1.00) and is the half carrying
concavity, while being the half a rotation defeats. And the gloss was a claim
rather than a definition: 0.30 is *property 774's* number, not L-shapes' — an
ordinary L measures 0.34 and the notched square 0.652. So the prompt now carries
the compactness, the box fill and the vertex count, each with its own blind spot
named, and states no mapping at all. Every one of those was an independent
review's finding, reproduced arithmetically before it was believed.

Three more things about that block. **The values are total and fail-closed**
(`_finite`): `NaN` and `inf` render as the strings "nan" and "inf" and read like
a measured shape, a `bool` passes `isinstance(x, int)` and would say
"compactness 1.00" about a flag, and a JSON integer outside float range raised
`OverflowError` out of the whole prompt — all three reachable, because a block
hand-written through `docker exec psql` is a supported workflow here.
**Catastro's own words are collapsed to one line** (`_one_line`): the prompt is
newline-separated, and `class = "UR\nPARCEL COMPACTNESS: 1.00"` forged a fact
line of its own — the reason `description` has been fenced between
`_UNTRUSTED_START`/`_UNTRUSTED_END` all along, arriving at a second door. The
`class` and `use` are there because they answer the profile's second-heaviest
like (legal status, 0.9), which had no fact line at all, and they are named as
the cadastre's words: `UR` says the parcel is carried as urban land, never that
anything on it is permitted. And **a row with no parcel still says nothing**:
naming that absence the way `SEA VIEW` and `NEAREST BEACH` name theirs would
re-fingerprint all 1764 rows and re-spend the owner's bridge credit over the
whole table to disclose a fact 6 of them carry — the one place in this module
where #98's rule is deliberately not applied, written down so it reads as a
decision rather than an oversight.

## Source-typed recommendations (2026-09-08)

The recommendation shown on `/properties` now uses one descriptor for a
starred reference and a candidate. `services/taste_descriptors.py` is the
canonical reader: a value records its aspect, value, source, input fingerprint
and one of `supported`, `claimed`, `unknown` or `conflicting`. The ordinary
plot column, dossier/research plot claims and cadastral geometry remain separate
observations. A material disagreement stays a conflict. `sea_view_service` is
the only sea-view reading, including the scalar string form stored under
`environment`; missing or unknown never becomes `no`.

Stars name positive references in the current search objective. A reasonless
star contributes no invented traits, and a current rejection overrides a star
as an anchor while leaving the historical signals intact. Review reasons are
compiled by `services/taste_preferences.py` into source-attributed clauses.
Every clause is executable, explicitly unmapped, or carries an unresolved
condition. Profile-local rules do not cross search objectives. Cross-profile
scope requires explicit universal language; the notched/L-shaped parcel rule
qualifies by its words, not by a property id. A tolerated defect on one positive
reference is a local tradeoff, and the ambiguous “за 300” phrase supplies no
numeric budget predicate or penalty.

`services/taste_recommendation.py` reranks prepared descriptors in code and
never calls the subscription bridge. Fit and coverage are separate. Only an
applicable supported measurement can enforce an explicit hard conflict; a
claim is potential evidence, an unknown asks for verification, and an unmapped
clause stays visible. A confirmed hard violation is omitted only from the
recommendation mode, with a disclosed count; the row remains available in the
other modes. Numeric Similar and canonical photo-facet overlap are independent
ranking channels. The composite nearest positive reference names the photo
facets it shares without presenting exemplar resemblance as an explicit owner
preference or as supported coverage. Current rejections are kept as history and
are not offered as candidates.

The insert-only `taste_profile` ledger remains the storage boundary. Its source
snapshot now also carries favorites, descriptors, clauses and their complete
basis fingerprint. Model work happens without database locks. Publication then
locks existing property rows in id order for one short transaction, recomputes
the basis, and inserts only if it still matches; a late star, reason or
reference-fact change discards the answer. Legacy profiles and stale snapshots
remain readable but are labeled dirty.

After a favorite or review reason changes, the Recommendations page exposes a
separate refresh action. It runs one bounded profile build under the existing
taste single-flight lock; prepared candidate descriptors are reranked in code
on the next read, without legacy per-listing score calls. The established
Retrain taste action remains the explicit path that also rebuilds legacy Taste
scores. Queued/running, failed and dirty states are named separately so a dirty
profile is never presented as if a refresh had already started.

Visual extraction is explicit and bounded. `services/visual_input.py` accepts
only hash-verified local raster attachments, exact approved portal image hosts,
or the exact property dossier host; redirects, credentials, query strings,
oversized bytes and oversized pixel dimensions are refused. The authenticated
bridge receives bytes rather than paths or URLs, creates private temporary
files for a Codex image call, and removes them after the call. Claude image
payloads are refused until its local attachment route is proven. A photo cannot
support parcel boundaries, exact dimensions, legal status, or absence outside
the frame. The schema sent to Codex uses its flat supported structured-output
subset, and the prompt contains only a bounded image index/source/hash manifest,
without URLs or bytes. The app then enforces the aspect-specific vocabulary,
unknown/null and parcel-outline rules, and exact image provenance before
anything can persist. `utils/extract_visual_descriptors.py` is dry-run first,
requires explicit ids and row/image/call caps, and discards a result whose
property or image fingerprint moved before the final locked write. Exact
property and image fingerprints also leave a completed row out of later model
calls; per-row commits make that bounded scope honestly resumable under the
shared in-flight marker.

`services/taste_evaluation.py` builds an unlabeled held-out manifest. It removes
learning rows, favorites, recorded verdict/activity and duplicate stored entity
identities, freezes descriptor fingerprints, and keeps the old Taste/Similar
baseline beside the new ordering. “No recorded activity” is not presented as
proof the owner never saw a listing. Owner utility remains `not_measured` until
blind ratings are collected without feeding those labels back into the profile.

The recommendation mode currently builds descriptors for the full filtered
candidate set before pagination, and the page-state path separately reads the
current signal basis. The isolated four-row artifact rendered in about 0.17 s;
production-scale request cost has not been profiled. Reusing that request-local
basis is a performance follow-up if measurement shows it matters, without
changing ranking or freshness semantics.
