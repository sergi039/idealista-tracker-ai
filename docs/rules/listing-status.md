# Listing status and the checker

Moved verbatim from `CLAUDE.md` (lines 683–729 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**`listing_status` is `active` by default, so `active` is only shown when
somebody established it.** The column is written at ingestion and nothing
verifies it; measured 2026-08-15, 1 of 311 land rows had ever been checked, and
`/properties` drew the other 310 exactly like confirmed live listings — property
192, withdrawn by the advertiser on 08/05/2026, among them. That is #98 in the
status column: an absence of measurement rendered as a measurement.
`services/listing_verification.py` owns the rule and is the only thing the
surfaces read. `removed`/`sold` always show, because no writer sets them by
default; `active` shows only with `listing_status_source` of `check` (the
scraper read the page) or `manual` (the owner looked); everything else,
including `ingest`, NULL and the stored `unknown`, presents as **`unchecked`**.
That is a fourth *presentation* state, not a fourth database value — no
migration, no row rewritten, the database keeps what it knows. The module holds
both readings of the rule, `read_verdict` for a row and `verified_expression`
for a query, because the list draws them together: the coverage line beside the
result count ("3 of 4 verified against Idealista") is the disclosure an
unchecked row cannot make for itself, since badging ~100% of the table
"unverified" is noise, and a header disagreeing with its own badges would be a
third wrong number. A check older than 30 days keeps its badge and loses the
green — it verified something, just not about today. The CSV export and
`to_dict` carry the verdict too; a report built off the raw column is how a dead
listing got recommended.

**Idealista blocks the checker from this machine, and the app says so instead of
retrying into the wall.** Measured 2026-08-15 over 76 properties, one at a time
behind the service's own throttle: every call hit DataDome, zero listing pages;
`curl` from the *host* with full browser headers gets 403 with the same block
body, so it is not the container, and only the owner's real logged-in Chrome
renders a listing. Defeating that is not on the table — it is bot-detection
circumvention, and a headless profile would be one more thing to lose to
DataDome's next update — so the honest half is what shipped. `RefusalBreaker`
(`services/listing_status_service.py`) counts refusals *across* calls, and after
three in a row the service answers from what it already knows for 30 minutes
instead of spending a request per press; the cooldown buys back exactly one
probe, and a refusal re-arms it, because it heals on evidence and not on a
timer. Each refusal carries a reason — `blocked`, `backing_off`,
`not_the_listing_page`, `http_error`, `timeout` — so the page can say "idealista
is refusing this machine" rather than reporting 76 unrelated failures. None of
this changes the #136 storage contract: an `error` still writes nothing, not
even `listing_last_checked`. The breaker is process-local and shared by every
instance (each caller builds its own service), which makes it exactly the kind
of state `tests/conftest.py` resets between tests — it is reset there, and
skipping that reset makes 21 tests in three other files fail with no reference
to the file that armed it. There is still **no scheduled sweep for
`Property`** and no `check_all_active_properties` to run — the bulk paths select
on `Land` — so the per-listing button and the hand-set status are the way in.
