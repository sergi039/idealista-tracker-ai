# The listing surface and its subscriptions

Moved verbatim from `CLAUDE.md` (lines 238–414 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**There is exactly one listing surface: `/properties`** (owner decision,
2026-08-09, superseding the 2026-08-08 one that kept `/lands` as a second,
archived page). `/`, the navbar and `/lands` itself all lead there, and that
is where new work goes. The reason is where the data is. `lands` froze at 168
rows with nothing newer than 2026-02-18; every ingested listing goes to
`properties` (`INGESTION_TARGET` defaults to it), and the legacy `Land` model
cannot represent the houses that now arrive at all, so switching ingestion
back was rejected.

`/lands` is a redirect, `templates/lands.html` is gone, and **the archive
banner it carried is gone with it** — the owner never asked for it. Do not
reintroduce a second listing page. The legacy rows are still readable: they
are mirrored into `properties` under the "Legacy Lands" subscription, which
is inactive and therefore offered in the subscription filter under *Archive*.
`Land` itself stays for `/lands/<id>`, `/export.csv` and the migration
service; nothing else reads it.

**Subscriptions are the organising idea of that surface.** They are
`search_profiles` rows — the owner's saved searches on idealista.com, matched
to incoming mail by search URL (`source_search_key`, #102) and by label. The
filter offers them in one dropdown: live ones first, retired ones under
*Archive*, each with its listing count; a subscription holding nothing (the
`Default` catch-all) is not offered unless it is selected. A bare
`/properties` shows **every live subscription at once** — it used to open on
one profile picked for the owner, which hid the other saved search behind a
control they had to know about. `profile_id=all` means the active ones only,
so retiring a subscription (`is_active = false`) is what moves it into the
archive without touching its listings. When more than one is on screen the
rows carry a subscription badge, and the travel columns fall back to the
preset targets — a *custom* target id belongs to one profile, so it would
label a column with a destination most rows were never measured against.

**Retiring a subscription and hiding one are different questions** (owner
request, 2026-08-17). `is_active = false` *archives*: the subscription leaves
the chips and is offered under *Archive*, one tick away, because a saved
search that stopped still holds listings worth reaching. That is the wrong
answer for a search that is still running and that the owner does not want on
screen — production carries fourteen subscriptions, eleven of them active,
three of those created by the ingester and holding one listing each, and every
one takes a chip on the one working page. So `search_profiles.is_hidden`
(migration 020) takes a subscription off the screens: no chip, no menu entry,
not under *Archive*, and its listings are out of `profile_id=all`, which is
what /properties, /map and `properties/export.csv` all define "all
subscriptions" against. The control is on `/profiles` and only there — the
page that lists every subscription side by side, including the hidden ones,
because a page that hid them from their own control would leave no way back.

Four things about it are deliberate. The rule has one home,
`SearchProfileService.visible_clause()` / `list_visible_profiles()`, and
`list_profiles()` still returns **everything** by default: ingestion reads that
list to match an email against each profile's `email_matchers`, and a hidden
subscription that stopped matching its own mail would send those listings to
the catch-all — a data change wearing a UI change's clothes. A hidden id named
explicitly in `profile_id=<id>` still renders, under its own heading in the
menu with its own checkbox, because a selected id with no checkbox reads as
"nothing ticked" to the page's own script and the next Apply would silently
widen the view (the same reason an unknown id keeps one, #104). The menu
carries a line saying how many subscriptions and how many listings it is not
showing — the owner chose this, so it is a disclosure and not a warning, but
without it "all subscriptions" reads as the whole table. And the **catch-all
cannot be hidden at all**: it receives every email that matches nothing else,
so hiding it would take listings off the page as they arrive, which is why
`edit_profile` already forces the default profile active — and since #533
the database refuses it too: `ck_search_profiles_catch_all_never_hidden`
(migration 028) is the hiding half of 025's
`ck_search_profiles_catch_all_never_routes`, on the same shape, because
curation SQL through `docker exec` never meets a route. A CHECK on a pair
refuses the pair from either side, so a hidden subscription cannot be made
the default either, and `edit_profile` says so instead of answering a 500.
`tests/test_hidden_subscriptions.py` pins the UI half and
`tests/test_postgres_migrations.py` the constraint — SQLite never runs the
migration, so only the PostgreSQL suite can prove it.

**And routing one is a third question, because hiding does not stop mail**
(#502, owner request 2026-08-31). Hiding takes a subscription off the
screen; its alert keeps arriving and its listings keep landing on it. The
six idealista Galicia alerts each create their own profile — that is #102's
identity design, one `source_search_key` per saved search, and it is not
negotiable — so the owner woke up to four chips where he had asked for one,
and four more alerts still to deliver. `search_profiles.routed_to`
(migration 025) is the answer: the stub keeps its saved-search identity and
its listings live on the target.

**The enforcement is a database trigger, not a rule in the writers**, and
that choice is the whole of it. `BEFORE INSERT OR UPDATE OF
search_profile_id ON properties` canonicalizes through `routed_to`, exactly
one hop, reading the route under `FOR KEY SHARE` — so a listing inserted
while a route is being set waits for that decision instead of racing past
it. Writers are legion here (`resolve_profile`, the paste import, three
portal email doors, `run_full_sync`, the `Land` mirror, and curation SQL
through `docker exec`, which is a supported workflow), and the trigger is
the one layer they all share. `SearchProfileService.canonical_profile()` is
the readable first line of defence and `fotocasa_import.build_property()`
applies it too, because SQLite runs no trigger and the suite is SQLite.

`route_profile()` is the ONE writer of `routed_to`, and every refusal it
carries is a defect somebody reproduced first: a self-route; the catch-all
on either side (it receives everything unmatched, so redirecting it would
move all unrouted mail silently); a target that is itself routed, and a
source something else already routes to (chains, both directions); and
**re-pointing a stub that is already routed** — that one answered
`ok, moved 0` while leaving the old target's listings behind and sending
future ones elsewhere, a split wearing a success message. Both rows are
locked `FOR UPDATE` in ascending id order and the existing listings move in
the same transaction, so a route is never half-applied.

`auto_route_from_pattern` lives on the TARGET (`^Galicia\s` on production's
profile 24): a profile ingestion auto-creates whose name matches is born
routed and hidden. That is what stops the four undelivered alerts from each
putting a chip back, and it is why the pattern may not sit on a routed stub
— the CHECK refuses that, because a pattern on a stub would chain.

**The owner's criteria are enforced here because no portal can express
them.** He asks for a house of at least 150 m² on a plot of at least 700,
and not one of the four portals filters by plot — measured when the alerts
were created. `search_profiles.criteria` holds the bounds and
`services/subscription_criteria.py` answers `pass` / `fail` / `unknown` in
Python and in SQL, branch for branch (the `advertiser.py` contract). Four
things about it are load bearing:

* **`unknown` is not `fail`.** A plot nobody has stated is not a plot that
  is too small, and the default view hides only *measured* fails. On
  production 2026-08-31 that is 57 hidden of 434, with 375 unknown and 2
  passing — and "2" is honest rather than disappointing: `pass` needs BOTH
  figures, and `properties.plot_area` exists only where fotocasa states it.
  `utils/backfill_plot_area.py` filled 82 rows across the table (free,
  paced 30 s, and it records a measured "page states no plot" so a run
  stops re-fetching pages that already answered).
* **A row the owner has judged is never hidden.** Favorited or reviewed
  beats the filter: a listing somebody already decided about must not
  vanish because a bound says so.
* **Every SQL clause is definite**, never NULL. `unknown` is `~fail AND
  ~pass`, and one NULL-able comparison would silently eat rows from it —
  which is exactly what an unassigned listing (`search_profile_id = NULL`)
  did before the membership guard went in.
* **NaN is not a measurement, and PostgreSQL disagrees with Python about
  that.** `NUMERIC 'NaN'` sorts ABOVE every number, so `plot_area > 0 AND
  plot_area >= 700` is TRUE for a NaN and SQL called such a row a `pass`
  while Python (`nan > 0` is False) called it unmeasured. One credibility
  rule now serves both — present, positive, under `MAX_CREDIBLE_M2` — and
  the migration refuses the write outright for the new column.

**What this cost, in one line each, because each is a class rather than an
example.** `.with_for_update().count()` passes every SQLite test and
PostgreSQL refuses it outright ("FOR UPDATE is not allowed with aggregate
functions"), so the one writer of `routed_to` would have been dead on
arrival in production — **migration-touching code is proven against real
PostgreSQL 15, the version `idealista-db` runs**, and a green SQLite suite
is not evidence about it. Both paths reach that same 15: CI's `pytest` job
runs `postgres:15-alpine` as a service with `REQUIRE_POSTGRES_TESTS=1`, so
a missing server fails the job instead of skipping it, and
`tools/ci/migration_test_db.sh` raises a throwaway of the same image for a
local run. One asymmetry between them is deliberate and should not be
"fixed": CI pins that image **by digest** while the script and
`docker-compose.yml` use the bare `postgres:15-alpine` tag, because the
throwaway exists to reproduce `idealista-db` and therefore has to move when
the deployment moves. **Pin both or neither**: digest-pinning the
script alone makes the test server *more* frozen than production, which is
this rule's own divergence pointed the other way, and it is the tidy-looking
change a future reader will reach for first.
A compact form that snapshots a field into hidden inputs erases whatever
another tab set in the meantime, so the comment card sends `keep_action`
and `set_review` re-reads the action under its own lock. And a test can
pass for the wrong reason: the first NaN test asserted against criteria
whose *other* bound was false for that row whatever the NaN did, and only
mutating the fix away exposed it.

`/properties` carries the controls the owner actually used on `/lands`: the
cards/list toggle
(`view_type`), the combined/investment/lifestyle modes (`mode`, with
`score_total` / `score_investment` / `score_lifestyle`), the investment-rating
filter (`inv_metr`), and the Score / ★ / Title / Price / Area / Coords /
Travel / Inv. Metr. / Type / Added / Actions table. A bare `/properties`
opens on **that table, not the cards** (owner decision, 2026-08-09;
`DEFAULT_PROPERTY_VIEW_TYPE` in `routes/main_routes.py`) and still sorts by
date so the freshest listings stay on top.
