#!/bin/bash
# Add the one gate GitHub cannot: an independent reviewer, then merge.
#
# Branch protection on main now requires the status checks listed in the
# protection itself and refuses any branch that is behind — enforced by GitHub,
# atomically, at merge time. This script no longer re-implements either rule.
# What it adds is the reviewer, and the bookkeeping that makes a verdict mean
# something:
#
#   - a verdict is keyed to base..head, so a new push or a moved base gets a
#     fresh review instead of inheriting an old PASS
#   - the commit that was reviewed is the commit that gets merged
#     (`--match-head-commit`), never a force-push in between
#   - `rx` reserves a bounded number of attempts per diff, so verdicts are
#     journalled rather than re-requested every tick
#   - UNAVAILABLE is not a pass, and a BLOCKER is posted on the PR rather than
#     buried in a local log
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
# Branch protection is the authority on what must be green. Reading its list
# rather than keeping a copy here means the two can never drift: add a check to
# protection and the bot honours it on the next tick, with no edit.
#
# Overridable for a dry run against a repository whose protection is not set up.
REQUIRED_CHECKS_OVERRIDE="${AUTOPILOT_REQUIRED_CHECKS:-}"

# One newline-separated name per line: a check may legitimately be called
# "Unit tests / pytest", and word-splitting would turn that into four names
# that match nothing, quietly blocking every PR.
required_checks() {
    if [ -n "$REQUIRED_CHECKS_OVERRIDE" ]; then
        printf '%s\n' $REQUIRED_CHECKS_OVERRIDE
        return 0
    fi
    gh api "repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/required_status_checks" \
        --jq '.contexts[]' 2>/dev/null || true
}

# The bot no longer re-checks the base before merging, because `strict` makes
# GitHub refuse a behind-branch atomically. That deletion is only sound while
# `strict` is actually on - switch it off and the guarantee vanishes with no
# sign in the log. Verify it, and refuse to merge rather than merge blind.
strict_protection_is_on() {
    [ -n "$REQUIRED_CHECKS_OVERRIDE" ] && return 0
    [ "$(gh api "repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/required_status_checks" \
        --jq '.strict' 2>/dev/null)" = "true" ]
}

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
# Not a gate any more - branch protection is. This only avoids spending a
# minutes-long independent review on a PR that GitHub is going to refuse.
ci_is_green() {
    local pr="$1" checks bad pending
    # `gh pr checks` exits non-zero both for a failure and while checks are
    # still running, so read the states rather than the exit code.
    checks="$(gh pr checks "$pr" --repo "$REPO_SLUG" --json name,state 2>/dev/null || true)"

    if [ -z "$checks" ] || [ "$(printf '%s' "$checks" | jq 'length')" = "0" ]; then
        log "  PR #${pr}: no checks reported yet"
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
    local required state missing=0
    while IFS= read -r required; do
        [ -n "$required" ] || continue
        state="$(printf '%s' "$checks" \
            | jq -r --arg n "$required" '[.[] | select(.name == $n) | .state] | first // "MISSING"')"
        if [ "$state" != "SUCCESS" ]; then
            log "  PR #${pr}: required check '${required}' is ${state}, not SUCCESS"
            missing=1
        fi
    done <<<"$(required_checks)"
    [ "$missing" = "0" ]
}

# A BLOCKER that only reaches data/autopilot-merge.log is a verdict nobody
# sees: the PR just sits there looking mergeable. Put the reason where the work
# is. Failure to comment is logged but never blocks - the merge was refused
# either way, which is the part that matters.
post_blocker_comment() {
    local pr="$1" base_sha="$2" head_sha="$3" verdict

    # The reviewer's own words, from the tail of the log it just wrote. This
    # script's own lines carry a timestamp prefix and are dropped, so the
    # comment quotes the review rather than the plumbing around it.
    verdict="$(awk '/^rx: .*outcome=BLOCKER/{found=NR} {lines[NR]=$0} END {
        if (found) for (i = found; i <= NR; i++) print lines[i]
    }' "$LOG_FILE" 2>/dev/null | grep -v '^20[0-9][0-9]-[0-9][0-9]-[0-9][0-9] ' | head -40)"

    if [ -z "$verdict" ]; then
        verdict="(the reviewer returned BLOCKER; see data/autopilot-merge.log)"
    fi

    gh pr comment "$pr" --repo "$REPO_SLUG" --body "## Automated review: BLOCKER

\`tools/autopilot/merge_bot.sh\` took this PR through both gates — CI green,
branch up to date — and stopped at the independent review. Not merged.

\`\`\`
${verdict}
\`\`\`

Reviewed range: \`${base_sha:0:7}..${head_sha:0:7}\`. Push a fix and the bot
re-reviews automatically: verdicts are keyed to the base..head pair, so a new
commit is never covered by this one." >>"$LOG_FILE" 2>&1 \
        || log "  PR #${pr}: could not post the BLOCKER comment (merge still refused)"
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
    # Branch protection now enforces the same rule at merge time ("require
    # branches to be up to date"), so this is no longer what keeps a stale
    # branch out of main. It stays because it protects the *review*: sending a
    # reviewer a diff that is not the merge result buys a verdict about code
    # that will never exist, and costs a bounded rx attempt to do it.
    if ! git merge-base --is-ancestor "$base_sha" "$ref" 2>/dev/null; then
        log "  PR #${pr}: behind ${BASE_BRANCH} (${base_sha:0:7}) - rebase it before it can be reviewed"
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
           post_blocker_comment "$pr" "$base_sha" "$head_sha"
           return 1 ;;
        *) log "  PR #${pr}: review UNAVAILABLE (rc=${rc}) - not a pass, retrying next tick"
           return 1 ;;
    esac
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
warn_about_unreported_checks

# Fail closed. Everything below assumes GitHub will reject a merge whose branch
# is behind; without `strict` that assumption is silently false and the bot
# would merge a diff nobody reviewed.
# A required context is matched by name. Rename a job in ci.yml and the old
# name stays required and never reports — GitHub blocks every merge on a check
# that will be pending forever, and nothing says why. Cheap to catch here, on
# the way past, rather than from a PR that mysteriously will not merge.
warn_about_unreported_checks() {
    local workflow="${REPO_DIR}/.github/workflows/ci.yml" required
    [ -f "$workflow" ] || return 0
    while IFS= read -r required; do
        [ -n "$required" ] || continue
        grep -qF "name: ${required}" "$workflow" && continue
        log "WARNING: required check '${required}' is not a job name in ci.yml."
        log "         If nothing else reports it, every PR will sit on a check that"
        log "         never arrives. Fix the name in protection or in the workflow."
    done <<<"$(required_checks)"
}

if ! strict_protection_is_on; then
    log "FATAL: ${BASE_BRANCH} protection does not have strict (require branches up to date)."
    log "       The bot relies on it instead of re-checking the base itself. Enable it with:"
    log "       gh api -X PATCH repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/required_status_checks -f strict=true"
    exit 1
fi

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

    # Branch protection settles the base race: with `strict` on, GitHub refuses
    # a merge whose branch is behind main, atomically, at merge time. The bot
    # used to re-check the base here and audit the squash commit's first parent
    # afterwards — both were approximations of exactly that rule, with a window
    # a script cannot close. They are gone; `--match-head-commit` still pins the
    # head so the merged commit is the reviewed one.
    if gh pr merge "$number" --repo "$REPO_SLUG" --squash --delete-branch \
        --match-head-commit "$head_sha" >>"$LOG_FILE" 2>&1; then
        log "  MERGED #${number}"
    else
        # Expected outcomes, not errors to work around: the head moved after the
        # review, or main moved and protection now wants a rebase.
        log "  merge refused for #${number} (head moved, branch behind ${BASE_BRANCH}, or a required check is not green)"
    fi
done
