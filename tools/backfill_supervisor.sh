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
  --run-log PATH           where the module's own output goes, under data/
                           (default data/<module-tail>_supervised.log). Only
                           ./data is bind mounted into the container, so any
                           other path names one file on the host and a
                           different one inside, and is refused.
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

# Where this checkout lives, so that neither the run log nor the lock depends
# on the caller's working directory. BACKFILL_ROOT is the test seam.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${BACKFILL_ROOT:-$(dirname "$SCRIPT_DIR")}"

# The restart redirects to /app/$RUN_LOG inside the container while finished()
# reads the same file here. They are one file only under the ./data:/app/data
# bind mount, so the run log has to resolve inside data/ — and three separate
# ways of leaving it have now been found:
#   * an absolute path names two unrelated files;
#   * `--run-log run.log` writes /app/run.log in the container while the host
#     reads ./run.log;
#   * `data/../run.log` passes a prefix test and lands outside data/ anyway.
# Reading it relative to REPO_ROOT rather than the cwd closes the fourth: the
# same default diverges when the tool is invoked from anywhere else.
case "$RUN_LOG" in
    *..*) echo "--run-log must not contain '..', got: $RUN_LOG" >&2; exit 2 ;;
esac
case "$RUN_LOG" in
    data/?*) : ;;
    *) echo "--run-log must be a path under data/ (only ./data is bind mounted into the container), got: $RUN_LOG" >&2; exit 2 ;;
esac
RUN_LOG_HOST="$REPO_ROOT/$RUN_LOG"

if [ -z "$DONE_PATTERN" ]; then
    # grep matches an empty pattern against every line, so the first progress
    # line the module wrote would report the paid job as finished.
    echo "--done-pattern must not be empty" >&2
    exit 2
fi

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

# Only what THIS run appends counts as its completion; see rule 4 above.
RUN_LOG_START=0
if [ -f "$RUN_LOG_HOST" ]; then
    RUN_LOG_START="$(wc -c <"$RUN_LOG_HOST" 2>/dev/null | tr -d ' ')"
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
    # `-m mod`, `-mmod` and `-um mod` are all the same invocation to python, and
    # anchoring on the literal "-m " missed the last two — declaring a live paid
    # backfill idle and starting a second copy of it.
    if printf '%s\n' "$listing" | grep -qE -- "(^| )-[A-Za-z]*m ?${MODULE_RE}( |\$)"; then
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
    [ -f "$RUN_LOG_HOST" ] || return 1
    tail -c "+$((RUN_LOG_START + 1))" "$RUN_LOG_HOST" 2>/dev/null \
        | grep -q -- "$DONE_PATTERN"
}

# One supervisor per module and container (rule 5).
#
# The lock is anchored to the repository, NOT to `dirname $LOG_FILE`: keying it
# to an option meant two supervisors with different --log paths took two
# different locks and both restarted the same paid job.
#
# It is a FILE created under `noclobber`, not a directory, because create and
# claim then happen in one atomic step. The directory version had two races:
# two supervisors could both find one stale lock, both decide it was dead, and
# both fall through before either wrote its pid; and a pid write that failed
# was ignored, leaving a supervisor running with a lock it did not hold.
LOCK_ROOT="$REPO_ROOT/data"
LOCK_FILE="$LOCK_ROOT/.supervisor.${CONTAINER}.${MODULE}.lock"

if ! mkdir -p "$LOCK_ROOT" 2>/dev/null; then
    echo "cannot create the supervisor lock directory $LOCK_ROOT" >&2
    exit 2
fi

# The ONLY way to hold this lock is the atomic create below. A stale lock is
# reported and refused, never taken over automatically.
#
# Taking one over cannot be made atomic, and an audit found the race: two
# supervisors both read one dead pid, A removes it and creates its lock, B then
# performs the removal it had already decided on — deleting A's fresh lock —
# and creates its own. Both then believe they hold it and both restart the paid
# job. Any read-then-remove has that shape, and macOS has no flock(1) to
# replace it with. Refusing costs one manual `rm` when a supervisor is killed;
# losing the race costs a duplicate paid backfill.
acquire_lock() {
    local other
    if (set -o noclobber; printf '%s\n' "$$" >"$LOCK_FILE") 2>/dev/null; then
        return 0
    fi
    other="$(cat "$LOCK_FILE" 2>/dev/null)"
    # `kill -0` cannot tell "no such process" from "not yours", so a lock held
    # by another user reads as dead. The supervisor runs as the owner of the
    # container on a single-user host, where that does not arise.
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        echo "another supervisor (pid $other) already watches $MODULE in $CONTAINER" >&2
    else
        echo "a supervisor lock is present at $LOCK_FILE (pid ${other:-unknown}, not running)." >&2
        echo "It is not taken over automatically: that cannot be done atomically, and losing the race starts a second paid backfill. Remove the file by hand once you are sure no supervisor is running." >&2
    fi
    return 1
}

if ! acquire_lock; then
    log "supervisor: refusing to start - could not take the lock for $MODULE in $CONTAINER"
    exit 2
fi
trap '[ "$(cat "$LOCK_FILE" 2>/dev/null)" = "$$" ] && rm -f "$LOCK_FILE"' EXIT

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
                    # The restart counter makes this unique within a run (a
                    # clock does not: --interval 0 restarts twice in one
                    # second), the pid makes it unique between runs that share
                    # a second and a counter, and the date stops a run tomorrow
                    # from landing on a file left today (rule 3).
                    cmd="$cmd --snapshot ${SNAPSHOT_PREFIX}_$(date +%Y%m%d_%H%M%S)_$$_${restarts}.json"
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
