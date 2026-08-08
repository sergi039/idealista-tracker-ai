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
                              deploy_watcher.sh ──▶ rebuild + /api/healthz
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
| `merge_bot.sh` | independent review, verdict bound to the exact diff, BLOCKER posted on the PR |

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

**A verdict covers a diff, not a branch.** The journal key is `base..head`, so a
PASS stops applying the moment either end moves, and `--match-head-commit`
guarantees the merged commit is the reviewed one.

**UNAVAILABLE is not PASS.** A reviewer that cannot run leaves the PR open.

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

Note that children inherit the lock along with fd 9 — fine for these scripts,
whose subprocesses all exit before the script does, but worth knowing before
adding a background job to one of them.

## Requirements

`gh` (authenticated), `jq`, `docker`, `uv`, `claude`, `rx`, `python3`, and GNU
`timeout` (`brew install coreutils`).
