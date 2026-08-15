#!/bin/bash
# Deploy whatever landed on main, and roll back if the result is not healthy.
#
# GitHub Actions cannot reach this Mac (no self-hosted runner), so continuous
# deployment lives here: a LaunchAgent runs this on a timer, it notices a new
# main, rebuilds, and verifies. Nothing is deployed that does not answer
# /api/healthz afterwards.
#
# Deliberately conservative:
#   - a lock file means two ticks never build at once
#   - the previous image is tagged before the build, so rollback is a retag
#   - a failed health check rolls the code AND the image back, then reports
#
# Usage: deploy_watcher.sh [--once]   (--once is the default; the LaunchAgent
# supplies the schedule, this script does not loop)

set -euo pipefail

# Where this script really is, and what it was called with. Both are needed to
# hand over to the version of itself that a fast-forward brings in (#293), and
# the path has to be resolved before anything can rewrite the file.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_PATH="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_ARGS=("$@")

REPO_DIR="${AUTOPILOT_REPO_DIR:-/Users/ss/IdealistaRank}"
BRANCH="${AUTOPILOT_BRANCH:-main}"
COMPOSE_FILE="${AUTOPILOT_COMPOSE_FILE:-docker-compose.yml}"
IMAGE="${AUTOPILOT_IMAGE:-idealistarank-app}"
ROLLBACK_TAG="${IMAGE}:autopilot-rollback"
HEALTH_URL="${AUTOPILOT_HEALTH_URL:-http://127.0.0.1:5001/api/healthz}"
LOCK_DIR="${AUTOPILOT_LOCK_DIR:-/tmp/idealista-autopilot-deploy.lock.d}"
LOG_FILE="${AUTOPILOT_LOG_FILE:-${REPO_DIR}/data/autopilot-deploy.log}"
# What is actually running, as opposed to what is checked out. Comparing
# local git against remote git only answers "is the checkout current", and the
# container can be older than the checkout - a `git pull` by hand is enough to
# make the watcher believe there is nothing to do while the app still serves
# the previous build. Observed on the very first run.
DEPLOYED_MARKER="${AUTOPILOT_DEPLOYED_MARKER:-${REPO_DIR}/data/.deployed_sha}"

# The app has to answer healthz within this window after a rebuild.
HEALTH_TIMEOUT_SECONDS="${AUTOPILOT_HEALTH_TIMEOUT:-180}"
HEALTH_POLL_SECONDS=5

# healthz renders no template, so it cannot see a broken one: on 2026-08-14 a
# TemplateSyntaxError turned every /properties/<id> into a redirect for 15
# minutes while healthz stayed green. A page that renders is the other half of
# "is this build serving", so one real page must answer 200 - not a redirect,
# which is exactly what that defect produced.
#
# Which page, and what counts as rendered, live in lib/render_check.sh: one
# contract, read here and by .githooks/post-merge, because this rule used to be
# written down twice under two names that had to move together (#292). Set
# DEPLOY_RENDER_PATH="" to skip the check and be told the page is unverified.
# The origin is this watcher's own: the health URL's, so a test harness
# pointing healthz at a stub points the page check at the same stub. The
# contract is loaded further down, once log() and die() exist to report a
# failure to load into the deploy log rather than into launchd's stderr file.
AUTOPILOT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
RENDER_LIB="${AUTOPILOT_LIB_DIR}/render_check.sh"

# --- long-running work inside the container (#283) --------------------------
# `docker compose up -d --build` recreates the app container, which kills
# whatever is running in it. Observed twice on 2026-08-14: a pool backfill died
# mid-flight and nothing anywhere said so. The watcher deliberately does not
# decide whose work matters, so the default is unchanged - deploy, but name
# what is being killed. AUTOPILOT_DEFER_ON_INFLIGHT=1 buys a bounded wait
# instead, and the bound is the point: a deploy that never lands is also a
# failure, just a quieter one.
APP_CONTAINER="${AUTOPILOT_APP_CONTAINER:-${COMPOSE_CONTAINER_PREFIX:-idealista}-app}"
INFLIGHT_DIR="${AUTOPILOT_INFLIGHT_DIR:-${REPO_DIR}/data/.inflight}"
# Which container processes count as a job. Both spellings the repo uses.
# This pattern is a deliberately generous PRE-FILTER, and it must stay one.
# Three spellings of the same command defeated three successive attempts to be
# precise here: `-m utils.x`, `-mutils.x` (python takes the argument joined),
# and `-um utils.x` (a cluster - `-u` is what a logged background job is
# usually started with). Each time, a job the pattern did not match was not
# reported as `unknown`; it was not reported at all, and the deploy killed it
# in silence. That asymmetry decides the design: an extra process named here
# costs a bounded deferral and a log line, while a missing one costs work
# nobody knows was lost. So match any python whose command mentions `utils.`
# or `utils/` at all, and let the marker join below be the precise layer.
INFLIGHT_PATTERN="${AUTOPILOT_INFLIGHT_PATTERN:-python.*utils[./]}"
DEFER_ON_INFLIGHT="${AUTOPILOT_DEFER_ON_INFLIGHT:-0}"
DEFER_BUDGET="${AUTOPILOT_DEFER_BUDGET:-6}"
DEFER_STATE="${AUTOPILOT_DEFER_STATE:-${REPO_DIR}/data/.deploy_deferrals}"
# What a truthful process list for THIS container must contain. The Dockerfile
# CMD runs gunicorn, so a table without it is not an idle container - it is a
# probe that answered without looking. Shape alone is too weak: eight columns
# and a numeric PID can be satisfied by junk. Set empty to fall back to the
# shape check alone.
CONTAINER_SENTINEL="${AUTOPILOT_CONTAINER_SENTINEL-gunicorn}"

# --- this watcher deploys its own source (#293) -----------------------------
# The tick that rolled out #285 on 2026-08-14 16:33:30 ran the *pre*-#285
# script: bash had read this file before the tick's own `git merge --ff-only`
# replaced it, so that deploy had neither the in-flight survey nor the page
# check it was shipping, and it killed a pool backfill at 32 ledger rows
# silently. A watcher that deploys its own source has to hand over to the
# version it is deploying, before it deploys it.
#
# Set AUTOPILOT_SELF_UPDATE=0 to keep the old behaviour; the tick then says
# loudly that it deployed a watcher it did not run.
SELF_UPDATE="${AUTOPILOT_SELF_UPDATE:-1}"
# The interpreter that will run the new watcher, and therefore the only one
# entitled to vet it. A bare `bash` is not it: the LaunchAgent execs /bin/bash
# (3.2.57 on this Mac) while handing the job a PATH that starts with
# /opt/homebrew/bin, where bash is 5.x. Measured, `cmd &>>file`, `;;&`, `|&`
# and `coproc` all pass `bash -n` under 5.x and are syntax errors under 3.2 -
# and `>>"$LOG_FILE" 2>&1` is written a dozen times in this file, so `&>>` is
# one keystroke away. Lines 153-163 harden git and docker against exactly this
# first-match-on-PATH trap; bash deserves the same. It is a floor, not a
# guarantee: `declare -A` parses under 3.2 and fails at runtime.
SELF_INTERPRETER="${BASH:-/bin/bash}"
# One handover per tick. main moving again mid-tick is legitimate but has to
# terminate, and the next tick is five minutes away.
REEXEC_MAX="${AUTOPILOT_REEXEC_MAX:-1}"

# State carried across the handover. Read once, then unset: these must not leak
# into docker, git, curl or python3, and an operator's stale export must not
# look like a handover that never happened.
REEXEC_DEPTH="${AUTOPILOT_REEXEC_DEPTH:-0}"
case "$REEXEC_DEPTH" in
    '' | *[!0-9]*) REEXEC_DEPTH=0 ;;
esac
HANDOVER_ROLLBACK_SHA="${AUTOPILOT_ROLLBACK_SHA:-}"
HANDOVER_LOCK="${AUTOPILOT_LOCK_INHERITED:-}"
unset AUTOPILOT_REEXEC_DEPTH AUTOPILOT_ROLLBACK_SHA AUTOPILOT_LOCK_INHERITED

log() {
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

die() {
    log "FATAL: $*"
    exit 1
}

mkdir -p "$(dirname "$LOG_FILE")"

# --- the page-check contract -----------------------------------------------
# Present is not loaded, and loaded is not complete: a half-written file parses
# into nothing, and a page check that cannot run must never read as one that
# passed. `set -e` would already abort on a source that fails, but silently,
# into launchd's stderr file - so say it in the deploy log instead, and require
# the functions themselves rather than the file.
# shellcheck source=lib/render_check.sh
if [ ! -r "$RENDER_LIB" ] || ! source "$RENDER_LIB"; then
    die "${RENDER_LIB} is missing or did not load - the page check cannot run"
fi
for contract_fn in deploy_render_origin deploy_render_url deploy_render_ok \
    deploy_render_legacy_vars; do
    command -v "$contract_fn" >/dev/null 2>&1 \
        || die "${RENDER_LIB} defined no ${contract_fn}() - the page check cannot run"
done
PAGE_URL="$(deploy_render_url "$(deploy_render_origin "$HEALTH_URL")")"

# --- single instance -------------------------------------------------------
# A build takes minutes; the timer fires more often than that.
# shellcheck source=lib/lock.sh
source "${AUTOPILOT_LIB_DIR}/lock.sh"
# A handover (#293) replaces the program, not the process, and bash sets no
# close-on-exec on fd 9 - so the descriptor, and the flock(2) on it, are still
# ours. Measured on this Mac: after `exec`, /dev/fd/9 is still open and an
# unrelated process is still denied the lock.
#
# Taking it again would *work* - `exec 9>file` closes the old description
# before it locks the new one, which was measured too - and that is exactly the
# problem: it drops the lock for the length of a fork and exec, during which
# another tick can take it and start a second concurrent build. A watcher whose
# whole point is that two ticks never build at once should not open that window
# once per self-update.
#
# The lock path has to match as well as the descriptor be open, so a stray
# export in somebody's shell cannot talk this script out of locking; if either
# check fails it acquires normally, which is the fail-safe direction.
if [ -n "$HANDOVER_LOCK" ] && [ "$HANDOVER_LOCK" = "$LOCK_DIR" ] \
    && [ -e "/dev/fd/${AUTOPILOT_LOCK_FD}" ]; then
    log "still holding the deploy lock handed over with this tick"
elif ! autopilot_acquire_lock "$LOCK_DIR"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S')  another deploy is in progress, skipping" >>"$LOG_FILE"
    exit 0
fi
cd "$REPO_DIR" || die "repo not found: $REPO_DIR"

# Is this script part of the repository it deploys? On the mini it is; in the
# shell tests the real script runs against a throwaway repo that does not
# contain it, and then there is nothing to hand over to. `pwd -P` on both sides
# so a symlinked path does not read as a different tree.
REPO_PHYS="$(pwd -P)"
SELF_REL_DIR=""
case "$SCRIPT_DIR" in
    "$REPO_PHYS"/*) SELF_REL_DIR="${SCRIPT_DIR#"$REPO_PHYS"/}" ;;
esac
# The files this process actually executes: itself, and the library it sources.
SELF_PATHS=()
SELF_REL_SCRIPT=""
if [ -n "$SELF_REL_DIR" ]; then
    SELF_REL_SCRIPT="${SELF_REL_DIR}/$(basename "$SCRIPT_PATH")"
    SELF_PATHS=("$SELF_REL_SCRIPT" "${SELF_REL_DIR}/lib")
fi

# --- the tools have to be the right ones, not merely present ---------------
# launchd resolves the first match on PATH, and this Mac carries a Homebrew
# git 2.13 from 2017 in /usr/local/bin. It predates extensions.worktreeConfig,
# so once a worktree set that flag it refused the repository outright - every
# tick died on the first git call, into launchd's stderr file that nobody
# reads, while the log below stayed silent and main quietly drifted ahead of
# what was serving. Prove both tools work here rather than discovering it
# halfway through a build.
command -v docker >/dev/null 2>&1 || die "docker not on PATH (${PATH})"

# A malformed AUTOPILOT_INFLIGHT_PATTERN makes `grep -E` exit 2 on every call,
# which the survey's pipeline would turn into "no jobs are running" - a gate
# that always passes because it is broken. Prove the pattern compiles once,
# here, where the failure is loud.
printf '' | grep -E "$INFLIGHT_PATTERN" >/dev/null 2>&1 || [ $? -eq 1 ] \
    || die "AUTOPILOT_INFLIGHT_PATTERN is not a valid extended regex: ${INFLIGHT_PATTERN}"
git rev-parse --git-dir >/dev/null 2>&1 \
    || die "$(command -v git) ($(git --version 2>&1 | head -1)) cannot read ${REPO_DIR} - PATH is ${PATH}"

# --- is there anything to deploy? ------------------------------------------
current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "$BRANCH" ]; then
    log "on branch '$current_branch', not '$BRANCH' - refusing to deploy someone's working tree"
    exit 0
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    log "working tree is dirty - refusing to deploy over uncommitted work"
    exit 0
fi

git fetch --quiet origin "$BRANCH" || die "git fetch failed"

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/${BRANCH}")"
deployed_sha="$(cat "$DEPLOYED_MARKER" 2>/dev/null || true)"

# Where a rollback goes: the commit that is *serving*. Normally that is the
# checkout before the fast-forward, but a handover (#293) merged before this
# process started, so its HEAD is already the commit under test and the serving
# commit has to be carried in.
ROLLBACK_SHA="$local_sha"
if [ -n "$HANDOVER_ROLLBACK_SHA" ]; then
    if git rev-parse --verify --quiet "${HANDOVER_ROLLBACK_SHA}^{commit}" >/dev/null; then
        ROLLBACK_SHA="$HANDOVER_ROLLBACK_SHA"
    else
        log "WARNING: AUTOPILOT_ROLLBACK_SHA=${HANDOVER_ROLLBACK_SHA} is not a commit here"
        log "  a rollback would return to ${local_sha:0:7} instead"
    fi
elif [ -n "$deployed_sha" ] && [ "$deployed_sha" != "$local_sha" ]; then
    # The handover above carries the serving commit for the length of one tick,
    # and one tick is not always enough: a tick that hands over and then
    # *defers* to an in-flight job ends without deploying, leaving the checkout
    # on the commit under test while the container still runs the previous one.
    # The next tick is a fresh process with nothing handed to it, so `local_sha`
    # is the commit that has not deployed yet - and rolling back to it would
    # "return" to the very build being rolled back, then rebuild it from the
    # tree if no saved image was available.
    #
    # The marker answers this, and it answers it across processes because it is
    # on disk: it is written only after a build passed health, so it names what
    # is serving. This also covers the plain case of a checkout someone moved
    # ahead of the container by hand.
    if git rev-parse --verify --quiet "${deployed_sha}^{commit}" >/dev/null; then
        ROLLBACK_SHA="$deployed_sha"
    else
        log "WARNING: the deployment marker names ${deployed_sha:0:7}, which is not a commit here"
        log "  a rollback would return to ${local_sha:0:7} instead"
    fi
fi

# Two separate questions: is the checkout current, and is the container built
# from it. Answering only the first is what let a hand-run `git pull` convince
# the watcher that a stale container was up to date.
if [ "$local_sha" = "$remote_sha" ] && [ "$remote_sha" = "$deployed_sha" ]; then
    # Nothing new. Stay quiet: this runs every few minutes and the log is read
    # by a human - except after a handover, where silence would read as the
    # tick having disappeared.
    if [ "$REEXEC_DEPTH" != "0" ]; then
        log "handed-over watcher found ${remote_sha:0:7} already deployed - nothing to build"
    fi
    exit 0
fi

if [ "$local_sha" != "$remote_sha" ]; then
    log "new ${BRANCH}: ${local_sha:0:7} -> ${remote_sha:0:7}"
    git --no-pager log --oneline "${local_sha}..${remote_sha}" | while read -r line; do
        log "    $line"
    done
elif [ -z "$deployed_sha" ]; then
    # First run after installing the watcher, or the marker was removed. The
    # running image cannot be identified, so redeploy rather than assume.
    log "no deployment marker - redeploying ${remote_sha:0:7} to establish one"
else
    log "checkout is current (${remote_sha:0:7}) but the deployed build is ${deployed_sha:0:7}"
fi

# The pre-#292 names are not read any more. Ignoring one can only make the page
# check stricter, never weaker, but an operator who set one deliberately has to
# be told rather than quietly overruled. Said here rather than at the top, so a
# tick with nothing to deploy stays silent.
while read -r legacy_var; do
    [ -n "$legacy_var" ] || continue
    log "NOTE: ${legacy_var} is set but no longer read - the page check is DEPLOY_RENDER_PATH now (#292)"
done <<EOF
$(deploy_render_legacy_vars)
EOF

# --- checkpoint for rollback ----------------------------------------------
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker tag "$IMAGE" "$ROLLBACK_TAG"
    log "tagged current image as ${ROLLBACK_TAG}"
    have_rollback_image=1
else
    log "WARNING: no existing '${IMAGE}' image to tag; rollback will be code-only"
    have_rollback_image=0
fi

# Written only after health passes, so the marker always names a build that
# actually served traffic. Atomic rename: a half-written marker would be read
# as a SHA that never existed.
record_deployed() {
    local sha="$1" tmp
    tmp="$(mktemp "${DEPLOYED_MARKER}.XXXXXX")" || return 1
    printf '%s\n' "$sha" >"$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$DEPLOYED_MARKER" || { rm -f "$tmp"; return 1; }
    return 0
}

# Removing the marker is the fallback whenever the truth cannot be recorded,
# so its own failure cannot be ignored: an unwritable directory fails the write
# *and* the delete, leaving the previous commit's marker in place while a
# different one serves. The next tick still redeploys (marker != HEAD), so the
# app is never stale - but it will redeploy on every tick until someone fixes
# the permissions, and that deserves to be shouted rather than hidden.
clear_marker() {
    local reason="$1"
    if rm -f "$DEPLOYED_MARKER" 2>/dev/null && [ ! -e "$DEPLOYED_MARKER" ]; then
        log "  cleared the deployment marker (${reason})"
        return 0
    fi
    log "  ALERT: could not clear the deployment marker (${reason})."
    log "  ${DEPLOYED_MARKER} still names an older commit; expect a redeploy every"
    log "  tick until the file is writable again."
    return 1
}

rollback() {
    local reason="$1"
    log "ROLLBACK (${reason}): returning to ${ROLLBACK_SHA:0:7}"

    # Every step here runs on the failure path, where `set -e` aborting
    # mid-rollback would leave the app down with no further attempt. Each
    # command is therefore guarded and reported rather than allowed to exit.
    git reset --hard "$ROLLBACK_SHA" >/dev/null 2>&1 \
        || log "  git reset failed - the tree may not match the image"

    local restored=0
    if [ "$have_rollback_image" = "1" ] && docker image inspect "$ROLLBACK_TAG" >/dev/null 2>&1; then
        if docker tag "$ROLLBACK_TAG" "$IMAGE" 2>>"$LOG_FILE"; then
            if docker compose -f "$COMPOSE_FILE" up -d --no-build >>"$LOG_FILE" 2>&1; then
                restored=1
            else
                log "  rollback 'compose up' from the saved image failed"
            fi
        else
            log "  could not retag ${ROLLBACK_TAG} - saved image is unusable"
        fi
    else
        log "  no saved image available for rollback"
    fi

    # Rebuilding from the restored source is slower and can itself fail, but it
    # is the only remaining way back up. Try it whenever the image path did not
    # already restore service.
    local rebuilt=0
    if [ "$restored" = "0" ]; then
        log "  falling back to a rebuild from ${local_sha:0:7}"
        if docker compose -f "$COMPOSE_FILE" up -d --build >>"$LOG_FILE" 2>&1; then
            rebuilt=1
        else
            log "  rollback rebuild failed"
        fi
    fi

    if ! check_health; then
        log "ROLLBACK IS ALSO UNHEALTHY - THE APP IS DOWN, MANUAL ATTENTION NEEDED"
        clear_marker "the rollback itself is unhealthy"
        return
    fi
    log "rollback healthy - previous version is serving again"

    # The marker must name the commit that is *serving*, and only the rebuild
    # path knows that: it built from ROLLBACK_SHA and then passed health.
    #
    # The saved image carries no such guarantee. With no marker to start from,
    # that image can predate the checkout entirely - built from B while the tree
    # sits at A. Writing local_sha then claims A is deployed while B serves, and
    # the next tick sees local=remote=marker and skips the redeploy forever.
    # No marker is the honest answer: one wasted rebuild beats a permanently
    # wrong belief about what is running.
    if [ "$restored" = "1" ]; then
        clear_marker "restored from the saved image, whose commit is unknown"
        return
    fi

    # A rebuild that failed leaves the previous container running, and a
    # container that was never stopped answers healthz perfectly well - so
    # "healthy" here does not mean ROLLBACK_SHA is serving. Recording it anyway
    # is how the marker came to claim e926de6 while the container still ran
    # the build before it, and the watcher then skipped every later tick
    # because marker == HEAD. Observed on 2026-08-08.
    if [ "$rebuilt" = "0" ]; then
        clear_marker "the rollback rebuild failed; the running build is unknown"
        return
    fi

    if ! record_deployed "$ROLLBACK_SHA"; then
        clear_marker "the rollback rebuilt ${ROLLBACK_SHA:0:7} but the marker could not be written"
    fi
}

check_health() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    local body
    while [ $SECONDS -lt $deadline ]; do
        body="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null || true)"
        if [ -n "$body" ]; then
            # `ok` is false when the DB ping fails; the endpoint 503s then and
            # curl -f already swallowed it, but check the body too in case the
            # contract loosens.
            if printf '%s' "$body" | grep -q '"ok":true'; then
                if [ -z "$PAGE_URL" ]; then
                    log "health OK: $body"
                    log "  (DEPLOY_RENDER_PATH is empty - no page was rendered, so a broken template would pass)"
                    return 0
                fi
                if deploy_render_ok "$PAGE_URL"; then
                    log "health OK: $body"
                    log "  ${PAGE_URL} rendered (200)"
                    return 0
                fi
                log "healthz is green but ${PAGE_URL} answered ${DEPLOY_RENDER_STATUS:-no response}"
            else
                log "health not ready: $body"
            fi
        fi
        sleep "$HEALTH_POLL_SECONDS"
    done
    log "health check timed out after ${HEALTH_TIMEOUT_SECONDS}s"
    return 1
}

# --- what is running inside the container ----------------------------------
# `docker top` reads the container's process list from the host, so it needs
# nothing installed in the image and covers jobs that know nothing about
# markers - including anything started by hand with `docker exec`. It is
# authoritative about liveness; the marker files are authoritative about
# whether killing a job loses work. Both are needed.
#
# One job is one line, even when it arrives as a process tree. Measured on the
# mini 2026-08-14, a backfill started as `sh -c 'python -m utils.X ... >> log'`
# shows up twice - the shell and the python it exec'd never replaced:
#
#   34107  sh -c python -m utils.backfill_pool --snapshot data/x.json >> ...
#   34113  python -m utils.backfill_pool --snapshot data/x.json
#
# Reporting that as "the 2 job(s) above will be killed" is wrong in the way
# this whole survey exists to prevent, and it is worse than cosmetic: the
# marker records the *python* process's PID (`os.getpid()`), so the wrapper
# finds no marker and reports `unknown` while its own child reports
# `resumable`. One job, two contradictory verdicts, and with deferring on the
# wrapper alone is enough to hold a deploy for a job that was safe to kill.
#
# So drop the wrapper - but ONLY a wrapper. The rule is not "any match that is
# the parent of another match": `utils.coordinator` legitimately spawning
# `utils.worker` would lose the coordinator, and if the coordinator were the
# non-resumable half its disappearance would take `inflight_unsafe` to zero
# and let the deploy run straight over it. So a parent is dropped only when
# its own command is a shell `-c` invocation, which is what an `sh -c 'python
# -m utils.X ... >> log'` launch actually looks like. Two genuine `utils`
# processes in a parent/child relationship stay two jobs.
#
# Output: one "<pid>\t<command>" line per matching job. Returns non-zero when
# the process list could not be read at all - see the fail-open note below.
inflight_processes() {
    local raw
    # Failure and emptiness are different answers and must not collapse. A
    # `docker top` that exits non-zero with no stdout used to become an empty
    # pipeline through `|| true`, which reads as "no jobs are running" and
    # deploys in silence - the exact defect this survey exists to prevent,
    # reproduced inside the survey itself.
    raw="$(docker top "$APP_CONTAINER" 2>/dev/null)" || return 1
    # It must contain the process this container cannot be without. Shape is
    # satisfiable by junk; the app's own server is not.
    if [ -n "$CONTAINER_SENTINEL" ]; then
        printf '%s\n' "$raw" | awk 'NR > 1' | grep -qF -- "$CONTAINER_SENTINEL" || return 1
    fi

    # The layout is read off the header, never assumed. `docker top` renders
    # whatever ps format it is handed, and the previous parse *checked* one
    # shape (eight fields, numeric PID) while *assuming* a stricter one (the
    # command starting at field 8, fields 1-7 blanked positionally). A table
    # can satisfy the check and still be split wrongly by the parse - and a
    # mis-split command matches the pattern no more, so the job it described
    # disappears from the survey instead of being reported. Check and parse
    # now describe the same table because both come from the same header.
    local rows
    rows="$(printf '%s\n' "$raw" | awk '
        NR == 1 {
            for (i = 1; i <= NF; i++) {
                if ($i == "PID") pid_col = i
                else if ($i == "PPID") ppid_col = i
                else if ($i == "CMD" || $i == "COMMAND") cmd_col = i
            }
            # No header, or one naming no command column, is not a process
            # list this code can read. Guessing a layout is how a job goes
            # missing quietly.
            if (!pid_col || !cmd_col) exit 1
            next
        }
        # A row the header cannot describe is unreadable, and unreadable is
        # not empty: ONE such row makes the whole table unknown rather than
        # silently dropping the process it was describing.
        NF < cmd_col || $pid_col !~ /^[0-9]+$/ { exit 1 }
        {
            cmd = $cmd_col
            for (i = cmd_col + 1; i <= NF; i++) cmd = cmd " " $i
            print $pid_col "\t" (ppid_col ? $ppid_col : "") "\t" cmd
            seen = 1
        }
        # A header with no data rows is not an idle container: this one always
        # runs the app server, so an empty table means the probe did not work.
        END { if (!seen) exit 1 }
    ')" || return 1

    printf '%s\n' "$rows" \
        | grep -E "$INFLIGHT_PATTERN" \
        | awk -F'\t' '
            # A shell running `-c`, however it spells it: `sh -c`, `sh -cx`,
            # `/bin/bash --login -c`, `bash -o pipefail -c`.
            #
            # Every token is scanned, deliberately, rather than stopping at
            # the first non-option: an option that takes an operand puts a
            # bare word in the middle of the option run (`-o pipefail`,
            # `--rcfile /x`), and a scan that halts there misses the `-c`
            # behind it. Knowing where the options end means knowing an
            # optstring per shell, and a parser covering `-o` but not
            # `--rcfile` would read as complete while still being wrong.
            #
            # Scanning wide can only mistake a shell whose *operand* happens
            # to be `-c` for a wrapper - and this is asked only of a shell
            # that already has a matched `utils` child, where dropping it is
            # right anyway: that shell launched the job, killing it kills the
            # job, and the child is the row carrying the marker.
            function is_shell_wrapper(c,   parts, n, i, base) {
                n = split(c, parts, /[ \t]+/)
                if (n < 2) return 0
                base = parts[1]
                sub(/^.*\//, "", base)
                if (base !~ /^(sh|bash|dash|ash|zsh)$/) return 0
                for (i = 2; i <= n; i++)
                    if (parts[i] ~ /^-[A-Za-z]*c[A-Za-z]*$/) return 1
                return 0
            }
            { pid[NR] = $1; ppid[NR] = $2; cmd[NR] = $3; n = NR }
            END {
                for (i = 1; i <= n; i++) has_matched_child[ppid[i]] = 1
                for (i = 1; i <= n; i++)
                    if (!((pid[i] in has_matched_child) && is_shell_wrapper(cmd[i])))
                        print pid[i] "\t" cmd[i]
            }' || true
    return 0
}

# The marker a job wrote for itself, if it wrote one. Prints
# "<resumable>\t<ledger>"; an absent, unreadable or mismatched marker prints
# "unknown\t". Unknown and false have to behave identically downstream - a
# deploy cannot tell them apart, and guessing "resumable" is how work gets
# lost silently.
#
# **The join is the command line, never the PID.** The two sides do not share
# a PID namespace: `utils/inflight.py` records `os.getpid()` from inside the
# container, `docker top` reports the host/VM view. Measured on the mini
# 2026-08-14: the marker said `"pid": 41` while `docker top` reported 21974
# for the same process, so a PID-keyed lookup matched nothing, every job read
# as `unknown`, and the whole `resumable` half of #283 was dead in production
# - eleven "no marker" lines in the deploy log before anyone noticed.
#
# So a marker vouches for a process when its `module` and *every* one of its
# recorded `argv` tokens appear in that process's command line. That also
# separates two concurrent runs of the same module, because their `--snapshot`
# paths differ, and it keeps a stale marker from a killed run from vouching
# for anything still alive under a different argv.
#
# If several markers match and disagree about `resumable`, the answer is
# `unknown`: an ambiguous claim must not be resolved in the deploy's favour.
inflight_marker() {
    local command="$1"
    python3 - "$INFLIGHT_DIR" "$command" <<'PY' 2>/dev/null || printf 'unknown\t\n'
import glob
import json
import os
import sys

directory, command = sys.argv[1], sys.argv[2]
tokens = command.split()


def _program(tokens):
    """What these tokens actually run: ("module"|"script", name, args).

    Position matters. Scanning for the module *anywhere* let a snapshot path
    impersonate the program - `--snapshot data/utils.backfill_pool.json`
    satisfied a `backfill_pool` marker, and so did
    `--snapshot data/utils/backfill_pool.py`. The program is whichever comes
    first: the token after `-m`, or the first `.py` token (arguments follow
    the script, never precede it). Everything after it is the argv.

    Short options are read the way python reads them, not matched as a
    literal `-m`. Three spellings of one command have already defeated three
    attempts to anchor on a form: `-m utils.x`, `-mutils.x`, and `-um utils.x`
    - the last is a cluster, and `-u` is what a background job writing to a
    log is usually started with. Anchoring closes the example it was given and
    leaves the class open, so this walks the cluster instead: on `m`, the
    module is the rest of the token if there is one and the next token
    otherwise; `c` means python is running a command string, so there is no
    module to find; `W`, `X` and `Q` swallow their own operand and cannot be
    read as options themselves.
    """
    takes_operand = "cmWXQ"
    i, n = 1, len(tokens)  # tokens[0] is the interpreter itself
    while i < n:
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if tok == "-" or not tok.startswith("-"):
            # `-` means the program is read from stdin, and the first token
            # that is not an option ends option parsing and IS the program -
            # with or without a `.py`. Treating only `.py` as a script let
            # `python worker -m utils.x` read as running utils.x, so a marker
            # for utils.x vouched for a process that is not it.
            break
        if tok.startswith("--"):
            i += 1
            continue
        rest = tok[1:]
        j = 0
        while j < len(rest):
            ch = rest[j]
            if ch not in takes_operand:
                j += 1
                continue
            operand = rest[j + 1 :]
            if ch == "c":
                return None  # `python -c '...'` runs a command, never a module
            if ch == "m":
                if operand:
                    return ("module", operand, list(tokens[i + 1 :]))
                if i + 1 < n:
                    return ("module", tokens[i + 1], list(tokens[i + 2 :]))
                return None
            # -W/-X/-Q take an operand too, joined or as the next token. The
            # separate form has to be stepped over or it reads as the script:
            # `python -X pycache_prefix=/tmp/utils/x.py -m utils.y` ran y, not
            # the path in the -X operand.
            if not operand:
                i += 1
            break
        i += 1
    if i < n and tokens[i] != "-":
        return ("script", tokens[i], list(tokens[i + 1 :]))
    return None


def _render(argv):
    """The arguments as a process table would show them.

    `docker top` returns one whitespace-joined line, so a tab or a run of
    spaces inside an argument survives in the marker and not in the table.
    Comparing the raw strings therefore made a job miss its own marker and
    spend the deferral budget as `unknown`. Both sides are normalised the
    same way instead, which is the only comparison the process list supports.
    """
    return " ".join(" ".join(a.split()) for a in argv)


def _runs_module(program, module):
    kind, name, _args = program
    if kind == "module":
        return name == f"utils.{module}"
    parts = name.split("/")
    return len(parts) >= 2 and parts[-1] == f"{module}.py" and parts[-2] == "utils"


program = _program(tokens)
matches = []
for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        continue
    if not isinstance(data, dict):
        continue
    module = data.get("module")
    if not isinstance(module, str) or not module:
        continue
    if program is None or not _runs_module(program, module):
        continue
    # The argv must be the SAME argv, in order - not a set of tokens that
    # happen to occur. Membership let a marker recording `data/a` vouch for a
    # live `data/aaa.json`, let a reordered argv match, and worst of all made
    # an EMPTY argv vacuously true: a stale `bulk_ai_analysis` marker with no
    # args (resumable, because no --force) then vouched for a live
    # `--force` run, which is precisely the run that is not resumable.
    #
    # Compared as the rendered string, not as token lists, because `docker
    # top` returns one whitespace-joined line and the shell's quoting is
    # already gone by then. A job launched with `--snapshot 'data/My Pool.json'`
    # arrives as four tokens against the marker's two, so a list comparison
    # missed the live job's own marker and reported the run it was looking at
    # as unknown. Joining both sides asks the only question the process list
    # can actually answer - "is this the same command line" - and keeps order
    # and exactness, which is what membership threw away.
    # A marker that does not describe an argv describes nothing, and must be
    # REJECTED rather than normalised. Coercing a missing or malformed `argv`
    # to `[]` gave it the identity of a job with no arguments, so a corrupt
    # marker claiming `resumable: true` vouched for a live no-argument job -
    # inventing a claim out of damaged data, which is the opposite of what
    # every other guard in this reader does.
    argv = data.get("argv")
    if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
        continue
    # An argument that is empty, or nothing but whitespace, cannot be told
    # apart from *no argument* once rendered - `[""]` and `[]` both render to
    # "". Rather than let that ambiguity resolve in the deploy's favour, such
    # a marker matches nothing and the job reads as unknown.
    if any(not a.split() for a in argv):
        continue
    if _render(argv) != _render(program[2]):
        continue
    matches.append(data)

if not matches:
    print("unknown\t")
else:
    verdicts = {m.get("resumable") is True for m in matches}
    if len(verdicts) > 1:
        print("unknown\t")
    else:
        resumable = "true" if verdicts.pop() else "false"
        ledger = next((m.get("ledger") for m in matches if m.get("ledger")), "")
        print(f"{resumable}\t{ledger}")
PY
}

# Logs every job in flight and sets `inflight_count` / `inflight_unsafe`.
# `inflight_unsafe` counts the ones that did not claim to be resumable: those
# are the jobs a deferral exists for.
inflight_count=0
inflight_unsafe=0
# Set when the process list could not be read. "I do not know" is a third
# answer, distinct from "nothing is running", and it has to block exactly like
# a job that did not claim to be resumable - otherwise a broken probe is a
# free pass.
inflight_unknown=0
survey_inflight() {
    inflight_count=0
    inflight_unsafe=0
    inflight_unknown=0

    local running
    if ! running="$(docker ps --filter "name=^/${APP_CONTAINER}$" --format '{{.Names}}' 2>/dev/null)"; then
        inflight_unknown=1
        log "WARNING: could not ask docker whether ${APP_CONTAINER} is running."
        log "  work may be in flight; this tick cannot tell."
        return 0
    fi
    if [ -z "$running" ]; then
        # Nothing is running, so nothing can be killed. Not an error: the very
        # first deploy on a machine starts the container.
        return 0
    fi

    local procs line command marker resumable ledger
    if ! procs="$(inflight_processes)"; then
        inflight_unknown=1
        log "WARNING: 'docker top ${APP_CONTAINER}' gave no readable process list."
        log "  that is UNKNOWN, not empty - a long job may be running right now."
        return 0
    fi
    [ -n "$procs" ] || return 0
    log "long-running work is in flight inside ${APP_CONTAINER}:"

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        # The PID column is still emitted so the process list stays readable
        # in a debug run, but nothing joins on it any more - see above.
        command="${line#*$'\t'}"
        marker="$(inflight_marker "$command")"
        resumable="${marker%%$'\t'*}"
        ledger="${marker#*$'\t'}"

        inflight_count=$((inflight_count + 1))
        case "$resumable" in
            true)
                log "  in flight (resumable): ${command}"
                if [ -n "$ledger" ]; then
                    log "      ledger: ${ledger}"
                fi
                ;;
            false)
                inflight_unsafe=$((inflight_unsafe + 1))
                log "  in flight (NOT resumable - a restart repeats work already done): ${command}"
                if [ -n "$ledger" ]; then
                    log "      ledger: ${ledger}"
                fi
                ;;
            *)
                inflight_unsafe=$((inflight_unsafe + 1))
                log "  in flight (no marker, so resumability is unknown): ${command}"
                ;;
        esac
    done <<<"$procs"

    return 0
}

# Deferrals are counted per target commit: a new commit is a new decision, and
# a budget that carried over would expire against work it never waited for.
read_deferrals() {
    local sha="$1" recorded_sha recorded_count
    read -r recorded_sha recorded_count <"$DEFER_STATE" 2>/dev/null || true
    # Digits or nothing. This file lives in data/ where a human can edit it,
    # and every later use is arithmetic: a non-numeric count would abort the
    # watcher rather than merely be ignored.
    case "${recorded_count:-}" in
        '' | *[!0-9]*) printf '0'; return 0 ;;
    esac
    if [ "${recorded_sha:-}" = "$sha" ]; then
        printf '%s' "$recorded_count"
    else
        printf '0'
    fi
}

write_deferrals() {
    local sha="$1" count="$2" tmp
    tmp="$(mktemp "${DEFER_STATE}.XXXXXX")" || return 1
    printf '%s %s\n' "$sha" "$count" >"$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$DEFER_STATE" || { rm -f "$tmp"; return 1; }
}

# --- hand over to the watcher this deploy brings (#293) --------------------
# The tick that rolled out #285 executed the pre-#285 script and therefore
# deployed the in-flight survey and the page check without running either -
# killing a pool backfill at 32 ledger rows silently.
#
# Why the fast-forward alone cannot fix it, measured on this Mac: `git merge`
# writes a file by creating a new one and renaming over it, so the inode
# changes (654567352 -> 654567361 in the experiment). The shell's open
# descriptor still points at the old, now-unlinked inode, and it keeps reading
# the *previous* script to the end of the tick - reliably, not intermittently.
# That is a mercy, because a rewrite that kept the inode does corrupt the run:
# the same script overwritten in place with `cat >` resumed at the old byte
# offset inside the new bytes and executed a comment fragment. But it also
# means no amount of care after the merge can make this tick run new code.
#
# The only handover is a new process. So: when origin/main changes this script
# or the library it sources, fast-forward and `exec` *before* deciding
# anything, and let the new version survey, defer, build and verify.
#
# Deliberately placed before the in-flight survey rather than after: the survey
# is exactly the kind of thing a new watcher changes (#290 rewrote it), so
# running the old one first and then repeating it under the new one would log
# two contradictory answers to the same question.
self_update_and_reexec() {
    [ "$local_sha" != "$remote_sha" ] || return 0
    [ -n "$SELF_REL_DIR" ] || return 0

    local rc=0
    git diff --quiet "$local_sha" "$remote_sha" -- "${SELF_PATHS[@]}" || rc=$?
    case "$rc" in
        0) return 0 ;;
        1) ;;
        *)
            log "could not compare ${SELF_PATHS[*]} across ${local_sha:0:7}..${remote_sha:0:7}"
            log "  assuming this watcher changes, which is the safe way to be wrong"
            ;;
    esac

    log "${remote_sha:0:7} changes this watcher itself (${SELF_PATHS[*]})"

    if ! git cat-file -e "${remote_sha}:${SELF_REL_SCRIPT}" 2>/dev/null; then
        log "  ALERT: ${remote_sha:0:7} removes ${SELF_REL_SCRIPT} - there is nothing to hand over to"
        log "  this tick deploys that removal while running the script it removes"
        return 0
    fi

    if [ "$SELF_UPDATE" != "1" ]; then
        log "  ALERT: AUTOPILOT_SELF_UPDATE is off - this tick deploys the new watcher while running the old one"
        log "  whatever ${remote_sha:0:7} changes about deploying does not apply until the next tick"
        return 0
    fi

    if [ "$REEXEC_DEPTH" -ge "$REEXEC_MAX" ]; then
        # Deploying anyway is the whole defect this ticket exists to remove:
        # the deploy would be governed by the watcher from local_sha while
        # putting remote_sha's watcher on disk. The budget bounds handovers per
        # tick, and the honest way to respect it is to stop, not to fall back to
        # the behaviour being fixed.
        #
        # Stopping costs one tick and nothing else. The checkout already holds
        # the watcher this process is running, so the next tick starts from it
        # and hands over to remote_sha normally - and that handover deploys
        # itself, which is the point. Nothing has been merged for remote_sha,
        # the container and the marker are untouched, and the previous build
        # keeps serving.
        log "  ALERT: already handed over ${REEXEC_DEPTH}x this tick and ${BRANCH} moved again"
        log "  refusing to deploy ${remote_sha:0:7} under the watcher from ${local_sha:0:7}"
        log "  the next tick runs ${local_sha:0:7}'s watcher and hands over to ${remote_sha:0:7} then"
        exit 0
    fi

    # A watcher that does not parse cannot be handed over to, and deploying it
    # would kill the deploy chain at the *next* tick instead of this one, with
    # the checkout already advanced. Checked before the fast-forward, where
    # refusing costs nothing: checkout, container and marker are all untouched
    # and the previous build keeps serving.
    local listing entry mode kind file tmp
    # With modes, not just names: a syntax check reads the *blob*, and a
    # symlink's blob is its target path - one word, which parses as a valid
    # command and tells the gate nothing about what `exec` would actually run.
    # A dangling one would pass, be merged, and kill every later tick with the
    # checkout already advanced. Only a regular file can be the script this
    # process execs, so anything else is refused here, where refusing is free.
    listing="$(git ls-tree -r "$remote_sha" -- "${SELF_PATHS[@]}" 2>>"$LOG_FILE")" \
        || die "cannot list ${SELF_PATHS[*]} at ${remote_sha:0:7} - refusing to hand over blind"
    tmp="$(mktemp "${TMPDIR:-/tmp}/deploy-watcher-parse.XXXXXX")" \
        || die "cannot write a temporary file to syntax-check ${remote_sha:0:7}"
    while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        # "<mode> <type> <sha>\t<path>"
        mode="${entry%% *}"
        kind="${entry#* }"
        kind="${kind%% *}"
        file="${entry#*$'\t'}"
        case "$file" in
            *.sh) ;;
            *) continue ;;
        esac
        if [ "$kind" != "blob" ] || { [ "$mode" != "100644" ] && [ "$mode" != "100755" ]; }; then
            rm -f "$tmp"
            die "${file} at ${remote_sha:0:7} is a ${kind} with mode ${mode}, not a regular file - refusing to hand over to something that is not a script (nothing merged; ${deployed_sha:0:7} keeps serving)"
        fi
        if ! git show "${remote_sha}:${file}" >"$tmp" 2>>"$LOG_FILE"; then
            rm -f "$tmp"
            die "cannot read ${file} at ${remote_sha:0:7} - refusing to hand over blind"
        fi
        if ! "$SELF_INTERPRETER" -n "$tmp" 2>>"$LOG_FILE"; then
            rm -f "$tmp"
            die "${file} does not parse at ${remote_sha:0:7} - refusing to deploy a watcher that cannot run (nothing merged; ${deployed_sha:0:7} keeps serving)"
        fi
    done <<EOF
${listing}
EOF
    rm -f "$tmp"

    # The commit that was vetted, not the ref that named it. Several sessions
    # and a human fetch into this same clone, so origin/main can advance
    # between `git rev-parse` above and here - and the seconds in between hold
    # a `docker image inspect`, a `docker tag` and this whole syntax gate. A
    # ref here would fast-forward to, and then `exec`, a watcher nothing
    # checked. The next tick deploys the newer commit five minutes later.
    if ! git merge --ff-only "$remote_sha" >>"$LOG_FILE" 2>&1; then
        die "fast-forward to ${remote_sha:0:7} (origin/${BRANCH}) failed - local ${BRANCH} has diverged"
    fi
    log "  fast-forwarded to ${remote_sha:0:7}; handing this tick over to its ${SELF_REL_SCRIPT}"

    # The lock rides across on fd 9 (see the acquire above). ROLLBACK_SHA is the
    # commit that is *serving*: after the merge HEAD is the commit under test,
    # so the new process cannot work it out for itself.
    export AUTOPILOT_LOCK_INHERITED="$LOCK_DIR"
    export AUTOPILOT_REEXEC_DEPTH="$((REEXEC_DEPTH + 1))"
    export AUTOPILOT_ROLLBACK_SHA="$ROLLBACK_SHA"
    exec "$SELF_INTERPRETER" "$SCRIPT_PATH" ${SCRIPT_ARGS[@]+"${SCRIPT_ARGS[@]}"}
    die "could not re-execute ${SCRIPT_PATH}"
}

self_update_and_reexec

# --- who is about to be killed ---------------------------------------------
# Runs before the fast-forward, so a deferred tick leaves the checkout and the
# deployment marker exactly as they were and the next tick decides afresh. The
# rollback image was already re-tagged above; that is idempotent and costs a
# deferred tick nothing. (After a handover the fast-forward has already
# happened, in the tick that handed over - so there the checkout is one commit
# ahead of the container while a deferral waits. The marker still names what is
# serving, which is the part anything downstream reads.)
survey_inflight
# An unreadable process list blocks exactly like a job with no marker: both
# mean "this tick cannot say that killing costs nothing".
blocking=$((inflight_unsafe + inflight_unknown))
if [ "$inflight_count" != "0" ] || [ "$inflight_unknown" != "0" ]; then
    if [ "$DEFER_ON_INFLIGHT" != "1" ]; then
        # The two branches are genuinely exclusive: an unreadable list returns
        # from the survey before a single job is counted. Reporting the count
        # anyway would print "the 0 job(s) above will be killed" for the one
        # case where the count is not an observation but the failed probe's
        # residue - a deploy claiming it killed nothing precisely when it
        # cannot know what it killed.
        if [ "$inflight_unknown" != "0" ]; then
            log "  deploying anyway (AUTOPILOT_DEFER_ON_INFLIGHT is off); what this kills is UNKNOWN - the process list could not be read"
        else
            log "  deploying anyway (AUTOPILOT_DEFER_ON_INFLIGHT is off); the ${inflight_count} job(s) above will be killed"
        fi
    elif [ "$blocking" = "0" ]; then
        log "  every job above reports itself resumable; deploying and killing them"
    else
        deferrals="$(read_deferrals "$remote_sha")"
        if [ "$deferrals" -ge "$DEFER_BUDGET" ]; then
            log "  deferral budget exhausted (${deferrals}/${DEFER_BUDGET} ticks waited for ${remote_sha:0:7})"
            log "  deploying and killing the ${blocking} job(s)/unknown(s) that did not claim to be resumable"
        else
            deferrals=$((deferrals + 1))
            if ! write_deferrals "$remote_sha" "$deferrals"; then
                # Cannot count, so cannot bound. Deferring on a budget that
                # cannot be counted is the unbounded wait this design refuses
                # to have.
                log "  ALERT: could not record the deferral in ${DEFER_STATE}"
                log "  an unbounded wait is worse than a killed job - deploying now"
            else
                log "  deferring this tick (${deferrals}/${DEFER_BUDGET}) - ${blocking} job(s)/unknown(s) may lose work"
                log "  set AUTOPILOT_DEFER_ON_INFLIGHT=0 to deploy immediately instead"
                exit 0
            fi
        fi
    fi
fi
# Every path that reaches here is deploying, so the count has served its
# purpose - whatever it was waiting for is about to be killed or was never
# there.
rm -f "$DEFER_STATE" 2>/dev/null || true

# --- deploy ----------------------------------------------------------------
# The vetted commit, not the ref - same reason as the handover merge above, and
# the same commit the survey and the deferral budget were decided against. A
# no-op after a handover: that tick fast-forwarded before re-executing.
if ! git merge --ff-only "$remote_sha" >>"$LOG_FILE" 2>&1; then
    die "fast-forward to ${remote_sha:0:7} (origin/${BRANCH}) failed - local ${BRANCH} has diverged"
fi

log "building..."
if ! docker compose -f "$COMPOSE_FILE" up -d --build >>"$LOG_FILE" 2>&1; then
    rollback "build or start failed"
    exit 1
fi

if ! check_health; then
    rollback "unhealthy after deploy"
    exit 1
fi

deployed_sha="$(git rev-parse HEAD)"
if record_deployed "$deployed_sha"; then
    log "DEPLOYED ${deployed_sha:0:7} successfully"
else
    # The deploy worked; only the bookkeeping failed. Leave no marker rather
    # than a wrong one - the next tick then redeploys this same commit, which
    # is wasteful but never wrong.
    log "DEPLOYED ${deployed_sha:0:7} successfully, but the marker could not be written"
    clear_marker "the deploy succeeded but its marker could not be written"
fi

# Scheduler state is worth a line in the log: a 'not_initialized' scheduler is
# the difference between an app that ingests and one that only looks alive
# (#14).
sched="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null | sed -n 's/.*"scheduler":"\([^"]*\)".*/\1/p')"
if [ "$sched" != "running" ]; then
    log "WARNING: scheduler reports '${sched:-unknown}' - ingestion will not run"
fi
