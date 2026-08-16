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
# Overridable for a dry run against a repository whose protection is not set
# up. Refused outside --dry-run, below: the override also stands in for the
# `strict` verification, so honouring it during a real merge would let one
# environment variable disable the only guarantee that the reviewed diff is
# the diff that lands.
REQUIRED_CHECKS_OVERRIDE="${AUTOPILOT_REQUIRED_CHECKS:-}"
# Wall-clock ceiling on the documentation-only classifier. It reads git objects
# for every file the documentation cites, and it runs while this script holds
# the merge lock.
DOCS_EVIDENCE_TIMEOUT="${AUTOPILOT_DOCS_EVIDENCE_TIMEOUT:-60}"
# Largest diff worth sending to `rx`, and the number is measured (issue #182).
#
# `rx` does not degrade on a large diff, it dies: `cx` pipes the whole codex
# transcript to stderr and the coordinator kills the process group at its 256 KB
# cap, reporting `UNAVAILABLE` - which this script correctly refuses to treat as
# a pass and retries on the next tick, for ever. PR #177 measured 94 621 bytes
# and failed that way twice, at a 240 s and then at an 800 s timeout, failing
# *sooner* with the larger limit because a kill is not a timeout. The seven
# merges before it ran between 3 589 and 35 117 bytes.
#
# 60 000 sits between the largest diff known to work and the one known to fail.
# It is one failure and seven successes, not a curve: re-measure before moving
# it, and do not raise it because a PR happens to be over.
REVIEW_DIFF_MAX_BYTES="${AUTOPILOT_REVIEW_DIFF_MAX_BYTES:-60000}"

# One name per line, split on newlines only. A check may legitimately be called
# "Unit tests / pytest", and word-splitting would turn that into four names
# that match nothing, quietly blocking every PR - which is exactly what the
# unquoted expansion here used to do.
required_checks() {
    if [ -n "$REQUIRED_CHECKS_OVERRIDE" ]; then
        # `|| [ -n "$name" ]` keeps a last line that has no trailing newline,
        # which is the normal shape of AUTOPILOT_REQUIRED_CHECKS=pytest.
        printf '%s' "$REQUIRED_CHECKS_OVERRIDE" | while IFS= read -r name || [ -n "$name" ]; do
            [ -n "$name" ] && printf '%s\n' "$name"
        done
        return 0
    fi
    # Read both shapes and take their union. GitHub returns the same list twice
    # today - `contexts` (deprecated) and `checks[].context`, which also carries
    # the app_id that may report it - but a branch configured through the newer
    # API can populate `checks` while leaving `contexts` empty. Reading only
    # `contexts` would then see nothing required, which since the empty-list
    # guard below means refusing every PR on a correctly protected branch.
    gh api "repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/required_status_checks" \
        --jq '[(.contexts // [])[], ((.checks // [])[] | .context)] | unique[]' 2>/dev/null || true
}

# The bot no longer re-checks the base before merging, because `strict` makes
# GitHub refuse a behind-branch atomically. That deletion is only sound while
# `strict` is actually on - switch it off and the guarantee vanishes with no
# sign in the log. Verify it, and refuse to merge rather than merge blind.
#
# `strict` alone is not enough, and this is the subtle part. It describes the
# rule; `enforce_admins` decides whether the rule binds the identity doing the
# merge. The bot authenticates as the repository owner, an administrator. With
# enforce_admins off, protection would still report strict=true while GitHub
# waved the merge through - and `--match-head-commit` pins only the head, so a
# base that moved from B to B2 after the review passed for B..H would take H
# anyway. That is exactly the race the removed base recheck used to cover.
strict_protection_is_on() {
    # Only reachable with the override set under --dry-run; a real run exits
    # before this point rather than accepting the substitute.
    [ -n "$REQUIRED_CHECKS_OVERRIDE" ] && return 0

    [ "$(gh api "repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/required_status_checks" \
        --jq '.strict' 2>/dev/null)" = "true" ] || return 1

    [ "$(gh api "repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/enforce_admins" \
        --jq '.enabled' 2>/dev/null)" = "true" ]
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

# The override answers both "what must be green" and "is strict protection on",
# so a real run that honoured it would merge without ever checking that the
# base has not moved since the review - the one thing `strict` guarantees. One
# environment variable must not be able to switch that off. Fail closed and say
# why, rather than merging under a weaker rule than the log implies.
if [ -n "$REQUIRED_CHECKS_OVERRIDE" ] && [ "$DRY_RUN" = "0" ]; then
    echo "AUTOPILOT_REQUIRED_CHECKS is a --dry-run affordance: it stands in for" >&2
    echo "branch protection, including the 'strict' verification that keeps the" >&2
    echo "reviewed diff and the merged diff identical. Refusing to merge with it" >&2
    echo "set. Re-run with --dry-run, or unset it and let protection answer." >&2
    exit 2
fi

log() {
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$JOURNAL")"
touch "$JOURNAL"

# Resolved before the `cd`, not after: `$0` may well be a relative path, and
# resolving it from inside $REPO_DIR would find this script's siblings only when
# the invocation happened to start there.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$REPO_DIR" || { echo "repo not found: $REPO_DIR" >&2; exit 1; }

# --- single instance -------------------------------------------------------
# shellcheck source=lib/lock.sh
source "${HERE}/lib/lock.sh"
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
    local required state missing=0 seen=0
    while IFS= read -r required; do
        [ -n "$required" ] || continue
        seen=$((seen + 1))
        state="$(printf '%s' "$checks" \
            | jq -r --arg n "$required" '[.[] | select(.name == $n) | .state] | first // "MISSING"')"
        if [ "$state" != "SUCCESS" ]; then
            log "  PR #${pr}: required check '${required}' is ${state}, not SUCCESS"
            missing=1
        fi
    done <<<"$(required_checks)"

    # An empty list is not "everything passed", it is "nobody said what has to
    # pass". This whole file treats protection as the authority on that, so an
    # authority with nothing to say leaves the bot with no gate at all: the loop
    # above would run zero times and any PR carrying one unrelated green check
    # would merge without pytest. Protection can return `contexts: []` from a
    # half-configured branch or an API hiccup, and neither is a reason to merge.
    if [ "$seen" = "0" ]; then
        log "  PR #${pr}: ${BASE_BRANCH} protection lists no required checks - refusing."
        log "            With nothing declared required there is nothing to verify;"
        log "            a green tick from an unrelated job is not a CI gate."
        return 1
    fi

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

# The size refusal is not a verdict about the code, so it does not read like
# one. It says what the bot could not do and what makes it possible.
post_oversized_comment() {
    local pr="$1" base_sha="$2" head_sha="$3" bytes="$4"

    gh pr comment "$pr" --repo "$REPO_SLUG" --body "## Not reviewed: the diff is too large for the reviewer

\`tools/autopilot/merge_bot.sh\` found CI green and the branch up to date, then
stopped before the independent review. This is not a verdict about the change.

The diff is **${bytes} bytes** against a ceiling of ${REVIEW_DIFF_MAX_BYTES}.
Past roughly that size \`rx\` does not return a verdict at all: \`cx\` pipes the
whole reviewer transcript to stderr and the coordinator kills it at a 256 KB
cap, which surfaces as \`UNAVAILABLE\`. \`UNAVAILABLE\` is not a pass, so the
bot would re-request the same doomed review on every tick. Refusing once and
saying so costs less and tells you more (issue #182).

What makes this mergeable:

- **Split it.** Several PRs under the ceiling each get a real review.
- **Or review it by hand** and merge it yourself. Running the same model
  directly works — it is the wrapper that fails, not the provider.

Range: \`${base_sha:0:7}..${head_sha:0:7}\`. This decision is keyed to that
pair, so pushing a smaller diff gets a fresh look automatically." >>"$LOG_FILE" 2>&1 \
        || log "  PR #${pr}: could not post the oversized comment (review still refused)"
}

# --- gate 2: independent review -------------------------------------------
# Every prompt opens with this. A reviewer that runs `gh pr merge` performs the
# very action it is gating - observed once in practice, where only a missing
# flag stopped it.
review_preamble() {
    printf '%s' "Review this pull request for merge into ${BASE_BRANCH} of a self-hosted Flask
app that ingests real estate listings.

Do NOT run gh, git push, docker or any other state-changing command: this review
decides whether an automated merge happens, and a reviewer that runs
'gh pr merge' would perform the very action it is gating. Observed once in
practice - the reviewer invoked gh pr merge and only a missing flag stopped it."
}

# Every prompt ends with this, because a verdict nobody can parse is a review
# thrown away. `rx` reads the first line and only the first line: it accepts the
# bare keyword, optionally wrapped in Markdown emphasis, and calls everything
# else UNAVAILABLE - which this script correctly refuses to treat as a pass, so
# the PR sits unmergeable and a bounded reviewer attempt is spent on nothing.
#
# Until now no prompt here said so. Measured 2026-08-15 on PR #312: the codex
# reviewer returned two well-argued BLOCKER findings under an opening line of
# prose, and rx reported `outcome=UNAVAILABLE reason="verdict not recognised"`.
# A real review with real findings, discarded on presentation. With this
# paragraph appended, codex complied on the first try, twice in a row.
#
# It states the rule instead of pointing at the parser on purpose: this text
# travels to a model that cannot read reviewer_coordinator.py, and that parser
# is not ours to change - it decides what counts as a verdict for every gate on
# this machine.
#
# One function, appended by both prompts, for the reason #292 gives about the
# render check: a rule written down twice is one that eventually ships
# half-changed. `tests/test_merge_bot_verdict_format.py` fails if either prompt
# stops carrying it.
verdict_format_rule() {
    printf '%s' "FORMAT: your very first line must be exactly 'PASS' or 'BLOCKER', with no
Markdown heading, no prefix and nothing else on that line. Reasoning and any
findings follow on the lines after it."
}

standard_review_prompt() {
    printf '%s\n\n%s\n\n%s\n' "$(review_preamble)" "Read the diff. Judge correctness, security, error handling and whether the
tests actually prove the claimed behaviour rather than mocking past it. This
repository has a history of tests that mock the failing call itself and
therefore pass against a broken fix. Return BLOCKER if the change is wrong,
unproven, or weakens the existing security posture (auth on state-changing
endpoints, CSRF, rate limits, parameterised queries)." "$(verdict_format_rule)"
}

# A documentation-only diff cannot be judged by that prompt, and the failure is
# structural rather than a matter of wording. The reviewer's contract is to
# audit the embedded diff and nothing else; the behaviour a docs PR describes
# lives in the *base* commit, so "the implementation is absent from the diff" is
# the only verdict the contract can produce - and it is not a defect. Hit twice
# on #151, both reviews correct about the diff and wrong about the repository,
# ending in a manual merge (issue #154).
#
# So give the reviewer the missing half instead of asking it to trust: for a
# diff that touches nothing but documentation, docs_review_evidence.py resolves
# the files and backticked identifiers the added text cites against the base and
# embeds those excerpts in the request. The reviewer still audits what it was
# handed - the added documentation and the base source behind its claims. It
# is simply handed the thing the claims are checkable against, which keeps the
# gate honest - a docs PR that misdescribes the code still fails.
#
# Returns 0 only for a documentation-only diff, leaving the prompt in
# DOCS_ONLY_PROMPT; every other outcome, including a broken helper, returns
# non-zero so the caller falls back to the standard prompt. That fallback can
# only make a review harder to pass, never easier.
#
# The prompt travels in a variable rather than on stdout so that `log` still
# works here. A `$(...)` caller would capture the log lines along with the
# prompt: the diagnostics would vanish from the console and, on the success
# path, land inside the text sent to the reviewer.
#
# A second opinion on the same question, taken straight from git rather than
# from the helper's report. Deliberately coarser than the helper's rule - it
# only asks whether every changed path looks like documentation, and says
# nothing about file modes - so it can never accept something the helper
# rejects. What it buys is that the relaxed prompt needs two agreeing answers,
# and one of them does not depend on the helper having run correctly at all.
docs_paths_only() {
    local base_sha="$1" head_ref="$2" listing path lower
    listing="$(git diff --name-only --no-renames "${base_sha}..${head_ref}" 2>/dev/null)" \
        || return 1
    [ -n "$listing" ] || return 1
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        # `tr` rather than ${path,,}: /bin/bash on macOS is still 3.2.
        lower="$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')"
        case "$lower" in
            *.md|docs/*) ;;
            *) return 1 ;;
        esac
    done <<<"$listing"
    return 0
}

DOCS_ONLY_PROMPT=""
docs_only_review_prompt() {
    local base_sha="$1" head_ref="$2" evidence rc timeout_bin

    # A hang here would hold the merge lock for as long as the process lives,
    # and the lock is released by the kernel rather than by a timer. Without a
    # bounded run there is no tick that recovers on its own.
    timeout_bin="$(command -v timeout || command -v gtimeout || true)"
    if [ -z "$timeout_bin" ]; then
        log "  GNU timeout is missing - not running the docs-only classifier unbounded"
        return 1
    fi
    # `timeout 0` means *no limit* to GNU timeout, so an unvalidated 0 here
    # would restore precisely the unbounded run this guard exists to prevent.
    # The digit-count pattern rejects a value long enough to overflow the
    # numeric comparison below rather than letting `[` decide what it means.
    case "$DOCS_EVIDENCE_TIMEOUT" in
        [1-9]|[1-9][0-9]|[1-9][0-9][0-9]) : ;;
        *) log "  AUTOPILOT_DOCS_EVIDENCE_TIMEOUT must be 1..600 seconds - using the strict prompt"
           return 1 ;;
    esac
    if [ "$DOCS_EVIDENCE_TIMEOUT" -gt 600 ]; then
        log "  AUTOPILOT_DOCS_EVIDENCE_TIMEOUT must be 1..600 seconds - using the strict prompt"
        return 1
    fi

    if ! docs_paths_only "$base_sha" "$head_ref"; then
        return 1
    fi

    set +e
    evidence="$("$timeout_bin" -k 5 "$DOCS_EVIDENCE_TIMEOUT" \
        python3 "${HERE}/docs_review_evidence.py" \
        --repo "$REPO_DIR" --base "$base_sha" --head "$head_ref" 2>>"$LOG_FILE")"
    rc=$?
    set -e
    [ "$rc" = "0" ] || return 1

    # Exit status alone is too weak a contract to hand a PR the relaxed prompt.
    # A helper truncated to `raise SystemExit(0)`, or one whose output was lost,
    # exits clean with nothing to show - and every PR, including one that
    # rewrites app.py, would then be reviewed as documentation on the strength
    # of a classification that never ran.
    #
    # Two lines are required, not one. Command substitution strips the trailing
    # newline, so a helper that emitted the sentinel *and nothing else* would
    # otherwise pass: the first-line test would see the whole string, and the
    # strip that follows would leave it untouched and send it as the evidence.
    local expected
    expected="DOCS-ONLY-EVIDENCE ${base_sha}"$'\n'"Documentation-only diff against base ${base_sha}."
    case "$evidence" in
        "$expected"$'\n'*) : ;;
        *) log "  docs-only classifier exited 0 without a complete block - using the strict prompt"
           return 1 ;;
    esac
    evidence="${evidence#*$'\n'}"

    # The format rule goes after the evidence, not before it: the excerpts run
    # to thousands of lines, and the instruction the reviewer has to obey when
    # it starts writing should be the last thing it read.
    DOCS_ONLY_PROMPT="$(printf '%s\n\n%s\n\n%s\n\n%s\n' "$(review_preamble)" "Every path in this diff is documentation. There is no executable behaviour to
get wrong, so the question is not whether the change works - it is whether it
tells the truth about code that already shipped. The implementation it describes
is in the base commit and is deliberately outside this diff.

Do NOT return BLOCKER because the implementation or its tests are absent from
the diff. That is the expected shape of this PR, not a defect.

\"Unproven\" still blocks. What changes is where the proof is expected: in the
excerpts below rather than in the diff. A claim the excerpts do not *establish*
is as unproven as one they contradict - if the lines shown do not settle what
the sentence asserts, the documentation cited the wrong place, and the fix is a
citation that points at the code which proves it.

Return BLOCKER only for:
  - a statement that contradicts the base excerpts embedded below
  - a statement the excerpts below do not support. \`app.py\` importing
    \`CSRFProtect\` does not establish \"every state-changing endpoint is
    CSRF-protected\"; the excerpt has to show the thing being claimed
  - an UNRESOLVED entry below: the documentation names a line the base commit
    does not have, so it is already wrong
  - a NOT IN BASE entry below that the documentation describes as an existing
    file. A document may legitimately name a file that is generated, ignored or
    still to be written; decide from the text which one this is
  - a REFUSED entry below whose *contents* the documentation asserts something
    specific about. The refusal itself is not a defect - the bot will not paste
    a file that may carry credentials - but a claim about what is inside one
    cannot be checked here, and documentation should not be making it
  - a claim this diff adds about how the code behaves that names no source file
    at all, so nothing in this request can check it
  - a credential, secret or absolute local path the diff adds to a tracked file
  - a removal that deletes a documented security constraint: the app has no
    authentication and is bound to loopback for that reason, and warnings about
    that, about CSRF, rate limits or secrets handling, are where a human learns
    it. Striking one out changes no code and changes who knows
  - a line added OR REMOVED under an AGENT INSTRUCTIONS notice below that
    changes what an autonomous agent may do: granting it new authority, telling
    it to run a command, or deleting a guardrail it had. Deletion counts:
    striking 'never read .env' out of those files widens what the next agent
    run will do exactly as surely as adding a permission. Those files are
    instructions this repository's own bot loads, and unlike a claim about
    behaviour their text is fully visible in this diff
  - an UNREADABLE CONTENT notice below, unless the surrounding documentation
    makes clear the image is a mock-up or a public page. Nobody has seen those
    pixels - not the diff, not this request, not you - so say a person has to
    look. A gate that certifies what it cannot read is worth nothing
  - a TRUNCATED notice below: the evidence did not fit, so ask for a smaller PR

Otherwise return PASS." "$evidence" "$(verdict_format_rule)")"
}

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
        OVERSIZED)
            log "  PR #${pr}: cached OVERSIZED for ${base_sha:0:7}..${head_sha:0:7} - needs a smaller diff or a human"
            return 1 ;;
    esac

    if [ "$DRY_RUN" = "1" ]; then
        log "  PR #${pr}: would request review of ${base_sha}..${head_sha} (dry run; no cached verdict, so would not merge)"
        return 1
    fi

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

    # Measure what would be sent before sending it (issue #182). Past the
    # ceiling `rx` returns no verdict at all, so requesting one is not a review
    # that might fail - it is a review that cannot happen, repeated every tick.
    # Refuse once, record it, and say so on the PR.
    #
    # A cap the operator can set to nonsense is not a cap: refuse rather than
    # guess, and refuse for every PR so the typo is visible immediately instead
    # of only on the large one.
    case "$REVIEW_DIFF_MAX_BYTES" in
        ''|*[!0-9]*|0) log "  AUTOPILOT_REVIEW_DIFF_MAX_BYTES must be a positive integer - refusing"
                       return 1 ;;
    esac

    local diff_bytes
    if ! diff_bytes="$(git diff --no-color "${base_sha}..${ref}" | wc -c | tr -d '[:space:]')"; then
        log "  PR #${pr}: could not measure the diff - skipping"
        return 1
    fi
    if [ "$diff_bytes" -gt "$REVIEW_DIFF_MAX_BYTES" ]; then
        log "  PR #${pr}: diff is ${diff_bytes} bytes, over the ${REVIEW_DIFF_MAX_BYTES} the reviewer survives"
        log "            not requesting a review that would come back UNAVAILABLE - see issue #182"
        record_verdict "$key" OVERSIZED "pr-${pr}"
        post_oversized_comment "$pr" "$base_sha" "$head_sha" "$diff_bytes"
        return 1
    fi

    local prompt
    if docs_only_review_prompt "$base_sha" "$ref"; then
        log "  PR #${pr}: documentation-only diff - reviewing it against the base"
        prompt="$DOCS_ONLY_PROMPT"
    else
        prompt="$(standard_review_prompt)"
    fi

    log "  PR #${pr}: requesting independent review of ${base_sha:0:7}..${head_sha:0:7}"
    set +e
    # Pin the reviewer to Codex instead of inheriting rx's `fallback` chain.
    #
    # The reason this pin was added is gone. `fallback` tries Claude first, and
    # on 2026-08-09 its verdict opened with a Markdown heading (`## PASS`) that
    # the parser discarded: 18 of 18 Claude legs UNAVAILABLE, 14 of them burning
    # the full 120s RX_CLAUDE_TIMEOUT, on top of a Codex review that then
    # succeeded. Nine hours later the parser learned to unwrap `## PASS`,
    # `**PASS**` and `` `PASS` ``, and the rx release this machine runs accepts
    # all three - measured 2026-08-15 by calling `_parse_verdict` in that
    # release directly, not the checkout it was built from.
    #
    # The pin stays because un-pinning cannot be proven right now, and an
    # unproven change to the merge gate is the thing this file exists to
    # prevent. Every Claude leg on this machine since 2026-08-13 has failed in
    # under 4.1s with rc=1 and the same ~835-byte reply: HTTP 429, "You've hit
    # your weekly limit - resets Aug 18 at 3pm". 28 of 28 in
    # ~/.cache/owner-guardrails/reviewer/events.jsonl, reproduced by hand on
    # 2026-08-15 with the same argv the coordinator uses. A leg that answers 429
    # is not a second opinion, and switching it on today would measure the quota
    # rather than the formatting.
    #
    # To drop the pin after the quota resets: run a real review with
    # RX_PROVIDER_POLICY=fallback and read the `provider=claude` rows of that
    # events.jsonl - PASS or BLOCKER, not UNAVAILABLE, twice. Being wrong costs
    # more than it did: RX_CLAUDE_TIMEOUT now defaults to 460s rather than the
    # 120s of the measurement above, so a leg that hangs holds the merge lock
    # nearly four times as long. `verdict_format_rule` above removes the *stated*
    # obstacle for both providers; it does not license removing the pin unmeasured.
    RX_PROVIDER_POLICY=codex-only \
    rx --range "${base_sha}..${ref}" "$prompt" >>"$LOG_FILE" 2>&1
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

# A required context is matched by name. Rename a job in ci.yml and the old
# name stays required and never reports - GitHub blocks every merge on a check
# that will be pending forever, and nothing says why. Cheap to catch here, on
# the way past, rather than from a PR that mysteriously will not merge.
#
# Defined up here with the other helpers, not below its call site: bash resolves
# a function only once its definition has been executed, so the version that sat
# after `main` never ran at all - the call printed "command not found" into the
# log and the warning this exists to give was never given.
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
if ! strict_protection_is_on; then
    log "FATAL: ${BASE_BRANCH} protection does not both require branches to be up to"
    log "       date and apply that to administrators. The bot merges as the owner,"
    log "       so either one missing means GitHub would accept a merge onto a base"
    log "       that moved after the review - which is what this check replaces."
    log "       gh api -X PATCH repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/required_status_checks -f strict=true"
    log "       gh api -X POST repos/${REPO_SLUG}/branches/${BASE_BRANCH}/protection/enforce_admins"
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
