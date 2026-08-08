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

REPO_DIR="${AUTOPILOT_REPO_DIR:-/Users/ss/IdealistaRank}"
BRANCH="${AUTOPILOT_BRANCH:-main}"
COMPOSE_FILE="${AUTOPILOT_COMPOSE_FILE:-docker-compose.yml}"
IMAGE="${AUTOPILOT_IMAGE:-idealistarank-app}"
ROLLBACK_TAG="${IMAGE}:autopilot-rollback"
HEALTH_URL="${AUTOPILOT_HEALTH_URL:-http://127.0.0.1:5001/api/healthz}"
LOCK_DIR="${AUTOPILOT_LOCK_DIR:-/tmp/idealista-autopilot-deploy.lock.d}"
LOG_FILE="${AUTOPILOT_LOG_FILE:-${REPO_DIR}/data/autopilot-deploy.log}"

# The app has to answer healthz within this window after a rebuild.
HEALTH_TIMEOUT_SECONDS="${AUTOPILOT_HEALTH_TIMEOUT:-180}"
HEALTH_POLL_SECONDS=5

log() {
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

die() {
    log "FATAL: $*"
    exit 1
}

mkdir -p "$(dirname "$LOG_FILE")"

# --- single instance -------------------------------------------------------
# A build takes minutes; the timer fires more often than that.
# shellcheck source=lib/lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/lock.sh"
if ! autopilot_acquire_lock "$LOCK_DIR"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S')  another deploy is in progress, skipping" >>"$LOG_FILE"
    exit 0
fi
cd "$REPO_DIR" || die "repo not found: $REPO_DIR"

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

if [ "$local_sha" = "$remote_sha" ]; then
    # Nothing new. Stay quiet: this runs every few minutes and the log is read
    # by a human.
    exit 0
fi

log "new ${BRANCH}: ${local_sha:0:7} -> ${remote_sha:0:7}"
git --no-pager log --oneline "${local_sha}..${remote_sha}" | while read -r line; do
    log "    $line"
done

# --- checkpoint for rollback ----------------------------------------------
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker tag "$IMAGE" "$ROLLBACK_TAG"
    log "tagged current image as ${ROLLBACK_TAG}"
    have_rollback_image=1
else
    log "WARNING: no existing '${IMAGE}' image to tag; rollback will be code-only"
    have_rollback_image=0
fi

rollback() {
    local reason="$1"
    log "ROLLBACK (${reason}): returning to ${local_sha:0:7}"

    # Every step here runs on the failure path, where `set -e` aborting
    # mid-rollback would leave the app down with no further attempt. Each
    # command is therefore guarded and reported rather than allowed to exit.
    git reset --hard "$local_sha" >/dev/null 2>&1 \
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
    if [ "$restored" = "0" ]; then
        log "  falling back to a rebuild from ${local_sha:0:7}"
        docker compose -f "$COMPOSE_FILE" up -d --build >>"$LOG_FILE" 2>&1 \
            || log "  rollback rebuild failed"
    fi

    if check_health; then
        log "rollback healthy - previous version is serving again"
    else
        log "ROLLBACK IS ALSO UNHEALTHY - THE APP IS DOWN, MANUAL ATTENTION NEEDED"
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
                log "health OK: $body"
                return 0
            fi
            log "health not ready: $body"
        fi
        sleep "$HEALTH_POLL_SECONDS"
    done
    log "health check timed out after ${HEALTH_TIMEOUT_SECONDS}s"
    return 1
}

# --- deploy ----------------------------------------------------------------
if ! git merge --ff-only "origin/${BRANCH}" >>"$LOG_FILE" 2>&1; then
    die "fast-forward to origin/${BRANCH} failed - local ${BRANCH} has diverged"
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
log "DEPLOYED ${deployed_sha:0:7} successfully"

# Scheduler state is worth a line in the log: a 'not_initialized' scheduler is
# the difference between an app that ingests and one that only looks alive
# (#14).
sched="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null | sed -n 's/.*"scheduler":"\([^"]*\)".*/\1/p')"
if [ "$sched" != "running" ]; then
    log "WARNING: scheduler reports '${sched:-unknown}' - ingestion will not run"
fi
