# Long jobs, enrichment writers and the deploy watcher

Moved verbatim from `CLAUDE.md` (lines 2557–2818 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- **A long job announces itself, and the deploy that kills it says so** (#283).
  `docker compose up -d --build` recreates the app container and kills whatever
  runs inside it; observed twice on 2026-08-14, when a pool backfill died
  mid-flight and nothing recorded it — healthz was green either side and the
  watcher logged an ordinary successful deploy. So every long `utils/*` entry
  point wraps its loop in `utils/inflight.inflight(...)`, which writes
  `data/.inflight/<module>.<run_id>.json` (a per-run id, not the PID — a
  containerised run is always PID 1, so the PID cannot tell two runs apart,
  #359; the body also records `host`, the container id, because a PID is only
  a name inside one namespace and a marker from another container reads as
  "cannot tell", never as dead or alive) while it runs and reports — on the
  next start — any marker a killed predecessor left behind. `resumable=True` is a
  *claim*: set it only where a restart really does resume, meaning a per-row
  commit and a scope that finished rows leave (`needs_pool`, `needs_beaches`,
  `--only-missing`). Where that depends on a flag, pass the flag
  (`resumable=bool(args.only_missing)`), never a hopeful constant — a missing
  or false marker makes `tools/autopilot/deploy_watcher.sh` treat the job as
  losing work, and a wrong `True` is #98's defect wearing an ops costume.
  The watcher's own liveness check is `docker top`, so a job that adopts
  nothing is still *seen*; what it cannot supply is whether killing it costs
  anything. **A marker is matched to a process by its rendered command line —
  the module it actually runs, plus an `argv` that renders to exactly that
  command's arguments — and never by PID** (#290 follow-up). The
  two sides do not share a PID namespace: `os.getpid()` inside the container
  returned 41 while `docker top` reported 21974 for that same process, so the
  original PID-keyed lookup matched nothing and every job read as `unknown`.
  The `resumable` half shipped dead and stayed dead for eleven deploy-log
  lines before anyone looked. Do not "simplify" the join back to a PID, and
  keep the fixture's marker PIDs absent from its `docker top` rows — a fixture
  that numbers both sides the same cannot fail on this. *Rendered* is load
  bearing on both halves: `docker top` returns one whitespace-joined line with
  the shell's quoting gone, so `--snapshot 'data/My Pool.json'` arrives as
  four tokens against the marker's two and a token-list comparison misses the
  job's own marker; and the module is read the way **python** reads short
  options, walking the cluster, never by matching a literal `-m`. Three
  spellings of one command defeated three attempts to anchor on a form —
  `-m utils.x`, `-mutils.x`, `-um utils.x` — and each time the job the
  anchor missed was not reported as unknown, it was **not reported at all**.
  That asymmetry is why `AUTOPILOT_INFLIGHT_PATTERN` is a deliberately
  generous pre-filter (any python mentioning `utils.` or `utils/`) with the
  marker join as the precise layer: an extra process named costs a bounded
  deferral, a missing one costs work nobody knows was lost. The same class
  bit `tools/backfill_supervisor.sh` twice on the same day (#311, #319) —
  when one of these turns up, close the class, not the example.
  Likewise **`docker top` failing is not `docker top` answering "nothing"**:
  an unreadable process list is a third state that blocks like an unmarked
  job, and only a *shell* `-c` parent is collapsed into its child — a real
  `utils` process that spawned another is two jobs, not one. That `-c` is
  looked for across every token, not only up to the first non-option one:
  `bash -o pipefail -c` puts a bare word in the middle of the option run, and
  knowing where the options end means knowing an optstring per shell, so a
  scan that covers `-o` but not `--rcfile` would read as complete and still be
  wrong.
  **A marker is not a liveness check and must never be read as
  one** — it outlives its process by design, because surviving the kill is
  what lets the next run report the interruption. A file in `data/.inflight/`
  therefore means "a run started and did not clean up", which is true of a
  live job and of a corpse alike. `tools/backfill_status.sh` is the question
  "is anything running", there and before a hand build ("Building by hand in
  the shared checkout" above); the marker only answers "and would killing it
  cost anything". It is that script and **not** a bare
  `docker top idealista-app`, which is correct only about one container at one
  instant: it cannot see the respawn a supervisor is a tick away from making,
  and it cannot see a job moved into a `docker compose run` sibling — which is
  where long work goes precisely *because* deploys kill it in the app
  container, so the operator who reacted correctly is the one the bare command
  reports as idle (#338). Deferring is opt-in and bounded
  (`AUTOPILOT_DEFER_ON_INFLIGHT`, `AUTOPILOT_DEFER_BUDGET`) — a deploy that
  never lands is a failure too. See `tools/autopilot/README.md`.
- **Two processes writing `enrichment` lose a measurement, and the #98 guard
  cannot see it happen** (#339, incident 2026-08-16). Two runs of
  `utils.backfill_pool` overlapped on the mini; properties 399 and 400 ended
  the afternoon holding `unavailable` with zero candidates, over measurements
  another run had committed seconds earlier, and three rows were billed to
  Google twice. `enrichment` is one JSON column, so every write is a
  read-modify-write over all of it, and `PoolService.enrich` consults the
  previous status from **the copy its own session loaded** — after `_compute`
  has spent seconds on external calls, which under Overpass 504s and four
  retries ran to about 90 s per row. The rule "a refusal never overwrites
  measured candidates" is therefore a guarantee about one transaction's view,
  not about the row. Do not fix it with ordering or a timestamp: on 399 the
  measurement was written 63 s *after* the refusal and still lost, so any
  comparison of write times would have left that row broken. The primitive
  is already here — `apply_to_property` in `services/sea_view_service.py`
  refreshes with `with_for_update=True` (#196) against this exact hazard, and
  `services/pool_service.py` contains no `with_for_update` at all. It takes
  that lock only when `commit=True`, because with `commit=False` the caller
  owns a transaction whose end this function cannot see, and taking one on
  their behalf for an interval it cannot close is worse than the race — that
  mode makes no concurrency promise at all. **The lock lives inside the one
  writer, on its `commit=True` path, and never at a call site**
  (`services/sea_view_service.py:1294` says so: a lock at a call site protects
  that call site, and `utils/backfill_sea_view.py` "and every future caller
  would otherwise reopen the same hole" — the Enrich button, an endpoint, next
  month's script). So what the tools change is the opposite of a lock of their
  own: `utils/backfill_pool.py` and `utils/recalc_property_travel.py` call
  their services with `commit=False` and commit themselves, and they have to
  give that ownership up and pass `commit=True`. And the boundary is **any** writer of
  `enrichment`, not the paid ones: a one-row free script run by hand through
  `docker exec` clobbers a backfill exactly as thoroughly, cost only setting
  the size of the loss.
- **A liveness check is not a claim about the next minute** (#338). A marker is
  not a lock, and `docker top` is not a reservation. The second run above was
  started after its session ran `docker top idealista-app` and correctly saw
  no `utils` process — because the deploy had killed the first run 57 seconds
  earlier (09:01:02 in the deploy log) and `tools/backfill_supervisor.sh`
  refilled the container at 09:01:59, on its next tick. A kill makes the process list read empty precisely
  when a respawn is imminent, and every deploy manufactures one such window.
  What was missing was anything that could express "nothing is running here,
  **and that is temporary**". `tools/backfill_status.sh` expresses exactly
  that, and it is what to run before starting one: it reads what is running
  now (`docker top`), what is *expected* — the supervisor's lock, taken under
  `noclobber` at startup and released only by its `EXIT` trap, so it spans the
  whole kill→respawn gap and is the one thing in the system that knows the
  future — and what started and never cleaned up (`data/.inflight/`, a report,
  never a lock). It answers in three states, and `unknown` blocks exactly like
  `busy`, because every defect in this family began with a failed probe
  reading as a negative answer. Its judgement about a stale lock is *copied*
  from `acquire_lock()` rather than re-derived — that refuses on any existing
  lock file, live pid or dead (#319) — because a tool calling a state "safe"
  that the supervisor calls "stop" is how two of them come to disagree about
  one file.
  It answers; it does not enforce — the daemon cannot stop you, and that is
  exactly why **`busy` and `unknown` are a stop, not an input to a judgement**
  (owner decision 2026-08-17). Wait, and say you are waiting. Do not weigh
  whether the two jobs touch different `enrichment` keys, whether your own run
  is cheap, or whether the other one looks stuck on a timeout and "is not doing
  anything anyway" — you cannot know when it resumes, and the session running
  it is very likely not in your conversation.

  That sentence exists because the door was walked through the day it was
  written. A session shipped this feature's two backfills over a running
  `backfill_quality_of_life`, twice, each time having first checked the thing
  that made it safe: both writers reach `enrichment` through
  `services/enrichment_write.locked_write` under `FOR UPDATE`, they write
  different keys, the lock spans milliseconds rather than the network calls,
  and the second run made one request and one write. All of that was true, no
  measurement was lost, and none of it is the point. The rule protects the case
  nobody thought to check, and "I read the other job's code and it is fine" is
  the shape of reasoning #339 and #338 were written about. Only an explicit
  owner command to start anyway overrides it.

  Sessions sharing this machine still announce a `utils.backfill_*` /
  `utils.recalc_*` — or any hand-run `docker exec` that writes `enrichment` —
  before starting it, naming the module, the rows and the cost. That is a
  protocol, not a guarantee: it holds while every writer is listening, and the
  next one may not be in the conversation at all.
- **A deploy is healthy when a page renders, not when healthz answers** (#283).
  `/api/healthz` reports database, scheduler and schema and renders no
  template, so it stayed green through the 15 minutes of 2026-08-14 in which a
  `TemplateSyntaxError` turned every `/properties/<id>` into a redirect. So a
  build is not accepted until a page that renders a template answers **200** —
  a redirect is the failure being looked for, so do not add `-L` or accept 3xx.
  Do not "simplify" this back to healthz alone, and do not solve it by making
  healthz render something: it answers "can the app serve", and job liveness
  and template health are different questions that must not be smuggled into
  it.
  **That rule has exactly one home: `tools/autopilot/lib/render_check.sh`**
  (#292). Both deployers — `tools/autopilot/deploy_watcher.sh` and
  `.githooks/post-merge`, which reached the rule the same day — source it and
  read `DEPLOY_RENDER_PATH` (default `/properties`), the join, and the
  200-only verdict from there. It used to be written down twice, as
  `AUTOPILOT_PAGE_URL` and `AUTO_REBUILD_RENDER_PATH`; both names are retired
  and, if still set in an environment, are named in the log rather than
  silently obeyed. Do not reintroduce a per-consumer copy: a rule in two places
  is one that eventually ships half-changed, which is why "change both or
  neither" stood here until this ticket. What is *not* shared, deliberately, is
  where each finds its origin — the hook from `AUTO_REBUILD_BASE_URL` or the
  published port, the watcher from its health URL. That answers "which stack is
  this", not "what proves it renders". Both refuse to run when the contract is
  missing **or merely half-loaded** — a truncated file in this shared checkout
  parses fine and defines nothing, which would turn the page check off and
  report it as an opt-out nobody chose — and both say plainly when
  `DEPLOY_RENDER_PATH` is empty and no page was rendered, on the rollback path
  as well as the forward one. `tests/test_deploy_page_check_shared.py` fails if
  either consumer grows its own copy.
- **A production that stops taking merges looks identical to one with nothing
  to take, so the watcher counts the ticks it refuses** (#532). On 2026-09-01
  the mini's checkout sat on another session's branch with uncommitted files
  for eight hours; `deploy_watcher.sh` refused every tick — correctly, and it
  must keep refusing — while two merged commits never reached production,
  healthz stayed green (the OLD image was healthy) and the page check passed
  (the OLD page rendered). Nobody was told. So a tick that ends without
  deploying while `origin/main` is ahead of `data/.deployed_sha` — branch,
  dirty tree, deferral, handover stop, a FATAL after the fetch — is counted in
  `data/.deploy_stall_ticks`, and from `AUTOPILOT_STALL_THRESHOLD` (3) such
  ticks in a row every refused tick logs one grep-able `STALLED:` line naming
  the reason, the deployed sha, main's sha, the gap and since when, and writes
  `data/.deploy_stalled` with the same; `tools/backfill_status.sh` prints it
  too. **The alarm leads to a person.** Nothing on that path deploys, stashes,
  switches branches or resets a tree — the incident's resolution was a human
  verifying the tree byte-identical to merged content before stashing it —
  and deleting the marker is not a cure, the next refused tick rewrites it.
  It clears on `DEPLOYED` and on any tick that measures no gap. A gap it
  cannot measure (no marker, or one naming no commit here) is left uncounted
  and said so in `tools/autopilot/README.md`, never rounded to zero.
- **A deployer sweeps only what it can prove is dead, and only in its own
  lane.** `tools/autopilot/lib/docker_cleanup.sh` is the second shared contract,
  read by both deployers after a build is *serving* — never on the rollback
  path, where the old image is the thing being restored. Measured 2026-08-17,
  the two machines litter differently and the obvious one-liner fixes neither
  well: the mini held 27 images at 1% reclaimable and no build cache at all
  (with the containerd snapshotter an untagged image is collected as soon as
  nothing holds it, which is why ~21 deploys a day leave no pile), while its
  real leak was three exited `docker compose run` corpses pinning the images
  behind them; the laptop held 20.24 GB of build cache, which
  `docker image prune` does not touch at all. Three things the sweep must never
  become, each a real deletion: **`-a`** removes every image no *container*
  uses, and `${IMAGE}:autopilot-rollback` is exactly that — it is the rollback;
  **`docker system prune`** collects vsdb, virto-property and inbox-zero off the
  shared daemon, which is why the scope is the compose project label, read off
  the running container rather than guessed from a directory name; and
  **`--remove-orphans`** kills a *running* one-off, which is where long
  backfills deliberately live (#338). Only `exited` containers carrying
  `com.docker.compose.oneoff` go, and not for a day — a job the deploy just
  killed leaves its container log as the only record of how far it got, and a
  finish time that will not parse keeps the container rather than deleting it.
  Nothing here may fail a deploy that is already serving.
  `tests/test_docker_cleanup_shared.py` pins the refusals.
- **A tick that deploys the watcher hands over to it first** (#293).
  `deploy_watcher.sh` is in the tree it deploys, and a running tick cannot
  pick up its own update: `git merge` renames a new file over the old one, so
  the inode changes and the shell's open descriptor keeps reading the
  *previous* script to the end of the tick. Measured, and reliable rather than
  intermittent — which is why the 16:33:30 deploy on 2026-08-14 rolled out
  #285's in-flight survey and page check while running neither, and killed a
  pool backfill at 32 ledger rows silently. (An in-place rewrite is worse
  still: the same script overwritten with `cat >` keeps its inode and bash
  resumes mid-statement in the new bytes. Nothing here does that, and nothing
  should start.) So when `origin/main` changes the script or `lib/`, the tick
  fast-forwards and `exec`s the new one **before** it surveys, defers, builds
  or verifies. The `flock` on fd 9 rides across the `exec` and is deliberately
  not re-taken — re-taking works, and that is the defect: it drops the lock for
  a fork and an exec, which is room for a second concurrent build. The commit
  that is *serving* rides across too (`AUTOPILOT_ROLLBACK_SHA`), because after
  the merge `HEAD` is the commit under test and a rollback would otherwise stay
  on it. That environment variable lasts one tick, and a tick that hands over
  and then **defers** to an in-flight job ends without deploying — so from the
  next tick the rollback target is read from `data/.deployed_sha` whenever the
  checkout is ahead of it. The marker is the only record of what is serving
  that outlives a process, and it is trustworthy because it is written only
  after a build passed health. An incoming watcher is syntax-checked **before** the merge, where
  refusing costs nothing — with `${BASH:-/bin/bash}` and never a bare `bash`,
  because launchd puts Homebrew bash 5 on PATH while the plist execs
  `/bin/bash` 3.2.57, and `&>>` and `;;&` pass `-n` under one and are syntax
  errors under the other (measured). Both merges take `"$remote_sha"`, not
  `origin/main`: sessions and humans fetch into this same clone, so the ref can
  advance past the commit that was vetted, surveyed and counted against the
  deferral budget. When the per-tick handover budget (`AUTOPILOT_REEXEC_MAX`)
  is spent and `main` has moved again, the tick **stops without deploying**
  rather than falling back to deploying the newer watcher under the running
  one — that fallback is this ticket's defect, and stopping costs a single
  tick because the checkout already holds the watcher in use, so the next tick
  hands over normally. `AUTOPILOT_SELF_UPDATE=0` restores the old behaviour and
  says so loudly; do not make that the default. The suite that pins all of
  this (`tools/autopilot/deploy_self_update_test.sh`) must not build a
  scenario out of a fact that is only true on one machine: the version gap
  above exists on this Mac and not on the Linux CI runner, where `/bin/bash`
  is bash 5 too, so the first version of that scenario proved the gate here
  and reported it broken there. It now models the disagreement with a `bash`
  stub on `PATH` that approves everything, and `WATCHER_BASH` runs every
  scenario under a bash 5 as well.
