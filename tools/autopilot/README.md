# Autopilot

Unattended issue → PR → merge → deploy for this repository.

```
open issue ──▶ run_issue.sh ──▶ PR ──▶ local CI gate (run_gate_on_sha.sh)
                                        │
                                        ▼
                                 independent review (rx / gpt-5.6-sol)
                                        │
                                   merge_bot.sh ──▶ squash into main
                                        │
                              deploy_watcher.sh ──▶ rebuild + /api/healthz
                                        │
                                  unhealthy? ──▶ rollback
```

Nothing here bypasses a gate. A PR merges only with **a green local gate and a
PASS from an independent reviewer**; a deploy survives only if `/api/healthz`
answers `"ok":true` afterwards.

What counts as green (issue #83): the PR head must already contain the current
base, and `tools/ci/run_gate_on_sha.sh` — the same snapshot runner the pre-push
hook uses — must exit 0 on that head: ruff check, ruff format,
no-source-bundles, full pytest, all inside a throwaway worktree of exactly that
commit. A gate that cannot run refuses the merge; it never reads as green.

### The bot only runs the owner's own code

The local gate executes the pull request's *own* code — `local_ci.sh`,
`conftest.py`, every test file — on this Mac, beside `.env`, the GitHub token
and the SSH keys. This repository is public, so for anyone else's PR that is
remote code execution, which is exactly what the first independent review of
PR #90 returned as CRITICAL. Pinning commands would not have closed it:
running the PR's pytest *is* running the PR's code.

So `merge_bot.sh` asks whose code it is first
(`lib/pr_is_owner_authored.sh`): the head branch must live in this repository
(not a fork) **and** the author must be the owner login
(`AUTOPILOT_TRUSTED_AUTHOR`, default `sergi039`). Anything else — forks,
Dependabot, any outside contributor — is skipped: the bot neither merges nor
executes it, and the owner handles it by hand. Unreadable metadata counts as
untrusted.

Both checks are needed. Dependabot pushes its branches *into* this repository,
so the fork test alone would wave it through; and a fork PR can carry any head
branch, so the author test alone would too. Regression coverage:
`tests/test_autopilot_pr_trust.py`.

`.github/workflows/ci.yml` therefore stays: GitHub Actions runs untrusted code
in a disposable VM, which is the one thing this Mac cannot do.

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
tools/autopilot/merge_bot.sh --pr 57        # one specific PR
tools/autopilot/run_issue.sh 24             # one specific issue
```

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

**UNAVAILABLE is not PASS.** A reviewer that cannot run leaves the PR open.

**A verdict covers a diff, not a branch.** The journal key is `base..head`, so
a PASS stops applying the moment main moves — and the merge step re-checks the
base one last time, because a review takes minutes and `--match-head-commit`
only guards the head.

**The branch must be up to date.** A PR that is behind main is refused before
review, not merged and hoped for. `base..head` on a stale branch shows what the
branch changes, which is not what will land: main tightens a helper, the branch
adds a caller written against the old one, both sides review clean, and the
merge combines them into something nobody saw. Requiring the branch to contain
the current base makes the reviewed diff and the merged code the same thing.

### Known residual risk: the base can move during the merge itself

GitHub has no "merge only if the base is still X". Between the last base check
and the `gh pr merge` call — a second or two — main can advance, and the squash
lands on a base nobody reviewed. This cannot be closed from a script.

The bot does two things about it rather than one: it re-checks the base as late
as possible, and after every merge it reads the first parent of the squash
commit and compares it to the reviewed base. A mismatch is logged as `ALERT`
with the commit to inspect. Silent is not the same as safe.

The actual fix is a repository setting the owner has to make — branch
protection → **"Require branches to be up to date before merging"**. With it,
GitHub itself refuses the merge when the base has moved. Worth turning on
before letting this run unattended over a busy main.

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

Note that children inherit the lock along with fd 9 — fine for these scripts,
whose subprocesses all exit before the script does, but worth knowing before
adding a background job to one of them.

## Requirements

`gh` (authenticated), `jq`, `docker`, `uv`, `claude`, `rx`, `python3`, and GNU
`timeout` (`brew install coreutils`).
