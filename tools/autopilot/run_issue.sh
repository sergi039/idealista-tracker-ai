#!/bin/bash
# Fix one GitHub issue unattended: isolate, implement, test, open a PR.
#
# The merge decision does NOT live here - this script only ever produces a PR.
# merge_bot.sh applies the CI + independent-review gates afterwards.
#
# Duplicate protection is the point of half this script. PRs #57 and #58 both
# "fixed" issue #17, in different files, because two agents were pointed at the
# same issue with nothing to stop them. An issue that already has a branch or an
# open PR is skipped here, not raced.
#
# Usage:
#   run_issue.sh 14
#   run_issue.sh 14 --keep-worktree     # leave the worktree for inspection

set -euo pipefail

REPO_DIR="${AUTOPILOT_REPO_DIR:-/Users/ss/IdealistaRank}"
REPO_SLUG="${AUTOPILOT_REPO_SLUG:-sergi039/idealista-tracker-ai}"
BASE_BRANCH="${AUTOPILOT_BRANCH:-main}"
LOG_FILE="${AUTOPILOT_ISSUE_LOG:-${REPO_DIR}/data/autopilot-issues.log}"
WORKTREE_ROOT="${AUTOPILOT_WORKTREE_ROOT:-${REPO_DIR}/.claude/worktrees}"
# An agent that has not produced a commit by now is stuck, not thinking.
AGENT_TIMEOUT_SECONDS="${AUTOPILOT_AGENT_TIMEOUT:-3600}"

ISSUE="${1:?usage: run_issue.sh <issue-number> [--keep-worktree]}"
shift || true
KEEP_WORKTREE=0
[ "${1:-}" = "--keep-worktree" ] && KEEP_WORKTREE=1

BRANCH="claude/issue-${ISSUE}"
WORKTREE="${WORKTREE_ROOT}/autopilot-issue-${ISSUE}"

log() {
    printf '%s  [#%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$ISSUE" "$*" | tee -a "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")"
cd "$REPO_DIR" || { echo "repo not found: $REPO_DIR" >&2; exit 1; }

# --- refuse to duplicate work ---------------------------------------------
existing_pr="$(gh pr list --repo "$REPO_SLUG" --state open --limit 100 \
    --json number,title,headRefName \
    --jq "[.[] | select((.headRefName | test(\"issue-${ISSUE}\$\")) or (.title | test(\"#${ISSUE}\\\\b\")))] | .[0].number" 2>/dev/null || true)"
if [ -n "$existing_pr" ] && [ "$existing_pr" != "null" ]; then
    log "already has open PR #${existing_pr} - refusing to open a second one"
    exit 0
fi

if git show-ref --verify --quiet "refs/heads/${BRANCH}" \
    || git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    log "branch ${BRANCH} already exists - another run owns this issue"
    exit 0
fi

state="$(gh issue view "$ISSUE" --repo "$REPO_SLUG" --json state --jq .state 2>/dev/null || echo MISSING)"
if [ "$state" != "OPEN" ]; then
    log "issue is ${state}, not OPEN - nothing to do"
    exit 0
fi

title="$(gh issue view "$ISSUE" --repo "$REPO_SLUG" --json title --jq .title)"
log "starting: ${title}"

# --- isolated worktree -----------------------------------------------------
git fetch --quiet origin "$BASE_BRANCH"
rm -rf "$WORKTREE"
git worktree add --quiet -b "$BRANCH" "$WORKTREE" "origin/${BASE_BRANCH}" \
    || { log "could not create worktree"; exit 1; }

cleanup() {
    if [ "$KEEP_WORKTREE" = "1" ]; then
        log "worktree kept at ${WORKTREE}"
        return
    fi
    cd "$REPO_DIR"
    git worktree remove --force "$WORKTREE" 2>/dev/null || true
}
trap cleanup EXIT

# --- the agent -------------------------------------------------------------
prompt="$(cat <<PROMPT
Fix GitHub issue #${ISSUE} in this repository: ${title}

Read the full issue first:  gh issue view ${ISSUE} --repo ${REPO_SLUG} --comments

Rules for this task:
- Read CLAUDE.md and follow it. The security posture (admin_required, CSRF,
  rate limits, parameterised queries) is hard-won; weakening it is a defect.
- Write a regression test that FAILS without your fix. Verify that by
  temporarily reverting the fix and watching the test fail, then restore it.
  A test that mocks the failing call itself proves nothing - this repository
  has shipped that mistake before (#14 hid behind mocked services for six
  months).
- Do not run bulk backfills or anything that spends money on external APIs
  (Anthropic, OpenAI, Google Places / Distance Matrix).
- Run the whole suite: uv run pytest tests/ -q. It must be green.
- Commit with a conventional-commit message ending in "(#${ISSUE})".
  Do not push and do not open a PR - the surrounding automation does that.
- If the issue turns out to be wrong, already fixed, or unsafe to fix as
  written, make no commit and say so plainly in your final message.
PROMPT
)"

log "handing off to the agent (timeout ${AGENT_TIMEOUT_SECONDS}s)"
agent_log="${WORKTREE}/.autopilot-agent.log"

set +e
( cd "$WORKTREE" && \
  timeout "$AGENT_TIMEOUT_SECONDS" \
  claude -p "$prompt" --permission-mode bypassPermissions >"$agent_log" 2>&1 )
agent_rc=$?
set -e

if [ $agent_rc -eq 124 ]; then
    log "agent timed out after ${AGENT_TIMEOUT_SECONDS}s"
elif [ $agent_rc -ne 0 ]; then
    log "agent exited with rc=${agent_rc}"
fi
[ -f "$agent_log" ] && tail -20 "$agent_log" >>"$LOG_FILE"

# --- did it actually do anything? -----------------------------------------
cd "$WORKTREE"
commits="$(git rev-list --count "origin/${BASE_BRANCH}..HEAD")"
if [ "$commits" = "0" ]; then
    log "no commit produced - leaving the issue open"
    cd "$REPO_DIR"
    git branch -D "$BRANCH" 2>/dev/null || true
    exit 0
fi
log "agent produced ${commits} commit(s)"

# --- verify before publishing ---------------------------------------------
log "running the suite"
set +e
test_output="$(uv run pytest tests/ -q 2>&1 | tail -15)"
test_rc=$?
set -e
printf '%s\n' "$test_output" >>"$LOG_FILE"

if [ $test_rc -ne 0 ]; then
    log "SUITE RED - not opening a PR"
    cd "$REPO_DIR"
    git branch -D "$BRANCH" 2>/dev/null || true
    exit 1
fi
log "suite green"

# --- publish ---------------------------------------------------------------
git push --quiet -u origin "$BRANCH" || { log "push failed"; exit 1; }

pr_url="$(gh pr create --repo "$REPO_SLUG" --base "$BASE_BRANCH" --head "$BRANCH" \
    --title "$(git log -1 --pretty=%s)" \
    --body "$(cat <<BODY
Closes #${ISSUE}

Opened automatically by \`tools/autopilot/run_issue.sh\`.

Local suite before opening:

\`\`\`
$(printf '%s' "$test_output" | tail -5)
\`\`\`

Merging is gated separately by \`tools/autopilot/merge_bot.sh\`: green CI plus
an independent reviewer verdict.
BODY
)" 2>&1 | tail -1)"

log "opened ${pr_url}"
