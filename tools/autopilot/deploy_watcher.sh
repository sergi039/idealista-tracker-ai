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
INFLIGHT_PATTERN="${AUTOPILOT_INFLIGHT_PATTERN:-python.*(-m +utils\.|utils/)}"
DEFER_ON_INFLIGHT="${AUTOPILOT_DEFER_ON_INFLIGHT:-0}"
DEFER_BUDGET="${AUTOPILOT_DEFER_BUDGET:-6}"
DEFER_STATE="${AUTOPILOT_DEFER_STATE:-${REPO_DIR}/data/.deploy_deferrals}"

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
# So keep the leaves: drop any match that is the parent of another match. A
# job with no wrapper has no matched child and survives untouched. (A job that
# deliberately spawned a second `utils` module would lose its parent line;
# nothing in this repository does that, and the leaf is the process doing the
# work either way.)
#
# Output: one "<pid>\t<command>" line per matching job.
inflight_processes() {
    docker top "$APP_CONTAINER" 2>/dev/null \
        | awk 'NR > 1 { pid = $2; ppid = $3; $1=$2=$3=$4=$5=$6=$7=""; sub(/^ +/, ""); print pid "\t" ppid "\t" $0 }' \
        | grep -E "$INFLIGHT_PATTERN" \
        | awk -F'\t' '
            { pid[NR] = $1; ppid[NR] = $2; cmd[NR] = $3; n = NR }
            END {
                for (i = 1; i <= n; i++) is_parent[ppid[i]] = 1
                for (i = 1; i <= n; i++)
                    if (!(pid[i] in is_parent)) print pid[i] "\t" cmd[i]
            }' || true
}

# The marker a job wrote for itself, if it wrote one. Prints
# "<resumable>\t<ledger>"; an absent, unreadable or mismatched marker prints
# "unknown\t". Unknown and false have to behave identically downstream - a
# deploy cannot tell them apart, and guessing "resumable" is how work gets
# lost silently.
#
# The marker is keyed by PID, and a PID left behind by a killed run can be
# reused by an unrelated job in the container that replaced it. So the
# marker's own module has to appear in the command line before its claim is
# believed - otherwise a stale `resumable: true` could vouch for a job that
# is nothing of the sort.
inflight_marker() {
    local pid="$1" command="$2"
    python3 - "$INFLIGHT_DIR" "$pid" "$command" <<'PY' 2>/dev/null || printf 'unknown\t\n'
import glob
import json
import os
import sys

directory, pid, command = sys.argv[1], sys.argv[2], sys.argv[3]
for path in sorted(glob.glob(os.path.join(directory, f"*.{pid}.json"))):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        continue
    if not isinstance(data, dict):
        continue
    module = data.get("module")
    if not isinstance(module, str) or module not in command:
        continue
    resumable = "true" if data.get("resumable") is True else "false"
    print(f"{resumable}\t{data.get('ledger') or ''}")
    break
else:
    print("unknown\t")
PY
}

# Logs every job in flight and sets `inflight_count` / `inflight_unsafe`.
# `inflight_unsafe` counts the ones that did not claim to be resumable: those
# are the jobs a deferral exists for.
inflight_count=0
inflight_unsafe=0
survey_inflight() {
    inflight_count=0
    inflight_unsafe=0

    if [ -z "$(docker ps --filter "name=^/${APP_CONTAINER}$" --format '{{.Names}}' 2>/dev/null)" ]; then
        # Nothing is running, so nothing can be killed. Not an error: the very
        # first deploy on a machine starts the container.
        return 0
    fi

    local procs line pid command marker resumable ledger
    procs="$(inflight_processes)"
    [ -n "$procs" ] || return 0
    log "long-running work is in flight inside ${APP_CONTAINER}:"

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        pid="${line%%$'\t'*}"
        command="${line#*$'\t'}"
        marker="$(inflight_marker "$pid" "$command")"
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
    local listing file tmp
    listing="$(git ls-tree -r --name-only "$remote_sha" -- "${SELF_PATHS[@]}" 2>>"$LOG_FILE")" \
        || die "cannot list ${SELF_PATHS[*]} at ${remote_sha:0:7} - refusing to hand over blind"
    tmp="$(mktemp "${TMPDIR:-/tmp}/deploy-watcher-parse.XXXXXX")" \
        || die "cannot write a temporary file to syntax-check ${remote_sha:0:7}"
    while IFS= read -r file; do
        case "$file" in
            '' | *.sh) ;;
            *) continue ;;
        esac
        [ -n "$file" ] || continue
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
if [ "$inflight_count" != "0" ]; then
    if [ "$DEFER_ON_INFLIGHT" != "1" ]; then
        log "  deploying anyway (AUTOPILOT_DEFER_ON_INFLIGHT is off); the ${inflight_count} job(s) above will be killed"
    elif [ "$inflight_unsafe" = "0" ]; then
        log "  every job above reports itself resumable; deploying and killing them"
    else
        deferrals="$(read_deferrals "$remote_sha")"
        if [ "$deferrals" -ge "$DEFER_BUDGET" ]; then
            log "  deferral budget exhausted (${deferrals}/${DEFER_BUDGET} ticks waited for ${remote_sha:0:7})"
            log "  deploying and killing the ${inflight_unsafe} job(s) that did not claim to be resumable"
        else
            deferrals=$((deferrals + 1))
            if ! write_deferrals "$remote_sha" "$deferrals"; then
                # Cannot count, so cannot bound. Deferring on a budget that
                # cannot be counted is the unbounded wait this design refuses
                # to have.
                log "  ALERT: could not record the deferral in ${DEFER_STATE}"
                log "  an unbounded wait is worse than a killed job - deploying now"
            else
                log "  deferring this tick (${deferrals}/${DEFER_BUDGET}) - ${inflight_unsafe} job(s) would lose work"
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
