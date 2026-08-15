#!/bin/bash
# Keep a long backfill alive across deploys.
#
# Why this exists (2026-08-14, issue #283): every `docker compose up -d
# --build` recreates the app container and kills whatever runs inside it. A
# Phase-2 pool backfill was interrupted **four times** in one afternoon by
# other sessions' merges; each interruption cost one property (the tools
# commit per row and their scope drops finished rows), but each one also
# needed a human to notice and restart. This supervisor is that human.
#
# It deliberately does NOT hold the deploy. Deferring a deploy for a
# resumable job is the worse trade — a deploy that never lands is a failure
# too — which is why #283 asks the watcher to *name* what it killed and
# leaves restarting to something like this. `utils/inflight.py` marks a run
# as resumable; this script is what acts on that fact.
#
#   tools/backfill_supervisor.sh --module utils.backfill_pool \
#       --snapshot-prefix data/pool_backfill
#
# Run it on the HOST that owns the container (the mini), not inside the
# container: a supervisor living in the thing it supervises dies with it.
#
# Three rules learned the hard way, each pinned by tools/backfill_supervisor_test.sh:
#
#  1. The restart budget counts RESTARTS, not loop iterations. The first
#     version burned its budget on quiet checks and gave up after an hour of
#     perfectly healthy running.
#  2. A container it cannot read is NOT an idle container. `docker exec`
#     failing during a rebuild must not look like "no job running", or the
#     supervisor starts a second copy of a job that is still alive.
#  3. Every restart needs a FRESH snapshot path. The backfills refuse to
#     overwrite a snapshot because it is a rollback point, so reusing the
#     name makes the restart exit instead of run.

set -uo pipefail

# docker is not on the PATH of a non-interactive ssh session on macOS; the
# deploy-watcher plist documents the same trap for launchd. Only *fall back*
# to the usual install locations: prepending them unconditionally overrode a
# deliberately-placed docker earlier in PATH, which made the tool untestable
# and — measured on 2026-08-14 — sent a test's stub calls to the real daemon.
if ! command -v docker >/dev/null 2>&1; then
    export PATH="${PATH}:/opt/homebrew/bin:/usr/local/bin"
fi

MODULE=""
SNAPSHOT_PREFIX=""
CONTAINER="${BACKFILL_CONTAINER:-idealista-app}"
MAX_RESTARTS="${BACKFILL_MAX_RESTARTS:-12}"
INTERVAL_S="${BACKFILL_INTERVAL_S:-90}"
MAX_TICKS="${BACKFILL_MAX_TICKS:-400}"
LOG_FILE=""
RUN_LOG=""
DONE_PATTERN="${BACKFILL_DONE_PATTERN:-Done}"
EXTRA_ARGS=""

usage() {
    cat <<'USAGE'
Usage: backfill_supervisor.sh --module utils.backfill_pool [options]

  --module MOD            python -m module to supervise (required)
  --snapshot-prefix PATH   fresh "<PATH>_HHMMSS.json" per restart
  --extra-args "..."       appended verbatim to the module invocation
  --container NAME         default idealista-app
  --run-log PATH           where the module's own output goes
                           (default data/<module-tail>_supervised.log)
  --log PATH               supervisor log (default data/backfill_supervisor.log)
  --max-restarts N         default 12
  --interval S             seconds between checks, default 90
  --max-ticks N            give up watching after N checks, default 400
  --done-pattern TEXT      run-log line that means "finished", default "Done"
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --module) MODULE="$2"; shift 2 ;;
        --snapshot-prefix) SNAPSHOT_PREFIX="$2"; shift 2 ;;
        --extra-args) EXTRA_ARGS="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --run-log) RUN_LOG="$2"; shift 2 ;;
        --log) LOG_FILE="$2"; shift 2 ;;
        --max-restarts) MAX_RESTARTS="$2"; shift 2 ;;
        --interval) INTERVAL_S="$2"; shift 2 ;;
        --max-ticks) MAX_TICKS="$2"; shift 2 ;;
        --done-pattern) DONE_PATTERN="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$MODULE" ]; then
    echo "--module is required" >&2
    usage >&2
    exit 2
fi

MODULE_TAIL="${MODULE##*.}"
RUN_LOG="${RUN_LOG:-data/${MODULE_TAIL}_supervised.log}"
LOG_FILE="${LOG_FILE:-data/backfill_supervisor.log}"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

# Returns: 0 running, 1 not running, 2 could not tell.
job_state() {
    local listing
    listing="$(docker exec "$CONTAINER" sh -c \
        'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline 2>/dev/null; echo; done' 2>/dev/null)"
    if [ -z "$listing" ]; then
        return 2
    fi
    if printf '%s\n' "$listing" | grep -q -- "$MODULE"; then
        return 0
    fi
    return 1
}

container_present() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"
}

finished() {
    # The run log lives inside the container's /app, which is bind-mounted
    # here, so the host can read it without another docker exec.
    [ -f "$RUN_LOG" ] && grep -q -- "$DONE_PATTERN" "$RUN_LOG"
}

restarts=0
log "supervisor: watching $MODULE in $CONTAINER (max $MAX_RESTARTS restarts)"

for _tick in $(seq 1 "$MAX_TICKS"); do
    if finished; then
        log "supervisor: $MODULE reported done after $restarts restart(s)"
        exit 0
    fi

    job_state
    case $? in
        0) : ;;  # running, nothing to do
        2) log "supervisor: could not read $CONTAINER - not assuming it is idle" ;;
        1)
            if ! container_present; then
                log "supervisor: $CONTAINER absent (deploy in progress), waiting"
            elif [ "$restarts" -ge "$MAX_RESTARTS" ]; then
                log "supervisor: $MAX_RESTARTS restarts used and $MODULE is still dying - stopping for a human"
                exit 1
            else
                restarts=$((restarts + 1))
                cmd="python -m $MODULE"
                if [ -n "$SNAPSHOT_PREFIX" ]; then
                    cmd="$cmd --snapshot ${SNAPSHOT_PREFIX}_$(date +%H%M%S).json"
                fi
                [ -n "$EXTRA_ARGS" ] && cmd="$cmd $EXTRA_ARGS"
                log "supervisor: restart $restarts/$MAX_RESTARTS -> $cmd"
                docker exec -d "$CONTAINER" sh -c "$cmd >> /app/$RUN_LOG 2>&1"
            fi
            ;;
    esac

    sleep "$INTERVAL_S"
done

log "supervisor: watch window ended after $restarts restart(s); $MODULE not finished"
exit 3
