# Listing status and the checker

Moved verbatim from `CLAUDE.md` (lines 683–729 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Historical text is amended below for the owner-requested availability checks (#608). "Above", "below" and "this file" refer to the order the
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
this changes the #136 storage contract: an `error` never changes status, its source, removal date or
`listing_last_checked`. A separate `enrichment.listing_status_check` records the
URL, start/completion timestamps, observed answer and refusal; readers disclose
the latest failed attempt instead of presenting an old Live badge as current. The breaker is process-local and shared by every
instance (each caller builds its own service), which makes it exactly the kind
of state `tests/conftest.py` resets between tests — it is reset there, and
skipping that reset makes 21 tests in three other files fail with no reference
to the file that armed it. As of #608, the existing daily job checks **up to 30 visible, active-subscription
`Property` favorites** with active/unknown status. The favorites API uses the
configured catalog (up to 50), always queues in production, and coalesces
identical jobs. Oldest attempts go first; a refused host stops for this run,
while other hosts remain eligible. Removed/sold favorites remain reference
examples, never automatically resurrected. The legacy check-all path still
selects `Land`; this change does not sweep the full Property catalog.

The single-property writer validates a clean attached session before fetching,
then uses the existing `locked_write` primitive after fetching to preserve
concurrent enrichment. A changed URL, status provenance, confirmation date or
attempt supersedes the in-flight result. No lock spans network I/O. Unsupported
sources never fetch or confirm. Status badges distinguish stale confirmations
(older than the existing 30-day threshold), blocked attempts and confirmed
availability; the detail shows the attempt separately with a Madrid timestamp.

Live evidence, 2026-11-07: both requested Idealista checks returned `blocked`;
the isolated automated Chrome profile on the approved MacBook also returned HTTP 403. The owner’s ordinary Chrome profile opened both pages. These attempts
are evidence of a verification failure, never evidence that either house is
available or removed.

The separate [shared-IP incident](portal-network-safety.md) keeps Yaencontre
offline in `observe` itself, before the transport or in-memory breaker. This
covers the legacy Land path and direct callers as well as the Property
source filter. A temporary-access restriction body is `blocked`, including
when delivered with HTTP 200; it cannot establish availability. The archived
MacBook bulk reader remains paused and is not a fallback for failed checks.
Known-refused Idealista HTTP is also stopped before I/O. Other supported sources
need an explicitly configured isolated portal proxy; missing or failed routing
cannot send a check over the owner's ordinary network.

## Browser publication checks after an HTTP refusal (#610)

`LISTING_BROWSER_FALLBACK_ENABLED=true` enables the existing `Property` writer’s
second reader for Idealista only when the owner checks a single property.
The favorites batch (scheduled or requested) never opens owner Chrome. HTTP
and browser refusals have independent host breakers. Only a technical challenge
frame/script or an exact document challenge title arms browser backoff; seller
prose never does. A browser page-parse failure stays with that listing. Browser failure/busy or missing
configuration preserves the previous confirmation and does not arm a portal
breaker. The attempt records `method=browser_archive` and the original HTTP
refusal. Other portals retain their existing readers.

The authenticated host bridge’s `/v1/listing-status` route calls
`tools/browser.sh --read-listing-status URL NONCE` on the fixed MacBook. It uses
an already running normal Chrome and a new owned window/tab. An app-name
AppleEvent can land in Playwright's Chrome if both processes are open. The
reader requires one running stock Chrome executable with no launch arguments,
owned by the current user, and binds every event to its PID and launch time.
Playwright's separate process is excluded even when it has focus. A missing,
ambiguous or replaced process fails without launching or switching browsers.
No JavaScript permission, login, profile, clipboard or existing tab changes. Chrome saves the
loaded page as MHTML to its own private temporary directory; only primary public
fields leave the MacBook, and the archive is deleted. Both the archive snapshot
URL and its main HTML URL must match the freshly generated nonce URL. The
canonical listing ID, primary title and positive price must agree; a challenge,
redirect, duplicate primary field or ambiguous page confirms nothing. No HTTP
code or transfer size is invented. This confirms that the advert is published
on the source; only the advertiser can confirm a sale has not happened privately.

The remote reader has a 30-second watchdog plus at most 4 seconds to close only
its own tab; the host bridge has a 50-second limit and the client a 75-second
socket allowance. Cleanup accepts only the initial blank, the requested nonce URL or its exact
same-listing canonical URL (query-stripping redirect). A different destination
is left alone because it could be an owner navigation; additional tabs are
preserved. A timed-out navigation still records its intended URL before sending
the AppleEvent. Complete invalid archives return their actual refusal immediately;
only unfinished MIME is polled again. The
MacBook must be awake, reachable and have Chrome running; there is no mini
browser fallback. The route works with JavaScript through Apple Events disabled.

Real pre-deployment reads on 2026-11-07 matched Idealista 93478124 at €285,000 and
108812772 at €295,000 through this actual helper. Rollback: disable the flag and
let the watcher redeploy; HTTP checking and failed-attempt disclosure remain.
Host bridge code updates require restarting its existing LaunchAgent only while
idle; Docker deployment remains owned by the watcher.


Each new Chrome window receives an unpredictable `given name` in the creation
command. If the creation reply is lost, cleanup queries only that marker and
recovers the uniquely tagged initial tab; a sole blank/new-tab fallback is
allowed, while other destinations and ambiguous matches are preserved. No
owner window URLs are enumerated. Creation is never recovered unless it was
actually attempted.

Cleanup uses `ExitStack` in this order: block handled signals, disarm the
watchdog, close the owned tab, delete the private archive, then restore signal
handlers and the prior signal mask. The watchdog cannot interrupt archive
deletion; pending cancellation is delivered only after mandatory cleanup.

Process-directed signals delivered through a background thread are also deferred
by the Python handler while cleanup is active, then re-delivered to the restored
handler after deletion. A pthread mask alone cannot protect Python's main-thread
signal dispatch in that case.

## Alternate publication after an Idealista refusal

Chrome can archive a DataDome iframe under a `cid:` URL. For a page
without primary listing fields, the reader resolves that CID against the
archive's unique HTML part and recognises the HTTPS DataDome host. It does
not follow or request the frame.

When a separately checked Yaencontre or Fotocasa advert has current
public-index evidence, the manual property action uses that evidence after
an Idealista refusal. The detail links the alternate advert and gives the
Madrid observation time. It does not refresh Idealista's confirmation date,
change the primary status or infer sale/withdrawal from an alternate.

The app performs no Yaencontre or Fotocasa HTTP calls for this fallback:
both can refuse its route. A trusted operator can refresh evidence after
reading the public indexed page and matching the same property:

```sh
python -m utils.record_alternate_publication PROPERTY_ID IDEALISTA_URL ALTERNATE_URL OBSERVED_AT_ISO
```

The exact Idealista URL must still match under the property row lock.
Yaencontre must also have the same syndicated listing ID. Fotocasa has its
own ID and requires the operator to establish the match. Only observations
from the last 24 hours are shown; dates are never advanced by checking
cached evidence. Terminal primary statuses suppress alternate evidence.
