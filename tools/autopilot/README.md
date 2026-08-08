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

## Requirements

`gh` (authenticated), `jq`, `docker`, `uv`, `claude`, `rx`, and GNU `timeout`
(`brew install coreutils`). macOS has no `flock(1)`; the scripts use atomic
`mkdir` locks with stale-PID reclamation instead.
