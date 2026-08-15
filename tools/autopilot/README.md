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
