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

Nothing here bypasses a gate. A PR merges only with **green CI and a PASS from
an independent reviewer**; a deploy survives only if `/api/healthz` answers
`"ok":true` afterwards.

What counts as green is spelled out rather than inferred: every check in
`AUTOPILOT_REQUIRED_CHECKS` (default `pytest no-source-bundles`) must report
`SUCCESS`. A run where the required checks are absent, skipped or neutral
verifies exactly as much as a run with no CI at all, and is refused.

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
`data/autopilot-reviews.tsv`, keyed by head SHA — a re-push gets a fresh review,
a re-run does not burn another reviewer attempt on an unchanged diff.

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
