#!/bin/bash
# Merge open PRs that pass both gates: green CI and an independent reviewer.
#
# The owner authorised unattended squash-merges for this repository only, and
# only behind those two gates. Everything else here exists to make that safe:
#
#   - a PR is never merged on a stale verdict: the review is keyed to the exact
#     head SHA, and a new push invalidates it
#   - `rx` reserves a bounded number of review attempts per diff, so verdicts
#     are cached in a journal rather than re-requested on every tick
#   - UNAVAILABLE is not a pass. Neither is a pending check.
#
# Usage:
#   merge_bot.sh              # review and merge what qualifies
#   merge_bot.sh --dry-run    # decide and report, merge nothing
#   merge_bot.sh --pr 57      # single PR

set -euo pipefail

REPO_DIR="${AUTOPILOT_REPO_DIR:-/Users/ss/IdealistaRank}"
REPO_SLUG="${AUTOPILOT_REPO_SLUG:-sergi039/idealista-tracker-ai}"
BASE_BRANCH="${AUTOPILOT_BRANCH:-main}"
LOCK_DIR="${AUTOPILOT_MERGE_LOCK_DIR:-/tmp/idealista-autopilot-merge.lock.d}"
LOG_FILE="${AUTOPILOT_MERGE_LOG:-${REPO_DIR}/data/autopilot-merge.log}"
# One line per reviewed head SHA. Keyed by SHA so a re-push gets a fresh review
# and a re-run does not burn another rx attempt on an unchanged diff.
JOURNAL="${AUTOPILOT_REVIEW_JOURNAL:-${REPO_DIR}/data/autopilot-reviews.tsv}"

DRY_RUN=0
ONLY_PR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --pr) ONLY_PR="${2:?--pr needs a number}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

log() {
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$JOURNAL")"
touch "$JOURNAL"
cd "$REPO_DIR" || { echo "repo not found: $REPO_DIR" >&2; exit 1; }

# --- single instance -------------------------------------------------------
if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ >"${LOCK_DIR}/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT
else
    holder="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
        rm -rf "$LOCK_DIR"
        mkdir "$LOCK_DIR" && echo $$ >"${LOCK_DIR}/pid"
        trap 'rm -rf "$LOCK_DIR"' EXIT
    else
        log "another merge_bot run is in progress, skipping"
        exit 0
    fi
fi

journal_verdict() {
    # Last recorded verdict for a head SHA, empty when never reviewed.
    awk -F'\t' -v sha="$1" '$1 == sha { v = $2 } END { print v }' "$JOURNAL"
}

record_verdict() {
    printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$(date '+%Y-%m-%dT%H:%M:%S')" "$3" >>"$JOURNAL"
}

# --- gate 1: CI ------------------------------------------------------------
ci_is_green() {
    local pr="$1" checks bad pending
    # `gh pr checks` exits non-zero both for a failure and while checks are
    # still running, so read the states rather than the exit code.
    checks="$(gh pr checks "$pr" --repo "$REPO_SLUG" --json name,state 2>/dev/null || true)"

    if [ -z "$checks" ] || [ "$(printf '%s' "$checks" | jq 'length')" = "0" ]; then
        # CI is not a required status check on this repo, so "no checks" means
        # nothing verified this diff - which is not the same as green.
        log "  PR #${pr}: no checks reported - not merging an unverified diff"
        return 1
    fi

    pending="$(printf '%s' "$checks" \
        | jq -r '[.[] | select(.state | IN("PENDING","QUEUED","IN_PROGRESS","EXPECTED"))] | length')"
    if [ "$pending" != "0" ]; then
        log "  PR #${pr}: ${pending} check(s) still running"
        return 1
    fi

    bad="$(printf '%s' "$checks" \
        | jq -r '[.[] | select(.state | IN("SUCCESS","SKIPPED","NEUTRAL") | not) | .name] | join(", ")')"
    if [ -n "$bad" ]; then
        log "  PR #${pr}: CI red (${bad})"
        return 1
    fi
    return 0
}

# --- gate 2: independent review -------------------------------------------
review_is_pass() {
    local pr="$1" head_sha="$2" cached rc ref

    cached="$(journal_verdict "$head_sha")"
    case "$cached" in
        PASS)
            log "  PR #${pr}: cached PASS for ${head_sha:0:7}"
            return 0 ;;
        BLOCKER)
            log "  PR #${pr}: cached BLOCKER for ${head_sha:0:7} - needs a human or a new push"
            return 1 ;;
    esac

    ref="refs/autopilot/pr-${pr}"
    if ! git fetch --quiet --force "https://github.com/${REPO_SLUG}.git" \
        "pull/${pr}/head:${ref}" 2>>"$LOG_FILE"; then
        log "  PR #${pr}: could not fetch head - skipping"
        return 1
    fi

    log "  PR #${pr}: requesting independent review of ${head_sha:0:7}"
    set +e
    rx --range "origin/${BASE_BRANCH}..${ref}" \
        "Review this pull request for merge into ${BASE_BRANCH} of a self-hosted Flask
app that ingests real estate listings. Judge correctness, security, error
handling and whether the tests actually prove the claimed behaviour rather than
mocking past it. This repository has a history of tests that mock the failing
call itself and therefore pass against a broken fix. Return BLOCKER if the
change is wrong, unproven, or weakens the existing security posture
(admin_required on state-changing endpoints, CSRF, rate limits, parameterised
queries)." >>"$LOG_FILE" 2>&1
    rc=$?
    set -e

    case "$rc" in
        0) log "  PR #${pr}: review PASS"
           record_verdict "$head_sha" PASS "pr-${pr}"
           return 0 ;;
        4) log "  PR #${pr}: review BLOCKER - left open"
           record_verdict "$head_sha" BLOCKER "pr-${pr}"
           return 1 ;;
        *) log "  PR #${pr}: review UNAVAILABLE (rc=${rc}) - not a pass, retrying next tick"
           return 1 ;;
    esac
}

# --- main ------------------------------------------------------------------
git fetch --quiet origin "$BASE_BRANCH" || log "WARNING: git fetch failed; review base may be stale"

if [ -n "$ONLY_PR" ]; then
    pr_query="$(gh pr view "$ONLY_PR" --repo "$REPO_SLUG" \
        --json number,title,isDraft,mergeable,headRefOid --jq '[.]')"
else
    pr_query="$(gh pr list --repo "$REPO_SLUG" --state open --limit 50 \
        --json number,title,isDraft,mergeable,headRefOid)"
fi

count="$(printf '%s' "$pr_query" | jq 'length')"
log "evaluating ${count} open PR(s)${DRY_RUN:+ (dry run)}"

printf '%s' "$pr_query" | jq -c '.[]' | while read -r pr_json; do
    number="$(printf '%s' "$pr_json" | jq -r '.number')"
    title="$(printf '%s' "$pr_json" | jq -r '.title')"
    draft="$(printf '%s' "$pr_json" | jq -r '.isDraft')"
    mergeable="$(printf '%s' "$pr_json" | jq -r '.mergeable')"
    head_sha="$(printf '%s' "$pr_json" | jq -r '.headRefOid')"

    log "PR #${number}: ${title}"

    if [ "$draft" = "true" ]; then
        log "  draft - skipping"
        continue
    fi
    if [ "$mergeable" = "CONFLICTING" ]; then
        log "  conflicts with ${BASE_BRANCH} - skipping"
        continue
    fi

    ci_is_green "$number" || continue
    review_is_pass "$number" "$head_sha" || continue

    if [ "$DRY_RUN" = "1" ]; then
        log "  WOULD MERGE #${number} (dry run)"
        continue
    fi

    if gh pr merge "$number" --repo "$REPO_SLUG" --squash --delete-branch \
        --match-head-commit "$head_sha" >>"$LOG_FILE" 2>&1; then
        log "  MERGED #${number}"
    else
        # --match-head-commit fails the merge if someone pushed after the
        # review; that is the desired outcome, not an error to work around.
        log "  merge failed for #${number} (head moved, or branch protection refused)"
    fi
done
