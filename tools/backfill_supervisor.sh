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
#     supervisor starts a second copy of a job that is still alive. That means
#     its EXIT STATUS, not just its output: an independent Tier-2 audit of the
#     first version found that a `docker exec` printing a partial listing and
#     then failing slipped through the empty-output check, and a probe
#     confirmed it started the paid backfill twice while the real one was
#     alive. Nonzero status is "could not tell", whatever came out of it.
#  3. Every restart needs a FRESH snapshot path. The backfills refuse to
#     overwrite a snapshot because it is a rollback point, so reusing the
#     name makes the restart exit instead of run. A clock alone does not give
#     that: two restarts in one second collide, and so does a run a day later
#     at the same HHMMSS. The restart counter is what makes it unique within
#     a run, and the date is what makes it unique between runs.
#  4. "Done" has to mean done *for this run*. The run log is append-only and
#     its default path is the same on every invocation, so grepping the whole
#     file makes the second supervision of a module exit 0 at its first tick
#     without ever calling docker — reporting success for work it never
#     watched. Only the bytes this run appended count.
#  5. One supervisor per module and container. Two of them race: both see an
#     idle container before either start is visible, and two paid backfills
#     then write the same rows. A host-side lock is what prevents it, not the
#     backfills' own snapshot guard, which distinct prefixes walk straight
#     past.

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
NO_SNAPSHOT=0

usage() {
    cat <<'USAGE'
Usage: backfill_supervisor.sh --module utils.backfill_pool --snapshot-prefix data/pool [options]

  --module MOD             python -m module to supervise (required)
  --snapshot-prefix PATH   required: fresh "<PATH>_YYYYmmdd_HHMMSS_N.json" per
                           restart. The backfills take --snapshot as a rollback
                           point and refuse to overwrite one, so a restart
                           without a fresh path exits instead of running.
  --no-snapshot            supervise a module that takes no --snapshot at all.
                           Required instead of --snapshot-prefix, and never a
                           default: silently restarting a snapshot-taking
                           backfill without one burns the whole budget on runs
                           that exit immediately.
  --extra-args "..."       appended to the module invocation and evaluated by a
                           shell INSIDE the container, so it is operator-trusted
                           input like any other argument to `docker exec`
  --container NAME         default idealista-app
  --run-log PATH           where the module's own output goes, RELATIVE to /app
                           (default data/<module-tail>_supervised.log). It is
                           read back on the host through the ./data bind mount,
                           so an absolute path would name two different files
                           and is refused.
  --log PATH               supervisor log (default data/backfill_supervisor.log)
  --max-restarts N         default 12
  --interval S             seconds between checks, default 90
  --max-ticks N            give up watching after N checks, default 400
  --done-pattern TEXT      run-log line that means "finished", default "Done".
                           Only bytes this run appends to the run log are
                           searched, never what an earlier run left there.
USAGE
}

# A non-integer here used to disable the very budget it configures: `[ 0 -ge
# abc ]` fails with "integer expression expected", which reads as false, and
# the supervisor then restarts a paid job on every tick to the end of the
# watch window.
require_int() {
    case "$2" in
        ''|*[!0-9]*) echo "$1 must be a non-negative integer, got: $2" >&2; exit 2 ;;
    esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --module) MODULE="$2"; shift 2 ;;
        --snapshot-prefix) SNAPSHOT_PREFIX="$2"; shift 2 ;;
        --no-snapshot) NO_SNAPSHOT=1; shift ;;
        --extra-args) EXTRA_ARGS="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --run-log) RUN_LOG="$2"; shift 2 ;;
        --log) LOG_FILE="$2"; shift 2 ;;
        --max-restarts) require_int --max-restarts "$2"; MAX_RESTARTS="$2"; shift 2 ;;
        --interval) require_int --interval "$2"; INTERVAL_S="$2"; shift 2 ;;
        --max-ticks) require_int --max-ticks "$2"; MAX_TICKS="$2"; shift 2 ;;
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

if [ -z "$SNAPSHOT_PREFIX" ] && [ "$NO_SNAPSHOT" -eq 0 ]; then
    echo "--snapshot-prefix is required (or --no-snapshot to say the module takes none)" >&2
    echo "without it every restart omits --snapshot and the module exits at once" >&2
    usage >&2
    exit 2
fi

# The environment defaults get the same treatment as the flags; only the flags
# ran through require_int above.
require_int BACKFILL_MAX_RESTARTS "$MAX_RESTARTS"
require_int BACKFILL_INTERVAL_S "$INTERVAL_S"
require_int BACKFILL_MAX_TICKS "$MAX_TICKS"

MODULE_TAIL="${MODULE##*.}"
RUN_LOG="${RUN_LOG:-data/${MODULE_TAIL}_supervised.log}"
LOG_FILE="${LOG_FILE:-data/backfill_supervisor.log}"

# The restart redirects to /app/$RUN_LOG inside the container while finished()
# reads $RUN_LOG here; those are the same file only because ./data is bind
# mounted at /app/data. An absolute path silently names two different files,
# and the host one going stale reads as either "never finishes" or, if an old
# Done sits in it, immediate false success.
case "$RUN_LOG" in
    /*) echo "--run-log must be relative to /app (it is read back on the host through the bind mount), got: $RUN_LOG" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

# Only what THIS run appends counts as its completion; see rule 4 above.
RUN_LOG_START=0
if [ -f "$RUN_LOG" ]; then
    RUN_LOG_START="$(wc -c <"$RUN_LOG" 2>/dev/null | tr -d ' ')"
    case "$RUN_LOG_START" in ''|*[!0-9]*) RUN_LOG_START=0 ;; esac
fi

# `.` is a regex metacharacter and a bare substring match also accepts a longer
# module name, so supervising utils.backfill_pool used to read
# utils.backfill_pool_v2 as "alive" and never restart the job it was watching.
MODULE_RE="$(printf '%s' "$MODULE" | sed 's/[][\.*^$\/+?(){}|]/\\&/g')"

# Returns: 0 running, 1 not running, 2 could not tell.
job_state() {
    local listing rc
    listing="$(docker exec "$CONTAINER" sh -c \
        'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline 2>/dev/null; echo; done' 2>/dev/null)"
    rc=$?
    # Rule 2: a failed inspection is "could not tell" even when it printed
    # something. Reading a partial listing as a complete one is how a second
    # copy of a live paid backfill gets started.
    if [ "$rc" -ne 0 ] || [ -z "$listing" ]; then
        return 2
    fi
    if printf '%s\n' "$listing" | grep -qE -- "(^| )-m ${MODULE_RE}( |\$)"; then
        return 0
    fi
    return 1
}

container_present() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"
}

finished() {
    # The run log lives inside the container's /app, which is bind-mounted
    # here, so the host can read it without another docker exec. It is
    # append-only and its default path is the same on every invocation, so
    # only the bytes appended after this supervisor started are its own
    # evidence of completion (rule 4).
    [ -f "$RUN_LOG" ] || return 1
    tail -c "+$((RUN_LOG_START + 1))" "$RUN_LOG" 2>/dev/null \
        | grep -q -- "$DONE_PATTERN"
}

# One supervisor per module and container (rule 5). mkdir is the atomic part;
# the pid inside lets a genuinely dead predecessor be taken over instead of
# blocking the owner forever.
LOCK_DIR="$(dirname "$LOG_FILE")/.supervisor.${CONTAINER}.${MODULE}.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # mkdir fails for two different reasons and they must not be confused.
    # Only "it already exists" is a lock held by somebody; a missing parent or
    # a permission error means this run holds NO lock, and carrying on there
    # is the same fail-open this tool was just audited for.
    if [ ! -d "$LOCK_DIR" ]; then
        echo "cannot create the supervisor lock at $LOCK_DIR" >&2
        exit 2
    fi
    other="$(cat "$LOCK_DIR/pid" 2>/dev/null)"
    # `kill -0` cannot tell "no such process" from "not yours", so a lock held
    # by another user reads as stale. The supervisor runs as the owner of the
    # container on a single-user host, where that distinction does not arise.
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        echo "another supervisor (pid $other) already watches $MODULE in $CONTAINER" >&2
        log "supervisor: refusing to start - pid $other already watches $MODULE in $CONTAINER"
        exit 2
    fi
    log "supervisor: taking over a stale lock left by pid ${other:-unknown}"
fi
printf '%s\n' "$$" >"$LOCK_DIR/pid" 2>/dev/null || true
trap 'rm -f "$LOCK_DIR/pid" 2>/dev/null; rmdir "$LOCK_DIR" 2>/dev/null' EXIT

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
                    # The restart counter is what makes this unique within a
                    # run (a clock does not: --interval 0 restarts twice in one
                    # second), and the date is what stops a run tomorrow from
                    # landing on a file left today (rule 3).
                    cmd="$cmd --snapshot ${SNAPSHOT_PREFIX}_$(date +%Y%m%d_%H%M%S)_${restarts}.json"
                fi
                [ -n "$EXTRA_ARGS" ] && cmd="$cmd $EXTRA_ARGS"
                log "supervisor: restart $restarts/$MAX_RESTARTS -> $cmd"
                if ! docker exec -d "$CONTAINER" sh -c "$cmd >> /app/$RUN_LOG 2>&1"; then
                    # Without this the budget drains against a start that never
                    # happened and the log says only that a restart was tried.
                    log "supervisor: restart $restarts FAILED to start (docker exec -d exited nonzero)"
                fi
            fi
            ;;
    esac

    sleep "$INTERVAL_S"
done

log "supervisor: watch window ended after $restarts restart(s); $MODULE not finished"
exit 3
