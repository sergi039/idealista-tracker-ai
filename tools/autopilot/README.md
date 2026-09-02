# Autopilot

Unattended issue → PR → merge → deploy for this repository.

```
open issue ──▶ run_issue.sh ──▶ PR ──▶ CI (GitHub Actions)
                                        │
                                        ▼
                                 independent review (rx / gpt-5.6-sol)
                                        │
                                   merge_bot.sh ──▶ squash into main
                                        │
                              deploy_watcher.sh ──▶ rebuild + healthz + /properties
                                        │
                                  unhealthy? ──▶ rollback
```

Branch protection on `main` requires the status checks named in the protection
itself and refuses any branch behind `main` — GitHub enforces both atomically at
merge time. The bot does not re-implement them. It adds the gate GitHub has no
concept of: **an independent reviewer must return PASS**.

| Enforced by | What |
|---|---|
| GitHub branch protection | required checks green, branch up to date, PR required, no force-push |
| `merge_bot.sh` | independent review, verdict bound to the exact diff, BLOCKER posted on the PR, diff small enough to review at all |

## Scripts

| Script | Does | Does not |
|---|---|---|
| `run_issue.sh <n>` | worktree, agent, full suite, push, open PR | merge anything |
| `merge_bot.sh` | check CI, request review, squash-merge | write code |
| `deploy_watcher.sh` | pull main, rebuild, health-check, roll back | merge anything |
| `autopilot.sh` | one issue pass, then a merge pass | deploy |

Every script takes a lock, so a slow run never overlaps the next tick.

## Running it

```bash
tools/autopilot/autopilot.sh --dry-run      # decide and report, change nothing
tools/autopilot/autopilot.sh                # one issue, then a merge pass
tools/autopilot/autopilot.sh --issues 3     # three issues this pass
tools/autopilot/merge_bot.sh --dry-run      # inspect CI/cache; no review or merge
tools/autopilot/merge_bot.sh --pr 57        # one specific PR
tools/autopilot/run_issue.sh 24             # one specific issue
```

A normal `merge_bot.sh` pass (including one started by `autopilot.sh`) calls
`rx` once for each eligible `base..head` without a cached verdict, and that
call costs a reviewer attempt. `--pr N` has the same cost unless combined with
`--dry-run`. Dry runs never call `rx`: they read CI and the review journal,
then report the cached decision or the review they would request.

Logs live in `data/autopilot*.log`. Review verdicts are journalled in
`data/autopilot-reviews.tsv`, keyed by `base..head` — a re-push or a moved base
gets a fresh review, a re-run does not burn another reviewer attempt on an
unchanged diff.

`data/.deployed_sha` records the commit that is actually serving. The watcher
compares it against `HEAD`, not just local git against remote git: a `git pull`
run by hand leaves the checkout current while the container still runs the old
build, and comparing only the two git refs reports "nothing to deploy" for a
stale app. The marker is written after the health check passes, so it never
names a build that failed.

After a rollback the marker is written only when the rollback rebuilt from
source — that path knows which commit is running. A rollback restored from the
saved image does not: with no prior marker, that image can predate the checkout
entirely, and claiming otherwise would make every later tick skip a redeploy
the app actually needs. The marker is removed instead. One wasted rebuild beats
a permanently wrong belief about what is running.

## Healthy means a page rendered

The health gate is `/api/healthz` **and** one real page. healthz reports
database, scheduler and schema; it renders no template, so it cannot see a
broken one. On 2026-08-14 a `TemplateSyntaxError` turned every
`/properties/<id>` into a redirect for 15 minutes while healthz stayed green —
`routes/main_routes.py` catches the error and redirects. So the watcher also
fetches a page and requires **200**, not a redirect, which is exactly what that
defect produced. Both must pass inside `AUTOPILOT_HEALTH_TIMEOUT`, on the
deploy *and* on the rollback.

Which page, and what counts as rendered, is **one contract** in
[`lib/render_check.sh`](lib/render_check.sh): `DEPLOY_RENDER_PATH` (default
`/properties`), joined to an origin, passing only on 200. `.githooks/post-merge`
reads the same file, because this rule used to be written down twice — as
`AUTOPILOT_PAGE_URL` here and `AUTO_REBUILD_RENDER_PATH` there — and two names
for one idea eventually ship half-changed (#292). Both retired names are no
longer read; a tick that still finds one set in its environment says so and
carries on with the shared rule.

What each deployer keeps is its own **origin**, which is a different question
("which stack is this"): the watcher takes the origin of `AUTOPILOT_HEALTH_URL`,
so a harness pointing healthz at a stub points the page check at the same stub,
while the hook asks `docker compose port` because `APP_HOST_PORT` lives in the
project `.env`.

Set `DEPLOY_RENDER_PATH=""` to skip the check; the log then says the build is
unverified rather than saying nothing.

## Long-running work inside the container

`docker compose up -d --build` recreates the app container and kills whatever
is running in it. Observed twice on 2026-08-14: a pool backfill
(`python -m utils.backfill_pool`, hours long, paced by Overpass) died
mid-flight and **nothing anywhere said so** — healthz was green before and
after, the watcher logged an ordinary successful deploy, and the only way to
learn what had completed was to read the backfill's own per-row ledger.

Before it builds, the watcher now enumerates the container's processes with
`docker top` — authoritative about liveness, and needs nothing installed in
the image, so it also covers a job someone started by hand with `docker exec`.

`AUTOPILOT_INFLIGHT_PATTERN` is deliberately a **generous pre-filter**, not a
precise one: any python whose command mentions `utils.` or `utils/`. Three
spellings of one command have already defeated three attempts to be precise —
`-m utils.x`, `-mutils.x` (python takes the argument joined) and `-um utils.x`
(a cluster; `-u` is what a job writing to a log is usually started with) — and
each time the job the pattern missed was not reported as *unknown*, it was not
reported at all. An extra process named here costs a bounded deferral and a
log line; a missing one costs work nobody knows was lost. The marker join
below is the precise layer, and it reads short options the way python does
rather than matching a literal `-m`.

Each match is logged by name:

```
long-running work is in flight inside idealista-app:
  in flight (resumable): python -m utils.backfill_pool --snapshot data/pool_backfill_20260814b.json
      ledger: data/pool_backfill_20260814b.json.ledger.jsonl
  deploying anyway (AUTOPILOT_DEFER_ON_INFLIGHT is off); the 1 job(s) above will be killed
```

**The default behaviour is unchanged.** The watcher deliberately does not make
judgement calls about someone's working state, and holding a deploy trades a
silent kill for a stalled deploy chain. What changes is that the postmortem
exists without reading a ledger.

Whether killing a job actually loses work is a question `docker top` cannot
answer, so the job answers it: `utils/inflight.py` writes
`data/.inflight/<module>.<pid>.json` on start and removes it on exit
(`data/` is bind-mounted, which is how a file written inside the container is
read on the host). `resumable: true` is a claim that an interrupted run
resumes without losing or re-billing work — per-row commit, an idempotent
scope that finished rows leave, and ideally a ledger. A missing marker means
*unknown*, and unknown is treated exactly like `false`: a deploy cannot tell
them apart, and guessing "resumable" is how work goes missing quietly.

**The marker is joined to a process by command line, not by PID.** The PID in
the filename is the container's (`os.getpid()`); `docker top` reports the
host/VM view — measured on the mini, 41 against 21974 for the same process.
A marker vouches for a process when the module is the program that command
runs — the token after `-m`, joined (`-mutils.backfill_pool`) or separated,
or the first `.py` token — and the recorded `argv` renders to exactly that
command's arguments, in order. Not "appears in": membership was the first
attempt and was wrong three ways at once — `data/a` vouched for a live
`data/aaa.json`, a reordered argv matched, and an *empty* argv was vacuously
true, so a stale `bulk_ai_analysis` marker with no arguments vouched for a
live `--force` run, the one run that is not resumable. Exactness also keeps
two concurrent runs of one module apart by their `--snapshot` paths. Markers
that match but disagree about `resumable` resolve to *unknown*, never to the
deploy's convenience.

The comparison is on the **rendered string**, not on token lists, because
`docker top` returns one whitespace-joined line with the shell's quoting
already gone: a job launched with `--snapshot 'data/My Pool.json'` arrives as
four tokens against the marker's two. Asking "is this the same command line"
is the only question a process list can answer, and it is the question that
finds the live job's own marker. Both sides are whitespace-normalised before
they are compared, since a tab the marker recorded cannot survive that line.

A marker that cannot be read is **rejected, never normalised**. A missing or
malformed `argv` coerced to `[]` would take on the identity of a job that
runs with no arguments, so a damaged file claiming `resumable: true` could
vouch for a live job — a claim invented out of the damage. For the same
reason an argument that is empty or nothing but whitespace disqualifies the
marker: `[""]` and `[]` render identically, and that ambiguity must not be
resolved in the deploy's favour.

**The known limit, stated rather than left to be discovered.** Rendering
cannot recover argument *boundaries*: `["--force", "data/x"]` and
`["--force data/x"]` are the same line in a process table, so a marker
recording the second would vouch for a live job running the first. It is not
fixable from this side — `docker top` lost the quoting before the watcher saw
it — and the fix that would work, reading `/proc/<pid>/cmdline` through
`docker exec`, means going back into the container's PID namespace, which is
the join this machinery exists to remove. Two things bound it: nothing in
`utils/` can write such a marker, because every entry point runs
`parse_args()` before `inflight()` and an argument spelled `--force data/x`
is rejected before any marker exists; and a live job that wrote its own
marker makes the two disagree, which already resolves to *unknown*.

Three states, not two: a `docker top` that cannot be read is **unknown**, and
blocks exactly like an unmarked job rather than reading as "nothing running".
That includes a table it cannot *parse*: the column layout is read off the
header (`docker top` renders whatever ps format it is handed, and only the
default one puts the command at field 8), and one row the header cannot
describe makes the whole table unknown. A mis-split command matches the
pattern no more, so the job it named would not become unknown — it would
disappear, which is the outcome this survey exists to remove.
Only a shell `-c` parent is folded into its child; a genuine `utils` process
that spawned another `utils` process stays two jobs, so a non-resumable parent
cannot vanish from the count.

| Variable | Default | Does |
|---|---|---|
| `AUTOPILOT_DEFER_ON_INFLIGHT` | `0` | `1` = skip a tick when a job would lose work |
| `AUTOPILOT_DEFER_BUDGET` | `6` | ticks (≈30 min) before deploying anyway |
| `AUTOPILOT_INFLIGHT_PATTERN` | `python.*(-m +utils\.` &#124; `utils/)` | what counts as a job |
| `AUTOPILOT_APP_CONTAINER` | `${COMPOSE_CONTAINER_PREFIX:-idealista}-app` | container to inspect |

The deferral budget is bounded on purpose: a deploy that never lands is also
a failure, just a quieter one. Deferrals are counted per target commit in
`data/.deploy_deferrals`, so a new commit is a new decision; when the budget
runs out the watcher deploys and says that it did. If the counter cannot be
written it deploys immediately rather than wait on a bound it cannot enforce.

The other half is the next run: a marker outlives its process precisely when
the process was killed, so the next start of the same job finds it, reports
what was interrupted and where its ledger stands, and clears it.

`tools/autopilot/deploy_inflight_test.sh` (wrapped by
`tests/test_deploy_watcher_inflight.py`) pins all of it — no job, a resumable
job, a job with no marker, the bounded defer, and a green healthz over a
redirecting page.

## Scheduled deploys

```bash
cp tools/autopilot/com.idealista.deploy-watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.idealista.deploy-watcher.plist
```

Polls every 5 minutes. GitHub Actions cannot reach this Mac — there is no
self-hosted runner — so deployment is pull-based by necessity, not preference.

To stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.idealista.deploy-watcher.plist
```

**On a machine with this watcher, nothing else deploys.** `.githooks/post-merge`
rebuilds the running container when main lands in a clone, but it detects this
LaunchAgent (`~/Library/LaunchAgents/com.idealista.deploy-watcher.plist`, or
`launchctl list`) and stands down where it is installed. That is not politeness
about ordering: the watcher takes its rollback checkpoint at *tick* time, so an
image built behind its back between two ticks becomes the build it would roll
back **to**, quietly destroying the last known good one. The hook exists for
machines with no deployer — the shared agent checkout on the laptop, which this
watcher rightly refuses to deploy because it sits on a branch with uncommitted
files. Both take this same deploy lock, so a hand-run of either never overlaps
the other.

## Deploying the watcher itself

The watcher is in the tree it deploys, so a tick that advances main can be
rolling out a change to `deploy_watcher.sh`. Until #293 that tick ran the *old*
script all the way through — measured, not intermittent. `git merge` writes a
file by renaming a new one over it, so the inode changes and the shell's open
descriptor still points at the previous, now-unlinked one. It reads that to the
end of the tick.

That is what happened at 16:33:30 on 2026-08-14: the deploy that shipped #285's
in-flight survey and page check ran with neither, and killed a pool backfill at
32 ledger rows silently. It also means no ordering inside one process fixes it.

So the tick hands over. When `origin/main` changes `deploy_watcher.sh` or
`lib/`, the watcher fast-forwards and `exec`s the new script **before** it
surveys, defers, builds or verifies anything:

```
a1b2c3d changes this watcher itself (tools/autopilot/deploy_watcher.sh tools/autopilot/lib)
  fast-forwarded to a1b2c3d; handing this tick over to its tools/autopilot/deploy_watcher.sh
still holding the deploy lock handed over with this tick
```

Three things ride across the `exec`:

- **the deploy lock.** It is `flock(2)` on fd 9, and `exec` replaces the
  program, not the process, so the descriptor and its lock are still held.
  Re-taking it would also work — `exec 9>file` closes the old description
  before locking the new one — and that is the problem: it drops the lock for
  the length of a fork and an exec, during which another tick can start a
  second concurrent build.
- **the commit that is serving,** as `AUTOPILOT_ROLLBACK_SHA`. After the
  fast-forward, `HEAD` is the commit under test, so the new process cannot work
  out on its own where a rollback goes. That covers one tick, which is not
  always enough: a tick that hands over and then *defers* to an in-flight job
  ends without deploying, and the next tick is a fresh process with nothing
  handed to it. There the rollback target comes from `data/.deployed_sha`
  whenever the checkout is ahead of it — the marker is written only after a
  build passed health, so it names what is serving, and unlike an environment
  variable it outlives the process. Without that, a failed build of the
  deferred commit rolls back to itself.
- **a handover count,** so this terminates. One per tick; if main moves again
  the log says so and the tick **stops without deploying**. Deploying at that
  point is the defect this whole mechanism removes — the deploy would be run by
  this watcher while the newer one goes on disk — and stopping costs only a
  tick: the checkout already holds the watcher this process is running, so the
  next tick starts from it and hands over normally, and that handover deploys
  itself. Nothing is merged for the newer commit, and the previous build keeps
  serving meanwhile.

Before merging anything it syntax-checks the incoming `deploy_watcher.sh` and
`lib/*.sh`. A watcher that does not parse cannot be handed over to, and
deploying it would break the deploy chain at the *next* tick with the checkout
already advanced — so it refuses while the checkout, the container and the
marker are all still untouched, and the previous build keeps serving.

Two details in that gate are load-bearing:

- **It asks the bash that will run it,** `${BASH:-/bin/bash}`, never a bare
  `bash`. The LaunchAgent execs `/bin/bash` (3.2.57) while handing the job a
  PATH starting with `/opt/homebrew/bin` (5.x). Measured, `cmd &>> file` and
  `;;&` are exit 0 under `bash -n` 5.3.15 and syntax errors under 3.2 — and
  `>>"$LOG_FILE" 2>&1` appears a dozen times in this script, so `&>>` is one
  keystroke away. It is still only a floor: `declare -A` parses under 3.2 and
  fails at runtime.
- **It merges the commit it vetted, not the ref that named it.**
  `git merge --ff-only "$remote_sha"`. Several agent sessions and a human
  `fetch` into this same clone, and the window between resolving `origin/main`
  and merging holds a `docker image inspect`, a `docker tag` and the gate
  itself — seconds. Merging the ref would fast-forward to, and then `exec`, a
  watcher nothing had read. The newer commit is deployed by the next tick.

| Variable | Default | Does |
|---|---|---|
| `AUTOPILOT_SELF_UPDATE` | `1` | `0` = the pre-#293 behaviour, which now says loudly that it deployed a watcher it did not run |
| `AUTOPILOT_REEXEC_MAX` | `1` | handovers allowed in one tick |

One property does change: after a handover the fast-forward has already
happened, so an in-flight deferral holds with the checkout one commit ahead of
the container. `data/.deployed_sha` still names what is serving, which is what
everything downstream reads.

`tools/autopilot/deploy_self_update_test.sh` (wrapped by
`tests/test_deploy_watcher_self_update.py`) drives the real watcher from inside
a throwaway repository that contains a copy of it — the arrangement the other
two watcher tests deliberately do not have, which is why neither could reach
this path.

**That suite must not borrow a fact from the machine it runs on.** The
scenario covering the gate above was first written with `&>>`, which is a
syntax error under `/bin/bash` on this Mac and valid under `/bin/bash` on the
Linux CI runner — so it proved the gate here and reported the gate broken
there (CI run 31868366707). The divergence is now modelled instead: a `bash`
stub leads `PATH` and pronounces everything valid, while the interpreter
running the watcher rejects an ordinary syntax error, and the scenario also
asserts the stub was never consulted. `WATCHER_BASH` picks the interpreter —
`/bin/bash` by default, which is what the LaunchAgent execs; run the suite
under a bash 5 as well before believing it, because that is the only bash CI
has.

## A stall is an alarm, not a deploy

On 2026-09-01 the mini's checkout sat on branch `codex/issue-473` with five
uncommitted files from 07:43 to 16:03, and every tick refused — correctly,
100+ times — while two merged commits never reached production (#532).
`/api/healthz` was green because the *old* image was healthy; the page check
passed because the *old* page rendered. Every liveness signal here answers
"is the app serving" and none answers "is the app current", so a production
that stops taking merges looks identical to one with nothing to take. It was
found by accident, in the log, eight hours later.

The refusal stays. What the watcher does now is count the ticks that **end
without deploying while `origin/main` is ahead of `data/.deployed_sha`** —
the branch refusal, the dirty-tree refusal, an in-flight deferral, the
handover-budget stop, and a `FATAL` after the fetch (the parse gate's
"refusing to deploy a watcher that cannot run" repeats every five minutes
exactly as the branch refusal did). From `AUTOPILOT_STALL_THRESHOLD` such
ticks in a row (3, fifteen minutes) every refused tick logs one line

```
STALLED: production is 2 commit(s) behind main after 3 refused tick(s) since 2026-09-01T05:43:14Z - deployed b286668, main 4a69583; last reason: on branch 'codex/issue-473', not 'main'
```

and writes `data/.deploy_stalled` — that line first, then `deployed=`, `main=`,
`branch=`, `gap=`, `ticks=`, `since=`, `reason=` one per line — so a session
that reads files rather than logs can see it; `tools/backfill_status.sh`
prints it too, without changing its verdict. The line repeats on every refused
tick past the threshold on purpose: a `tail` of the log at hour eight has to
show it, not only the refusal it showed 97 times before.

**The alarm leads to a person.** Nothing on this path deploys, `git stash`es,
switches branches or resets a tree. The primitive makes one git write — a
`git fetch --no-write-fetch-head` of the remote-tracking ref, because the
branch and dirty refusals come before the tick's own fetch and a stale
`origin/main` is how a gap hides; the option matters because a plain fetch
also rewrites `.git/FETCH_HEAD`, and a developer paused between `git fetch
origin feature` and `git merge FETCH_HEAD` in this shared clone would merge
main instead (found by the plan review, reproduced, and pinned) — and touches
two files under `data/`. The incident's own resolution was the orchestrator
verifying the tree byte-identical to merged content before stashing it, and
that is what the alarm asks for. Deleting `data/.deploy_stalled` is not a
cure; the next refused tick rewrites it.

It clears itself when production is current again: on `DEPLOYED`, and on any
tick that measures a gap of zero — the "nothing new" path, or a refused tick
after somebody deployed by hand and wrote the marker — because a count carried
into the next stall would alarm after one refused tick of it. The count is
**not** keyed on the target commit the way the deferral budget is: main moving
further during a stall is more reason to shout, not a reason to start over.

What it cannot see, stated rather than discovered. A gap it cannot measure —
no `data/.deployed_sha`, or a marker naming a commit this clone does not hold;
the deploy path already shouts about both — leaves the count alone. A `FATAL`
before the fetch (a missing contract, a failed fetch) is not counted for the
same reason. The lock skip is not counted because another process owns that
tick's outcome. A failed build is not a refusal: it is a failure with its own
`ROLLBACK` line, and the watcher tried. And a fetch that fails inside the
refusal path measures against `origin/main` as last fetched and says so in the
line — main is protected, so that is a lower bound, never a fabrication.

| Variable | Default | Does |
|---|---|---|
| `AUTOPILOT_STALL_THRESHOLD` | `3` | refused ticks in a row before `STALLED:`; `0` alarms on the first |
| `AUTOPILOT_STALL_STATE` | `data/.deploy_stall_ticks` | the counter, `<count> <since>` |
| `AUTOPILOT_STALLED_MARKER` | `data/.deploy_stalled` | the alarm, for anyone who reads files |

`tools/autopilot/deploy_stall_test.sh` (wrapped by
`tests/test_deploy_watcher_stall.py`, under `/bin/bash` and the PATH bash)
pins it: the incident's shape with `origin/main` stale in the clone, the
not-ahead case, the clear, the deferral, the hand-written marker, a bad
threshold, an unmeasurable gap, and the zero-gap reset. The parse-gate
refusal, the handover's continuity across the `exec` and the handover-budget
stop live in `deploy_self_update_test.sh`, the one harness where the watcher
is inside the repository it deploys.

## Why it is shaped like this

**One agent per issue.** PRs #57 and #58 both fixed issue #17, in different
files, because two agents were pointed at the same issue. `run_issue.sh` refuses
an issue that already has a branch or an open PR, and `autopilot.sh` filters
claimed issues before spending an agent's hour.

**A reviewer, not just tests.** The pre-existing suite mocked the IMAP services
wholesale, so the `db.session` call that actually crashed never ran under test —
issue #14 passed CI for six months while ingestion was dead. Green tests prove
less than they look like they prove; the reviewer is asked specifically whether
the tests mock past the failure.

**Health-gated deploys.** `/api/healthz` reports the database and the scheduler
separately. A deploy that leaves the scheduler `not_initialized` gets a warning
in the log even when the page loads — that combination is precisely what
"working app, no new data" looked like.

**A verdict covers a diff, not a branch.** The journal key is `base..head`, so a
PASS stops applying the moment either end moves, and `--match-head-commit`
guarantees the merged commit is the reviewed one.

**UNAVAILABLE is not PASS.** A reviewer that cannot run leaves the PR open.

**Every prompt ends by saying what a verdict looks like**, because `rx` reads
the first line and only the first line: the bare keyword, optionally wrapped in
Markdown emphasis, and anything else is `UNAVAILABLE`. On 2026-08-15 PR #312
came back with two well-argued BLOCKER findings under an opening line of prose,
and `rx` reported `verdict not recognised` — a real review discarded on
presentation, with the attempt spent and the PR unmergeable. `merge_bot.sh` now
appends `verdict_format_rule` to the strict prompt and, after the excerpts, to
the documentation-only one; the paragraph lives in one function, and
`tests/test_merge_bot_verdict_format.py` fails if either prompt loses it or
grows its own copy. What no test can prove is compliance — that half was
measured against codex, which obeyed first time, twice.

**A diff over 60 000 bytes is refused before the attempt is spent, and no
timeout will change that** (issue #182). `rx` does not degrade on a large diff,
it dies: `bin/cx` pipes the whole codex transcript to stderr and the coordinator
kills the process group at its 256 KB cap, reporting `UNAVAILABLE`. Since
`UNAVAILABLE` is correctly not a pass, the bot would re-request the same
impossible review on every tick, for ever, with nothing in the log saying why.

The tell that it is a kill and not a timeout: PR #177 measured 94 621 bytes and
failed at a 240 s limit after 210 s, then at an 800 s limit after **157 s** —
sooner, with more time. The seven merges before it ran 3 589 to 35 117 bytes, so
`AUTOPILOT_REVIEW_DIFF_MAX_BYTES` defaults to 60 000, between the largest diff
known to work and the one known to fail. That is one failure and seven
successes, not a curve — re-measure before moving it, and do not raise it
because a particular PR happens to be over.

The refusal is journalled as `OVERSIZED` and posted once on the PR, so the next
tick is silent. Split the PR, or review it by hand: running the same model
directly works, because it is the wrapper that fails and not the provider.

```bash
codex exec --ephemeral --ignore-user-config --ignore-rules -m gpt-5.6-sol \
  -s read-only -c model_reasoning_effort=xhigh --output-last-message /tmp/verdict.txt \
  -- "<prompt asking for PASS or BLOCKER on the first line>"
```

Two sibling failures print the same `UNAVAILABLE` and are also not timeouts: the
coordinator's secret preflight rejects the **diff** when an added line matches a
credential pattern (`review evidence failed…`), and the **prompt** when the
embedded text does (`review request failed…`). Neither names the offending line.
A test fixture imitating a real token will do it — so will a comment quoting one
to explain the first failure.

**A documentation-only PR is reviewed against the base, not against nothing.**
The reviewer's contract is to audit the embedded diff and nothing else, which is
right for code and impossible for a PR that writes down behaviour already
shipped: the proof is in the base commit, so "unproven" was the only verdict
available. #151 hit it twice — both reviews correct about the diff and wrong
about the repository — and was merged by hand. Adding `file:line` citations did
not help, because a citation still points outside the diff.

So the bot supplies the other half. When every changed path is documentation,
`docs_review_evidence.py` reads the *added* documentation, resolves what it
cites against the base commit, and embeds those excerpts in the request. The
reviewer still audits only what it was handed; it is handed the thing the claims
are checkable against.

"Documentation" is decided by name **and** by git's own file mode: `*.md`
anywhere or a non-executable format under `docs/` (no `.svg` — it can carry a
script element), and mode `100644` on both sides of the diff. A suffix alone is
not enough — an executable `tools/deploy.MD` is a script, a `docs/notes.md`
symlink hides its target, and a `160000` gitlink named `docs/vendor.md` moves a
whole tree of code the diff never shows. `merge_bot.sh` then asks git the same
question a second time, coarsely and on its own, so the relaxed prompt needs two
agreeing answers rather than the helper's word.

`CLAUDE.md`, `AGENTS.md`, `SKILL.md`, `.claude/**` and anything under a
`skills/`, `agents/` or `prompts/` directory stay eligible — every behavioural
claim in them is checkable against the base like any other document — but the
evidence flags them under `AGENT INSTRUCTIONS` and the prompt blocks on an added
line that grants an agent new authority, weakens a guardrail, or tells it to run
a command. That text is executed by the next agent run, and unlike a claim about
behaviour it is fully visible in the diff.

It reads both citation shapes, because this repository uses one of them almost
exclusively:

| In the documentation | Resolved to |
|---|---|
| `services/enrichment_service.py:1056` | ±10 lines around line 1056 |
| `` `utils/http.py` `` … `` `HTTP_USER_AGENT` `` | ±10 lines around where that identifier is defined |
| `` `utils/http.py` `` alone, no identifier | a line count, confirming the file exists |

Source paths only — a `.md` cited from a `.md` resolves to nothing, since one
document is not evidence for another.

The second row is the one that matters. #151 cites code as a backticked path
plus a backticked symbol and never once as `path:line`, so a resolver that
handled only explicit line numbers would have found nothing on the very PR this
exists to unblock. Run against `b01c3ac` it now resolves twelve windows, among
them `services/enrichment_service.py:1056` — the `remark` refusal the issue
names as the missing proof.

Consequences worth knowing before writing a docs PR:

- **"Unproven" still blocks.** What moved is where the proof is expected — the
  excerpts, not the diff. A claim the excerpts merely fail to contradict is not
  proven: `app.py` importing `CSRFProtect` does not establish "every
  state-changing endpoint is CSRF-protected".
- **Name the file you are describing.** A claim about behaviour that cites no
  source file at all is a BLOCKER, because nothing in the request can falsify
  it. A backticked path is enough; a symbol alongside it is better, and the
  citation should point at the line that settles the claim.
- **A `path:line` that has drifted is a BLOCKER**, printed as `UNRESOLVED`.
  Documentation naming a line the base does not have is already wrong.
- **A bare path the base lacks is reported, not blocked.** It prints as
  `NOT IN BASE` and the reviewer judges it from the text, because documentation
  legitimately names files that are generated or ignored
  (`docker-compose.override.yml`, a worktree `.env`).
- **Mixed PRs get the strict prompt.** One non-documentation path in the diff —
  including the destination of a rename out of `docs/` — and the normal review
  applies to the whole thing.
- **Credential-shaped paths are never read**, cited or not, and an excerpt whose
  content matches a credential pattern is dropped. Both print as `REFUSED`,
  which is deliberately *not* `UNRESOLVED`: the refusal is the bot's, not a
  defect in the documentation. The path rule fails *closed* — a name ending in
  `key` is a credential unless it is one of the handful of English words that
  end that way, because an allow-list of qualifiers let `apikey`,
  `clientsecret` and `clientkey` through one review round apiece.
- **An image is declared unreadable, not waved through.** Git shows a binary
  marker and the evidence shows nothing, so `docs/*.png` prints under
  `UNREADABLE CONTENT` and the prompt asks for a BLOCKER saying a person must
  look. A gate that certifies pixels it cannot see is worth nothing.
- **Every failure falls back to the strict prompt**, so a broken or missing
  helper can only make a review harder to pass, never easier. The helper has to
  prove it ran: the block's first line must be `DOCS-ONLY-EVIDENCE <base sha>`
  for the base the bot itself resolved. An exit status is too weak a contract —
  a helper truncated to `raise SystemExit(0)` exits clean with nothing to show.
  It also runs under `timeout`, because a hang would hold the merge lock until
  someone noticed.

**The bot still checks CI and up-to-dateness before reviewing** — not as a gate,
but to avoid spending a minutes-long review on a PR that protection will refuse,
and to keep the reviewer looking at the diff that will actually land. The list
of required checks is read from branch protection, so there is no second copy to
drift.

## The lock

`lib/lock.sh` uses real `flock(2)` (through `python3`, since macOS has no
`flock(1)`). The kernel releases it when the process dies, so there is no PID
bookkeeping and nothing to reclaim after a killed build.

The obvious `mkdir` + stale-PID substitute was tried first and is genuinely
racy: `lib/lock_race_test.sh` reproduces two simultaneous winners against it.
That test runs in CI via `tests/test_autopilot_lock.py`.

```bash
bash tools/autopilot/lib/lock_race_test.sh
```

Children inherit the lock along with fd 9, and that has a consequence worth
stating plainly: **killing the script does not release the lock**. The kernel
drops it when the *last* descriptor closes, so an orphaned `rx` or `codex exec`
keeps the lock held long after its parent is gone, and the next tick reports
"another run is in progress" with no such run in `ps`.

Observed in practice on 2026-08-08, when another session killed a bot run to
unblock a merge and had to hunt the orphans separately.

Ask the lock file who is holding it, rather than pattern-matching command lines.
`pkill -f "codex exec"` is the wrong instrument on a machine that runs more than
one project: it would reach into an unrelated repository's review and kill it
mid-write. `lsof` names the actual holders and nothing else.

```bash
lsof -t /tmp/idealista-autopilot-merge.lock.d
```

(The `.d` suffix is a leftover from the `mkdir` design; the path is a plain
file that `flock(2)` is taken on, not a directory.)

Inspect them before signalling — confirm they are this bot's family — then end
them, escalating only if they ignore the first request:

```bash
lsof -t /tmp/idealista-autopilot-merge.lock.d | xargs -r ps -o pid,ppid,command -p
```

```bash
lsof -t /tmp/idealista-autopilot-merge.lock.d | xargs -r kill
```

The deploy watcher's lock is `/tmp/idealista-autopilot-deploy.lock.d`; the
same three commands apply. The lock is released when the last of those
descriptors closes, so re-run `lsof` afterwards to confirm nothing is left
holding it.

## Requirements

`gh` (authenticated), `jq`, `docker`, `uv`, `claude`, `rx`, `python3`, and GNU
`timeout` (`brew install coreutils`).
