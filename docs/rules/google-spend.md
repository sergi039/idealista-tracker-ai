# Google spend: flags, the one door, the cap

Moved verbatim from `CLAUDE.md` (lines 1901–2124 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- **Nothing unattended spends Google money any more** (owner decision
  2026-08-17, after a billing overrun). `AUTO_TRAVEL_ENRICHMENT` defaults to
  **false**; it was `true`, and it was the only automatic caller of a billed
  Google API in this repository — everything else is behind a button press or
  a CLI backfill. One automatic run was, at the time, 6 preset Places Nearby
  lookups + 1 for the beaches + a Distance Matrix request of ~26 elements:
  about **$0.36 a listing**, twice a day, for however many alert emails
  arrived. (Since then the presets and beaches moved to the register and
  OpenStreetMap, and with `OSRM_URL` set — the mini since 2026-08-20, #416 —
  the Distance Matrix leg itself is answered locally for nothing; the flag
  stays false anyway, because the rule is about what runs unattended, not
  about today's price.) Travel is measured on request now — the Enrich button
  on `/properties/<id>`, or `utils/recalc_property_travel.py`.

  What made it expensive rather than merely wasteful is that **the scheduler
  ran on both machines against one mailbox.** `AUTO_START_SCHEDULER` used to
  arrive as `true` in every container whatever the machine intended, so the
  laptop ingested the same alert emails as the mini and paid Google a second
  time for every listing — into a database that is thrown away and restored
  from the mini's dump. Measured
  2026-08-17 from both databases: the mini's ingest is ~7 listings a day, and
  on 2026-08-16 four new saved searches delivered **306 listings to the laptop
  between 07:00 and 10:00** — roughly $110 of Google credit in one morning
  that nobody asked for and nobody read. A dev checkout must not run the
  scheduler; the laptop's `.env` says `AUTO_START_SCHEDULER=false`, at the
  cost of `/api/healthz` answering 503 there, which is correct and is what
  `.env.example` already warned about.

  **Every place that decides that default is fail-closed** (#376). `config.py`
  had defaulted it to false outside `DEV_MODE` all along and it made no
  difference: `docker-compose.yml` set the variable in the container
  environment as `${AUTO_START_SCHEDULER:-true}`, so the code never saw an
  unset variable, and `docker-compose.dev.yml` forced a flat `true` that won
  the Compose merge over whatever the machine's own `.env` said — through
  `docker compose -f docker-compose.dev.yml up`, the workflow documented under
  *Run* above. All three now say false, so an environment that says nothing
  produces no ingester, and the machine that IS the deployment says so in its
  own `.env`. Two consequences worth knowing: a fresh clone or worktree
  copying `.env.example` is silent by default and needs the flag set on
  purpose to ingest; and `docker inspect` cannot tell you which machine is
  which, because it shows the variable in the container environment without
  distinguishing a compose default from a value in `.env` — that mistake was
  made here and nearly stopped production ingestion on deploy.
  `tests/test_scheduler_flag_fails_closed.py` pins the three places.

  **A machine that does not ingest on a tick does not ingest on a click
  either** (#388). The flag governed the *scheduler* and nothing else, while
  `POST /api/ingest/email/run` — the Manual Sync button, CSRF-exempt and
  behind no authentication — read the same mailbox on one press regardless of
  it, with a 5/minute rate limit as the only friction. The rule now has one
  home, `services/ingest_policy.py`, and two readers: the endpoint refuses
  with 409 and a reason, and the control is absent exactly where the endpoint
  would refuse. No second flag was introduced — the configuration already
  answers "is this the machine that ingests?", and a second flag is a second
  thing to forget.

  Three things about it are load bearing. The guard is the endpoint's **first
  statement**, before the request body is parsed and before any service is
  constructed: a guard that refuses after the mailbox is open has already read
  the mail. It reads `app.config`, **where the scheduler reads it**
  (`services/scheduler_service.py`, `should_start_scheduler`) and not the
  `Config` class — those are two separate readings of the environment taken at
  different moments, and a guard consulting the other one could refuse a manual
  run on a machine whose scheduler is running, which is the very disagreement
  the module exists to prevent; four pre-existing tests set
  `app.config["AUTO_START_SCHEDULER"]` and none set the class attribute. And
  the control lived in **three** templates, not one — the navbar, the empty
  state of `/properties`, and Full Sync in settings — so a test that checked
  only one surface found the other two; where the button goes the copy goes
  with it, since the empty state used to read "run a manual sync to fetch new
  listings" directly above it.

  What this does **not** close, and the module says so in its own docstring: an
  ad-hoc script run through `docker exec -i idealista-app python -` builds the
  service directly and never reaches Flask. That is not hypothetical: 326 rows
  across five hand-made subscriptions were written into the laptop's database
  that way, entirely outside the email pipeline, and had to be merged into the
  deployment's database by hand on 2026-08-17. The boundary
  here is the interface, not the process. Also note there is no CLI path for
  ingestion at all, so on a non-ingester machine ingestion is unavailable
  outright — debugging it locally means setting the flag on purpose.
  `tests/test_manual_ingest_needs_an_ingester.py` pins the refusal, that the
  mailbox is never touched, the guard's position, both surfaces, and the
  `app.config` precedence.

  **`AUTO_GEOCODING` is a separate flag and stays on**, because the paid step
  was also the *geocoding* step: `calculate_for_property` opens with
  `ensure_coordinates`, so switching travel off silently took the coordinate
  with it — and with the coordinate the sea distance, the sea-view verdict,
  the OSM amenities and the quality-of-life block, four free measurements lost
  to a flag about a paid one, leaving a row that reads "nothing nearby" when
  the truth is "nobody looked". That is #98's defect arriving through the back
  door of a cost control. Geocoding is $0.005 a listing, ~$1 a month here
  against ~$75 for the travel step as it was billed then — a figure from before
  #416; with `OSRM_URL` set the routing itself is free, and the comparison
  survives only on a machine where Google still routes. Set it false only for
  a machine that must
  reach no Google API at all; ingestion then makes no billed call whatsoever.
  `tests/test_paid_google_is_on_request.py` pins the defaults (read from a
  clean interpreter, not from the suite's own patched `Config`), that
  ingestion fires no Places/Distance Matrix call, that it still geocodes
  exactly once, and that turning travel back on does not geocode twice.
  `tests/conftest.py` forces `AUTO_GEOCODING` off per test — nine ingestion
  modules assert something else entirely and mock no geocoder, and left on
  they reached live Nominatim.
- **Enrichment that spends money happens only on the owner's explicit
  request, and never on an agent's initiative.** This is the owner's standing
  rule (2026-08-26) and it outranks every convenience below it. "Explicit"
  means the owner asked, in this session, for *this* run: a page's Enrich
  button pressed by them, or a command they typed or told you to type. It does
  not mean an agent decided a row looked incomplete, that a backfill "would be
  cheap", that a scope was "only a few listings", or that a previous run's
  authorization covers today's. An approval given once for one scope is not an
  approval for the next one — the same rule the ship gates already state, in
  the one place where getting it wrong sends an invoice. When in doubt, ask
  and wait; a measurement not taken costs nothing and a listing can be
  enriched tomorrow.

  Announce before you spend, the way the backfill protocol already requires:
  name the module or endpoint, the rows, and the cost. Report what was
  actually spent afterwards, from `data/google_spend.jsonl` and not from
  arithmetic over the price list — the second thing is what produced the
  "$0.36 a listing" figure this file carried for weeks, which was never a
  reading from billing.

- **There is exactly one door to a billed Google API: `utils/google_spend.py`**
  (2026-08-26). It is the only module in the tree that may name a
  `maps.googleapis.com` URL, and `tests/test_google_spend_is_authorized.py`
  greps for a second one. Every billed call — Places Nearby, Places Text
  Search, Distance Matrix, Geocoding — goes through `billed_get(api, ...)`,
  which takes an `api` constant and never a URL.

  Before this there were **eleven** billed request lines across four files
  (`property_travel_service` ×3, `travel_time_service` ×3, `enrichment_service`
  ×3, `geocoding` ×2), reachable from three HTTP endpoints, seven CLI tools,
  the background-job executor, both ingest paths and the scheduler. Two
  `Config` booleans guarded exactly one of those paths.
  `POST /api/lands/enrich-all` guarded none of it: unauthenticated,
  CSRF-exempt, rate-limited only at 2 per 5 minutes, and it looped
  `enrich_land` over every row with an empty enrichment column. Measured
  2026-08-26, no template and no static asset called it — a loaded gun nobody
  was carrying. It answers **409** now and names the CLI that does the same
  work with a reason attached.

  Four things about the design are load bearing.

  **The authorization is ambient and defaults to absent.** A `contextvars`
  value, for the reason `utils/http.lookup_budget` already gives about
  threading a parameter through eleven call sites: "a parameter every one of
  them has to forward is a parameter one of them will not". The difference
  that matters is the default — an unset *budget* means no ceiling, which is
  safe because it costs time; an unset *authorization* means no, which is safe
  because it costs money. A path nobody thought about is refused by
  arithmetic rather than by review.

  **A refusal is a `requests.RequestException`.** All eleven call sites
  already wrap their request and hand the exception to
  `failure_from_exception`, so a refusal records an honest "nobody looked"
  (#98) rather than a measurement, and refusing cost no new branch anywhere.
  `failure_from_exception` checks `PaidCallRefused` *before* the generic
  branch, or a decision about our own wallet would be reported as
  `network_error` and send an operator to Google's status page.

  **The routes open theirs inside the job closure, not around `_enqueue`.** A
  `contextvars` value does not cross into a thread the background executor
  starts, and that property is relied upon rather than tolerated: an
  authorization that outlived the request granting it would be exactly the
  ambient permission this removes. `tests/test_google_spend_is_authorized.py`
  pins it with a real thread.

  **Retries are charged.** `request_with_retries` may issue the same request
  three times and each attempt is one Google may bill for, so `billed_get`
  counts attempts rather than trusting the nominal figure — otherwise the
  ledger under-reports in exactly the situation where somebody is reading it,
  which is an API that is throttling.

  A billed CLI tool carries `--reason` (`add_spend_arguments` /
  `cli_authorization`) and refuses to start without one, at the top, before it
  walks its scope — a per-row refusal would be correct and still terrible,
  rewriting hundreds of listings to say "nobody looked" because an operator
  forgot a flag. `GOOGLE_SPEND_ENABLED` is the second, outer lock, for a
  machine that must never spend whatever its code does (a dev checkout, a
  worktree); it defaults to **true** because defaulting it false would stop
  the deployment's own Enrich button on the deploy that shipped it, which is
  the mistake already on record for `AUTO_START_SCHEDULER`.

  What it does **not** close, stated because a guard presented as complete is
  worse than one known to be partial: it is not authentication (there is
  none, by owner decision), so whoever can reach the per-property Enrich
  endpoint can still cause the spend that endpoint authorizes — bounded by a
  cap and attributed in the ledger, which is the change. And it cannot see a
  process that never imports it: a `curl` to Google, or a script building its
  own `requests.get`. The boundary is the transport, not the machine — the
  same sentence `services/ingest_policy.py` has to make about the interface.

- **The geocoding rule below still holds, and the gate was built not to break
  it.** `AUTO_GEOCODING` stays on and ingestion opens its own authorization
  for it by name, so the ingest path is unchanged. On a path that opens *no*
  authorization the billed geocode is refused — and `geocode_address` falls
  through to Nominatim, which is free, so the row gets a coordinate **when
  Nominatim answers**. That last clause is not a hedge, it is the honest
  limit: an independent review caught this paragraph claiming the coordinate
  unconditionally, and a Nominatim that finds nothing still returns `None`
  exactly as it did before this gate existed. What the gate changes is
  nothing about that path — the harm the rule names is a row with *no*
  coordinate *because a cost control removed the call*, and a refusal here
  costs precision rather than the attempt. Pinned by a test.

- **A cap reserves the worst case, not the nominal cost** — `units *
  MAX_ATTEMPTS_PER_CALL`, refunded the moment the attempt count is known. The
  first version charged the nominal figure up front and the retries
  afterwards, which reads as careful accounting and bounds nothing: a cap of
  one unit funded a request `request_with_retries` issued three times, because
  the two extra attempts were charged after Google had already been sent them.
  In the same family, the check and the charge are **one** operation
  (`_reserve`, under a single lock) — reading the remaining cap and then
  charging it is a check-then-act race, and with `--workers 1 --threads 4` two
  threads both read "room for one", both passed, and both billed.
  `spend_verdict()` survives only as an advisory reader for surfaces deciding
  whether to draw a button, and says so in its own docstring; it is not the
  gate. All three of these were found by the Tier 2 independent review of the
  change that introduced them, not by its tests — which is the argument for
  the gate in one line.
