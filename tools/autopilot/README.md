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
