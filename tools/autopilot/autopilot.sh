#!/bin/bash
# One pass of the whole loop: pick up issues, then merge whatever qualifies.
#
#   open issues --> run_issue.sh --> PR --> CI --> independent review
#                                                       |
#                                     merge_bot.sh --> squash into main
#                                                       |
#                              deploy_watcher.sh (LaunchAgent) --> rebuild + healthz
#
# Deploy is not called from here: it runs on its own timer so a deploy still
# happens for commits that land by hand.
#
# Usage:
#   autopilot.sh                    # one issue, then a merge pass
#   autopilot.sh --issues 3         # up to three issues this pass
#   autopilot.sh --merge-only       # skip the issue work
#   autopilot.sh --dry-run          # decide and report, change nothing
#
# Issue order is the repository's own: lowest number first among the labels in
# PRIORITY_LABELS. Nothing here reorders the owner's backlog.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${AUTOPILOT_REPO_DIR:-/Users/ss/IdealistaRank}"
REPO_SLUG="${AUTOPILOT_REPO_SLUG:-sergi039/idealista-tracker-ai}"
LOG_FILE="${AUTOPILOT_LOG:-${REPO_DIR}/data/autopilot.log}"
LOCK_DIR="${AUTOPILOT_LOCK_DIR_MAIN:-/tmp/idealista-autopilot.lock.d}"

# Highest-severity work first; an issue with none of these labels is left alone
# unless the owner asks for it by number.
PRIORITY_LABELS="${AUTOPILOT_LABELS:-high,medium,low}"

MAX_ISSUES=1
MERGE_ONLY=0
DRY_RUN=0
# Non-zero when any stage failed. The pass still runs to the end - one bad
# issue should not stop the merge phase - but the exit code has to say so.
exit_status=0

while [ $# -gt 0 ]; do
    case "$1" in
        --issues) MAX_ISSUES="${2:?--issues needs a number}"; shift 2 ;;
        --merge-only) MERGE_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

log() {
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")"

# --- single instance -------------------------------------------------------
# An agent run takes minutes to an hour. Overlapping passes would point two
# agents at the same issue, which is how PRs #57 and #58 happened.
# shellcheck source=lib/lock.sh
source "${HERE}/lib/lock.sh"
if ! autopilot_acquire_lock "$LOCK_DIR"; then
    log "another autopilot pass is running, skipping"
    exit 0
fi

cd "$REPO_DIR" || { echo "repo not found: $REPO_DIR" >&2; exit 1; }

dry_run_note=""
[ "$DRY_RUN" = "1" ] && dry_run_note=" (dry run)"
log "=== autopilot pass start${dry_run_note} ==="

# --- issues ----------------------------------------------------------------
if [ "$MERGE_ONLY" = "0" ]; then
    # Issues that already have an open PR are excluded here as well as inside
    # run_issue.sh: no point spending an agent's hour to have it refuse.
    claimed="$(gh pr list --repo "$REPO_SLUG" --state open --limit 100 \
        --json title,headRefName \
        --jq '[.[] | (.headRefName | capture("issue-(?<n>[0-9]+)$").n // empty),
                     (.title | capture("#(?<n>[0-9]+)").n // empty)] | flatten | unique | join(" ")' \
        2>/dev/null || echo "")"
    log "issues already covered by an open PR: ${claimed:-none}"

    candidates="$(gh issue list --repo "$REPO_SLUG" --state open --limit 100 \
        --label "$(printf '%s' "$PRIORITY_LABELS" | cut -d, -f1)" \
        --json number --jq '[.[].number] | sort | join(" ")' 2>/dev/null || echo "")"

    if [ -z "$candidates" ]; then
        log "no open issues with label '$(printf '%s' "$PRIORITY_LABELS" | cut -d, -f1)'"
    fi

    started=0
    for issue in $candidates; do
        [ "$started" -ge "$MAX_ISSUES" ] && break
        case " $claimed " in
            *" $issue "*) log "#${issue}: already has a PR, skipping"; continue ;;
        esac

        if [ "$DRY_RUN" = "1" ]; then
            log "WOULD START #${issue} (dry run)"
        else
            log "--- starting #${issue} ---"
            if ! "${HERE}/run_issue.sh" "$issue"; then
                # A red suite is a normal outcome for one issue and must not
                # abort the pass, but it is still a failure of this pass.
                log "#${issue}: run_issue.sh exited non-zero"
                exit_status=1
            fi
        fi
        started=$((started + 1))
    done
    log "issue phase done (${started} started)"
fi

# --- merges ----------------------------------------------------------------
merge_args=()
[ "$DRY_RUN" = "1" ] && merge_args+=(--dry-run)

if ! "${HERE}/merge_bot.sh" "${merge_args[@]+"${merge_args[@]}"}"; then
    # Distinct from "nothing qualified to merge", which exits 0. This is an
    # auth failure, a bad base, or a crash - swallowing it would make a broken
    # pass look like a quiet one.
    log "merge_bot.sh FAILED"
    exit_status=1
fi

if [ "$exit_status" = "0" ]; then
    log "=== autopilot pass end ==="
else
    log "=== autopilot pass end WITH FAILURES ==="
fi
exit "$exit_status"
