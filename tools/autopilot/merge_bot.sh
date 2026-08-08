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
# CI is not a required status check on this repository, so the bot enforces its
# own list. A check that is absent, skipped or neutral is not a pass.
REQUIRED_CHECKS="${AUTOPILOT_REQUIRED_CHECKS:-pytest no-source-bundles}"

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
# shellcheck source=lib/lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/lock.sh"
if ! autopilot_acquire_lock "$LOCK_DIR"; then
    log "another merge_bot run is in progress, skipping"
    exit 0
fi

# A verdict covers one diff, and a diff is (base, head) — not head alone. When
# main moves from B1 to B2 the merged result is different code even though the
# PR head never changed, so the key has to include the base or a stale PASS
# gets applied to a diff nobody reviewed.
verdict_key() {
    printf '%s..%s' "$1" "$2"
}

journal_verdict() {
    # Last recorded verdict for this exact base..head pair, empty if unreviewed.
    awk -F'\t' -v key="$1" '$1 == key { v = $2 } END { print v }' "$JOURNAL"
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

    # SKIPPED and NEUTRAL are tolerated *alongside* real results, never instead
    # of them: a run where every check skipped verifies exactly as much as a run
    # with no checks at all, and must not read as green.
    for required in $REQUIRED_CHECKS; do
        local state
        state="$(printf '%s' "$checks" \
            | jq -r --arg n "$required" '[.[] | select(.name == $n) | .state] | first // "MISSING"')"
        if [ "$state" != "SUCCESS" ]; then
            log "  PR #${pr}: required check '${required}' is ${state}, not SUCCESS"
            return 1
        fi
    done
    return 0
}

# --- gate 2: independent review -------------------------------------------
review_is_pass() {
    local pr="$1" head_sha="$2" base_sha="$3" cached rc ref key

    key="$(verdict_key "$base_sha" "$head_sha")"
    cached="$(journal_verdict "$key")"
    case "$cached" in
        PASS)
            log "  PR #${pr}: cached PASS for ${base_sha:0:7}..${head_sha:0:7}"
            return 0 ;;
        BLOCKER)
            log "  PR #${pr}: cached BLOCKER for ${base_sha:0:7}..${head_sha:0:7} - needs a human or a new push"
            return 1 ;;
    esac

    ref="refs/autopilot/pr-${pr}"
    if ! git fetch --quiet --force "https://github.com/${REPO_SLUG}.git" \
        "pull/${pr}/head:${ref}" 2>>"$LOG_FILE"; then
        log "  PR #${pr}: could not fetch head - skipping"
        return 1
    fi

    # The fetch happens after the listing, so it can pick up a different commit
    # than the one the merge will pin with --match-head-commit. A force-push to
    # B and back to A would otherwise get A merged on B's PASS. Review only the
    # commit that is actually going to be merged.
    local fetched_sha
    fetched_sha="$(git rev-parse "$ref" 2>/dev/null || true)"
    if [ "$fetched_sha" != "$head_sha" ]; then
        log "  PR #${pr}: head moved during fetch (${head_sha:0:7} -> ${fetched_sha:0:7}) - skipping"
        return 1
    fi

    # The reviewed diff has to BE the merge result, not just the branch's own
    # changes. `base..head` on a branch that is behind hides semantic merge
    # conflicts: main tightens a helper, the branch adds a caller written
    # against the old helper, each side reviews clean, and the merge silently
    # combines them into something nobody looked at.
    #
    # Requiring the branch to already contain the current base makes
    # base..head exactly the code that will land. This is the same rule as
    # GitHub's "require branches to be up to date before merging" - enforced
    # here because the repository does not have it switched on.
    if ! git merge-base --is-ancestor "$base_sha" "$ref" 2>/dev/null; then
        log "  PR #${pr}: behind ${BASE_BRANCH} (${base_sha:0:7}) - rebase it; a diff that"
        log "            is not the merge result cannot be reviewed as one"
        return 1
    fi

    log "  PR #${pr}: requesting independent review of ${base_sha:0:7}..${head_sha:0:7}"
    set +e
    rx --range "${base_sha}..${ref}" \
        "Review this pull request for merge into ${BASE_BRANCH} of a self-hosted Flask
app that ingests real estate listings.

Read the diff. Do NOT run gh, git push, docker or any other state-changing
command: this review decides whether an automated merge happens, and a reviewer
that runs 'gh pr merge' would perform the very action it is gating. Observed
once in practice - the reviewer invoked gh pr merge and only a missing flag
stopped it.

Judge correctness, security, error handling and whether the tests actually
prove the claimed behaviour rather than mocking past it. This repository has a
history of tests that mock the failing call itself and therefore pass against a
broken fix. Return BLOCKER if the change is wrong, unproven, or weakens the
existing security posture (auth on state-changing endpoints, CSRF, rate limits,
parameterised queries)." >>"$LOG_FILE" 2>&1
    rc=$?
    set -e

    case "$rc" in
        0) log "  PR #${pr}: review PASS"
           record_verdict "$key" PASS "pr-${pr}"
           return 0 ;;
        4) log "  PR #${pr}: review BLOCKER - left open"
           record_verdict "$key" BLOCKER "pr-${pr}"
           return 1 ;;
        *) log "  PR #${pr}: review UNAVAILABLE (rc=${rc}) - not a pass, retrying next tick"
           return 1 ;;
    esac
}

# --- after the fact --------------------------------------------------------
# GitHub offers no "merge only if the base is still X". `--match-head-commit`
# pins the head; the base can still advance between the last check and the
# merge, and the squash then lands on a base nobody reviewed. The window is
# a second or two and cannot be closed from a script - the real fix is branch
# protection's "require branches to be up to date before merging", which is the
# owner's setting to make.
#
# What a script *can* do is refuse to pretend it did not happen: the first
# parent of a squash commit is the base it landed on, so compare it and say so.
verify_merged_onto_reviewed_base() {
    local pr="$1" reviewed_base="$2" merge_sha actual_base

    # "Could not check" is not "checked and fine". Both unreadable cases return
    # non-zero so the caller reports the merge as needing eyes, rather than
    # logging a warning nobody acts on and carrying on as if verified.
    merge_sha="$(gh pr view "$pr" --repo "$REPO_SLUG" \
        --json mergeCommit --jq '.mergeCommit.oid // empty' 2>/dev/null || true)"
    if [ -z "$merge_sha" ]; then
        log "  ALERT: #${pr} merged but the merge commit could not be read - base UNVERIFIED"
        return 1
    fi

    actual_base="$(gh api "repos/${REPO_SLUG}/commits/${merge_sha}" \
        --jq '.parents[0].sha // empty' 2>/dev/null || true)"
    if [ -z "$actual_base" ]; then
        log "  ALERT: #${pr} merged (${merge_sha:0:7}) but its base could not be read - UNVERIFIED"
        return 1
    fi

    if [ "$actual_base" != "$reviewed_base" ]; then
        log "  ALERT: #${pr} was reviewed against ${reviewed_base:0:7} but landed on ${actual_base:0:7}."
        log "         ${BASE_BRANCH} moved during the merge; the merged result differs from"
        log "         the reviewed diff. Inspect ${merge_sha:0:7} by hand."
        return 1
    fi
    return 0
}

# --- main ------------------------------------------------------------------
# A failed fetch means the local base ref is stale. Continuing would review
# against yesterday's main and then merge into today's — exactly the diff
# nobody looked at. Stop instead; the next tick retries.
if ! git fetch --quiet origin "$BASE_BRANCH"; then
    log "git fetch failed - cannot establish the merge base, aborting this pass"
    exit 1
fi
BASE_SHA="$(git rev-parse "origin/${BASE_BRANCH}")"
log "merge base: ${BASE_SHA:0:7}"

if [ -n "$ONLY_PR" ]; then
    pr_query="$(gh pr view "$ONLY_PR" --repo "$REPO_SLUG" \
        --json number,title,isDraft,mergeable,headRefOid --jq '[.]')"
else
    pr_query="$(gh pr list --repo "$REPO_SLUG" --state open --limit 50 \
        --json number,title,isDraft,mergeable,headRefOid)"
fi

count="$(printf '%s' "$pr_query" | jq 'length')"
dry_run_note=""
[ "$DRY_RUN" = "1" ] && dry_run_note=" (dry run)"
log "evaluating ${count} open PR(s)${dry_run_note}"

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
    review_is_pass "$number" "$head_sha" "$BASE_SHA" || continue

    if [ "$DRY_RUN" = "1" ]; then
        log "  WOULD MERGE #${number} (dry run)"
        continue
    fi

    # A review takes minutes. If main moved while it ran, the reviewed diff is
    # not the diff GitHub would merge — `--match-head-commit` guards the head
    # but nothing guards the base. Re-check and defer; the next tick reviews
    # against the new base and the verdict key makes that a fresh review.
    base_now="$(git ls-remote origin "refs/heads/${BASE_BRANCH}" 2>/dev/null | cut -f1)"
    if [ -z "$base_now" ]; then
        log "  cannot confirm ${BASE_BRANCH} head - deferring merge of #${number}"
        continue
    fi
    if [ "$base_now" != "$BASE_SHA" ]; then
        log "  ${BASE_BRANCH} moved ${BASE_SHA:0:7} -> ${base_now:0:7} during review - deferring #${number}"
        continue
    fi

    if gh pr merge "$number" --repo "$REPO_SLUG" --squash --delete-branch \
        --match-head-commit "$head_sha" >>"$LOG_FILE" 2>&1; then
        log "  MERGED #${number}"
        # Explicit `if`: a bare call returning non-zero would trip `set -e` and
        # abort the whole pass right after a successful merge.
        if ! verify_merged_onto_reviewed_base "$number" "$BASE_SHA"; then
            log "  #${number} NEEDS MANUAL INSPECTION - see the ALERT above"
        fi
    else
        # --match-head-commit fails the merge if someone pushed after the
        # review; that is the desired outcome, not an error to work around.
        log "  merge failed for #${number} (head moved, or branch protection refused)"
    fi
done
