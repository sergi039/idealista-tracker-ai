# The pool criterion

Moved verbatim from `CLAUDE.md` (lines 848–911 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**The pool criterion ships weightless and is live in production anyway**
(proposal D17, #278; turned on on the mini 2026-08-14). Every category in
`services/property_scoring_service.py` carries `pool_score: 0.0` in both
`DEFAULT_INVESTMENT_WEIGHTS` and `DEFAULT_LIFESTYLE_WEIGHTS`, so reading the code
tells you the criterion is off — and on the Mac mini it is not. The weight lives
in data: `scoring_config.categories.<cat>.lifestyle.pool_score = 0.1`, all six
categories, on the three subscriptions that carry a `scoring_config` at all —
`Default` (1), `Land at Norte` (6), `houses at your custom search area norte`
(8). The fourth live subscription, `Asturias` (11), has none and therefore still
scores at the shipped 0.0. A weight set on the *lifestyle* branch alone still
moves `score_total`, because the combined score is the saved investment/lifestyle
mix (#257) — the number the list sorts by. So a score that changed under you is
not necessarily a code change, and `git log` will not explain it.

**Rolling that back is a data restore, not a deploy.**
`data/pool_weight_enable_snapshot.json` on the mini (2026-08-14T17:48:43Z) holds
the three profiles' previous `scoring_config` — `null` for each — plus the score
columns and `scoring` payload of all 393 rows as they stood before. Deleting it
is not tidying up; it is discarding the only way back.

`utils/restore_score_snapshot.py` puts both halves back, in one transaction,
because weights restored without their scores (or the other way round) is a
state the app never had. It **reports and exits** unless `--apply` is given,
writes a backup of what it is about to overwrite first (`--no-backup` is a
thing you say out loud, never a default), parses every row before writing any,
and restores exactly the columns a row carries — this snapshot has no
`enrichment`, and nulling it would erase measurements the weight change never
touched. Rows ingested *after* the snapshot are the honest hole: they were
scored under the config being rolled back and nothing knows their earlier
values, because they had none, so the tool names them and `--rescore-uncovered`
recomputes them under the restored config. The snapshot primitives now live in
`utils/score_snapshot.py` — `backfill_pool`, `recalc_sea_distance` and
`recalc_property_travel` had three copies of them and this was nearly the
fourth.

**Turning the weight on is deliberately not an ordinary save**
(`routes/main_routes.py`, action `confirm_pool_scoring`). A save that raises
`pool_score` above 0 re-scores every listing in the subscription, so it first
runs a dry preview — stage the config, rescore in the session, count the rows
that move and the mean shift of the combined total, then `rollback()` — and
stores the pending config together with **the baseline it diffed against**. The
confirm applies it only while the stored config still equals that baseline; an
ordinary save in between drops the pending preview rather than silently
reverting the newer weights. Every score column is diffed, not just lifestyle:
the weight can go on the investment branch and move investment and the total
while lifestyle sits still, and a preview reporting "0 would change" ahead of a
mass rescore is worse than no preview. Do not collapse this into a direct save.

**The pool datum is honest-absence, like sea view** (`services/pool_service.py`).
Only a *measured* drive time scores. OSM finding nothing triggers one budgeted
Places Text Search cross-check and the verdict becomes `unverified_absence` —
component `None`, never 0, because one text search proves nothing about
completeness; the only path to a true 0 is the owner's hand-set `owner_no_pool`
flag, which outranks every computed state and survives recomputes. Indoor is
evidence with a source — `verified` (`covered=yes`), `likely` (a building, or
"climatizada" in the name), `unknown` for silence — and the require-indoor
toggle is applied by the *scorer*, so narrowing or widening it never rewrites
the evidence. A refusal never overwrites measured candidates.
`utils/backfill_pool.py` covers the Phase-2 auto-scope (last 30 days plus
favorites, `utils/enrich_scope.py`), measures at most three drive times per
property (billed Distance Matrix elements only when `OSRM_URL` is unset —
free legs of the local routing engine otherwise, #416), and is resumable per
row; everything older stays manual via the Enrich button.
