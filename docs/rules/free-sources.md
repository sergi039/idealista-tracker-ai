# Free sources: Overpass, OpenTopoData, Catastro, and the lookup budgets

Moved verbatim from `CLAUDE.md` (lines 2125–2346 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- External APIs cost real money (Anthropic, OpenAI, Google Places /
  Distance Matrix). Never run bulk backfills (`utils/bulk_ai_analysis.py`,
  `utils/recalc_travel_times.py`) without an explicit ticket saying so.
  `utils/backfill_sea_view.py` and `utils/backfill_osm_amenities.py` are the
  exceptions that prove the rule: they spend nothing, because OpenStreetMap
  and OpenTopoData are free. They are still paced — overpass-api.de grants two
  query slots per IP and answers 504 while they are busy, and it refuses the
  default `python-requests` User-Agent outright — so keep the caching, the
  shared `utils/http.py` `OVERPASS_GATE`, and the bare product token
  `HTTP_USER_AGENT`, both used by `services/sea_view_service.py` and the OSM
  amenity call in `services/enrichment_service.py`. The amenity backfill calls
  `EnrichmentService.enrich_osm_amenities` **directly and never
  `PropertyEnrichmentService.enrich_property`**, which would fire the paid
  Google travel and Places calls once per row.
- **Pacing is passed to the transport, never taken around it.** Hand
  `gate=OVERPASS_GATE` to `request_with_retries`; it takes the gate before
  every attempt. A caller that wraps the call in its own `gate.wait()` /
  `gate.mark()` paces its lookups and leaves the retries unpaced, which is the
  traffic a struggling endpoint sees most of. Same for any other
  rate-limited endpoint: give it its own `RateGate`
  (`ELEVATION_GATE` in `services/sea_view_service.py` is the second one) rather
  than a hand-rolled `_last_call_at` global.
- **Overpass is paced at 5 s, and that number is measured** (#152 follow-up).
  A 20-property dry run at the previous 2 s spent 39 requests on 20 answers —
  15 of them refused with `429 Too Many Requests`, more than the 8 refused with
  `504`. Both are retried, so nothing was recorded wrongly, but a backoff is
  not a rate: keep `OVERPASS_MIN_INTERVAL_S` where the measurement put it, and
  re-measure before lowering it. It costs an interactive Enrich nothing,
  because the gate is idle between presses.
- **One public Overpass instance is a single point of failure, so there is a
  fallback list** (2026-08-19). Measured the night the presets moved onto
  Overpass: **overpass-api.de refused every connection from the Mac mini** --
  `curl` from the host itself timed out at 25 s, repeatedly, for over an hour
  -- while answering the laptop in 0.27 s. An IP-level block or throttle,
  brought on by that evening's own free backfills, which is exactly the traffic
  this project generates. `overpass.kumi.systems` answered the mini with 200 in
  3.5 s throughout and `overpass.private.coffee` in 2.1 s, and both are in
  `Config.OSM_OVERPASS_FALLBACK_URLS` now.
  It matters more than it would have a day earlier: an Overpass refusal used to
  cost an amenity count nobody scored, and since the presets left Places there
  is no paid path behind them, so an instance that will not talk to this
  machine means a listing measures nothing. Three things keep the list from
  being worse than none. A **406 does not fall through** -- that is this
  client's User-Agent being refused and every instance runs the same software,
  so moving hosts repeats it and doubles the traffic; a network error, an HTTP
  error and the `remark`-inside-a-200 do fall through, because those are one
  instance being unreachable or loaded. The failure returned is the **first**
  one, not the last: it names the instance the deployment is configured
  against, and "kumi.systems timed out" sends an operator to the wrong place.
  And the shared gate stays shared rather than becoming per host -- moving to a
  second instance because the first is loaded is not a reason to be less polite
  to the second. `tests/test_overpass_fallback_instance.py` pins all three.
  The lesson underneath is worth more than the list: **this project can get
  itself blocked by its own backfills**, and the moment a free source becomes
  load bearing that stops being an inconvenience.

  **Two fallbacks were not enough, and a list whose entries fail together is
  the single point of failure with more names on it** (2026-08-20). Measured
  at 22:30 CEST from the mini: overpass-api.de timed out on connect,
  kumi.systems answered `502`, private.coffee timed out -- all three, at once,
  while the laptop got overpass-api.de in 0.6 s. So it is the same IP-level
  refusal as 2026-08-19 and the spares did not cover it.
  `overpass.openstreetmap.fr` answered the mini in 0.24 s throughout and is
  now first among the fallbacks, ahead of the two that were down, because it
  is the only one that answered either machine that day.

  **An instance is added on evidence, never on a `200`.** The dangerous
  failure is not the one that refuses -- it is a thin or regional mirror that
  answers `200` with an empty `elements` list, which this project writes down
  as a *measured* absence: "nothing hazardous nearby" for a plot beside a
  cement works, #98's defect arriving through the spare tyre. That is not
  hypothetical and it is not rare: `overpass.osm.ch` was caught doing exactly
  it the same afternoon, in another session's hand-run override. So a
  candidate is checked against a **known answer** first. openstreetmap.fr
  returned the same 144 elements with the same tags as overpass-api.de and as
  the committed fixture for property 793's coordinate -- zero differences --
  and the same national chains in the same counts for a dense unrelated query
  over central Gijon. Two sources agreeing on an answer somebody already had
  is the check; "it responded" is not.

  **A new instance is not free, and the one place it costs is
  `OSM_OVERPASS_WALK_BUDGET_S`.** The ceiling is derived from the *number* of
  instances -- one patient attempt on the primary plus a complete attempt on
  each fallback -- so a third fallback moved it from 210 s to 275 s, and
  `ENRICH_LOOKUP_BUDGET_S` from 240 to 305 to stay above it. Leaving the
  ceiling alone would have bought the shorter walk by clamping the *last*
  fallback's read leg, and on the day this was measured the last fallback was
  the only one answering. `tests/test_one_press_is_bounded.py` derives both
  from `len(OSM_OVERPASS_FALLBACK_URLS)` and goes red if a future instance
  arrives without them.
- **Every Overpass caller reads three refusals, not one** (#144, all measured
  against the live instance): the `406` above, which also fires for a UA
  carrying a parenthetical comment; the `504` above, which needs a backoff in
  tens of seconds, not the half-second default in `utils/http.py`; and a
  server-side failure delivered *inside a 200* as
  `{"elements": [], "remark": "runtime error: Query timed out ..."}`. Reading
  `elements` off that last one writes a computed negative for a query that
  never ran, and caches it — the #98 defect, in a free API. Treat a `remark`,
  and a body with no `elements` list, as refusals. All three are already
  handled in `EnrichmentService._fetch_osm_amenities` and pinned by
  `tests/test_overpass_user_agent_and_refusal.py`; this rule exists so the
  next Overpass caller does not have to rediscover them.
- **An advisory step may not hold a paid one hostage, and every free lookup
  runs on a clock** (#434, measured on the mini 2026-08-20 on property 793).
  The owner pressed **Enrich**, saw nothing, pressed three more times, and
  sixteen minutes later the travel block filled in; the AI analysis never ran
  at all. One Overpass lookup spent **888 s** without completing a single
  request -- three instances x four attempts at a scalar 60 s timeout plus
  8+16+32 s of backoff each -- and the step spending it was
  `PoolService.enrich`, whose criterion ships at weight 0. Meanwhile the
  Distance Matrix request billed at 12:59 sat in an uncommitted session until
  13:12:55, behind it.

  **The retry policy splits by whether the server spoke, not by connect
  versus read.** `429` and `504` mean the instance is alive and busy, and
  #144's patient 8-16-32 budget is still right for them. Silence means the
  host is unreachable or hung, and a caller with a fallback list says
  `silence_max_attempts=1` and moves on. It is not a connect-only rule and the
  measurement is why: on the mini, overpass-api.de refused the connection
  outright but kumi.systems **connected in 0.109 s and then sent nothing for
  30 s**, so a connect-only rule would have walked straight past the instance
  that cost the most. `utils/http._is_silence` therefore reads
  `ConnectionError` and `Timeout` alike.

  **The budgets are `utils/http.lookup_budget`, and only the free transports
  read them.** `OSM_OVERPASS_WALK_BUDGET_S` (210 s) bounds one walk across
  every instance; `ENRICH_LOOKUP_BUDGET_S` (240 s) bounds every free lookup of
  one Enrich press together, because a run makes a dozen of them -- eleven,
  plus the sea-view fan's second elevation request on a row whose shoreline
  ray was blocked. 210 is
  derived rather than chosen -- #144's patient budget on the first instance
  (~76 s with the gate) plus one complete attempt on each fallback (2 x 65 s)
  -- and the guarantee it buys is conditional and says so where the number
  lives: *a prompt refusal on the primary* leaves a complete attempt for each
  fallback, while a primary spending 30 s per `504` leaves the first fallback
  a clamped read. Making it unconditional costs seven and a half minutes for
  one lookup, and the price of the gap is a retry, never a wrong answer.
  Google's paid transports are deliberately outside all of it: abandoning a
  billed Distance Matrix request because a free source spent the clock is the
  same defect with the roles swapped, and the owner pays for a measurement
  nobody receives (#178).

  **A budget refusal is nobody's fault but the clock's**, and three places
  have to know it. It is not in `_OVERPASS_TRY_ELSEWHERE` -- the next instance
  would answer the same one gate wait later. It is not counted against
  #438's `OVERPASS_BREAKERS`, in the amenity client or the coastline one
  (`SeaViewBudgetExceeded` exists for that and for nothing else), or five
  minutes of silence gets armed against a healthy host on the strength of
  somebody else's slow run. And **a silence produced by a clamped attempt is
  the budget's too**: review reproduced three calls with a 0.1 s clamped read
  arming the breaker against a host never given the 60 s it is configured for.
  Conversely a server that already answered outranks the clock -- a `504`
  observed on an earlier attempt is returned rather than replaced by a budget
  error, because it is what the caller classifies the host by.

  **`enrich_property` has two passes and the boundary between them is a
  commit.** The decisive pass -- the coordinate, the sea distance, the travel
  times -- runs first and is committed on its own, so a container recreated
  mid-run (#283) cannot take a paid measurement with it. The advisory pass
  runs after, each step owning its write, and **every one of them takes the
  row under `FOR UPDATE`** (`services/enrichment_write.py`). That sentence
  said "three of its four" for eight hours: `enrich_osm_amenities` was #352's
  last unlocked writer, this file described the gap instead of closing it, and
  the first thing #437's new hazard block met was the one writer that could
  erase it -- reproduced, then closed in #460. Describing a gap is not the
  same as closing one, and a rule file that names an open hole is read as
  permission to leave it open. Order is a budget decision as much as a commit one: the free
  lookup the *paid* call depends on is `services/osm_places.py`, and no
  destinations means no Distance Matrix request, so the decisive steps must
  hold the clock while there is any. The pool step stays off the
  coordinate-less path, because `no_coordinates` is not one of the two
  statuses its "a refusal never overwrites an answer" guard defends against.

  **`dedupe_key` holds only while a job is *active*, and that is a trap that
  bit three times.** `property_enrich:<id>` is keyed on the property alone --
  keying on `(property, refresh_coords)` would let a `refresh=True` press race
  an ordinary one, which is #339. The AI sequel is where the trap lives: the
  enrichment job queues `property_ai_analysis` itself, so the analyses survive
  the tab, and it **returns the ids in its result** so the page attaches to
  them. A page that POSTs instead pays twice whenever the server's own job
  finished first and freed the key -- including on the path where the poller
  gave up and there are no ids to attach to, which is why the page does not
  dispatch there at all. The flag asking for the sequel is read from the JSON
  body only: these blueprints are CSRF-exempt and unauthenticated, and a
  simple cross-origin form POST cannot set `Content-Type: application/json`.

  **The client's poll budget is for silence, not duration.** No constant can
  be a run's worst case -- the executor's queue is unbounded, a `FOR UPDATE`
  can wait on a database with no statement timeout, and `requests` measures
  its read timeout *between* reads -- so `JOB_POLL_TIMEOUTS.enrichment` went
  stale the moment the fallback list was added and nobody re-derived it, which
  is #178. `services/enrich_budget.py` states what the server allows and the
  202 carries it as `poll_timeout_ms`; `pollJob` spends that on the gap
  between two answers, resetting whenever the server confirms the job is
  alive, with a backstop at four budgets because "still running" is not "will
  finish". Read the AI term from the transport the caller actually reaches:
  `classify_text_with_ai` passes no `timeout=`, so it takes
  `subscription_transport.DEFAULT_TIMEOUT_SECONDS` (300) and not
  `AI_ANALYSIS_TIMEOUT_SECONDS`.

  One thing the deadline does **not** promise, measured against a loopback
  server rather than assumed: it bounds when an attempt may *start* and what
  its socket timeouts are, not how long one attempt may run. A server dripping
  a byte often enough held a request open for 0.63 s under a 0.20 s deadline.
  That is stated rather than fixed because the outage this bounds is the
  opposite one -- instances that connect and say nothing -- and a total-time
  bound needs a streamed body with its own clock, which the coastline client
  already has. `tests/test_one_press_is_bounded.py` pins all of the above on a
  virtual clock, so what it asserts is the arithmetic and not how the machine
  felt on the day.
- **Catastro is free and keyless, and the way to lose it is an IP ban.** Its
  terms publish no requests-per-second figure at all and do publish a block of
  "generally ten days" for abuse, so the interactive path is bounded by
  arithmetic rather than by a backoff: `max_attempts=1` in
  `services/cadastre_service.py` (a press is one attempt per endpoint, the
  retry is the owner pressing again), three endpoints per uncached press, and
  `@limiter.limit("5 per minute")` on the route -- fifteen requests a minute at
  the very worst. `CATASTRO_GATE` paces and a `HostBreakers` breaker stops a
  broken loop, but neither is what makes the number true; dropping the retries
  is. Do not add a bulk path over it: this project has already blocked itself
  at one free source with its own backfills (the Overpass rule above), and
  there is no paid fallback behind Catastro at all.
