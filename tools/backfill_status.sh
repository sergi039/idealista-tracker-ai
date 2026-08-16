#!/bin/bash
# "May I start a backfill right now?" - the question `docker top` cannot answer.
#
# On 2026-08-16 a session asked the documented question, got the correct
# answer, and started a duplicate paid run anyway (#338). The sequence:
#
#   09:01:02  the deploy killed a running utils.backfill_pool
#   09:01:58  tools/backfill_supervisor.sh ticked
#   09:01:59  ...and restarted it in the rebuilt container
#   09:03:51  a second session, having seen an empty process list, started its
#             own run of the same module
#
# 57 seconds in which the container genuinely held no job, bounded by the
# supervisor's next tick. Nothing was wrong with the check: `docker top` is
# authoritative about the instant it runs. **A liveness check is not a claim
# about the next minute**, and every deploy manufactures one of these windows
# by killing what the supervisor will refill.
#
# Three sources each know a different part of the answer, and no one of them
# is enough:
#
#   docker top          a process running NOW. Blind to a respawn one tick
#                       away; also shows an orphan nobody is waiting on, since
#                       an interrupted `docker exec` leaves its python behind.
#   supervisor lock     that a respawn is EXPECTED, per (container, module).
#                       The only thing in the system that knows the future.
#                       Blind to a hand-run nobody supervises.
#   data/.inflight/     that a run STARTED and did not clean up. A report, not
#                       a lock - true of a live job and of a corpse alike.
#
# So this reads all three and answers in three states, never two:
#
#   0  idle     nothing running, nothing expected. Safe to start.
#   1  busy     a run is in progress or a respawn is expected. Do not start.
#   2  unknown  a source could not be read. NOT the same as idle, and treated
#               exactly like busy - the whole family of defects this tool
#               exists for begins with a failed probe reading as a negative.
#
# Deliberately advisory: it answers, it does not enforce. The runner cannot
# enforce this without a lock of its own, and a marker must not be pressed
# into that role (see utils/inflight.py).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CONTAINER="${BACKFILL_STATUS_CONTAINER:-${COMPOSE_CONTAINER_PREFIX:-idealista}-app}"
MODULE=""
QUIET=0

usage() {
    cat >&2 <<'USAGE'
usage: backfill_status.sh [--module utils.backfill_pool] [--container NAME] [--quiet]

Answers whether a utils backfill may be started right now.
  --module     ask about one module. Without it, every module is considered:
               any running job or any supervisor lock makes the answer busy.
  --container  default: ${COMPOSE_CONTAINER_PREFIX:-idealista}-app
  --quiet      print only the verdict word

exit 0 idle, 1 busy, 2 unknown (unknown blocks, exactly like busy)
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --module) [ $# -ge 2 ] || { usage; exit 2; }; MODULE="$2"; shift 2 ;;
        --container) [ $# -ge 2 ] || { usage; exit 2; }; CONTAINER="$2"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }

# The module as python would accept it, in every spelling: `-m mod`, `-mmod`
# and the `-um mod` cluster. This is the supervisor's own expression, reused
# rather than re-derived - two tools disagreeing about which process is which
# module is how the same paid job gets started twice.
module_regex() {
    local m escaped
    m="$1"
    escaped="$(printf '%s' "$m" | sed 's/[][\.*^$\/+?(){}|]/\\&/g')"
    printf '(^| )-[A-Za-z]*m ?%s( |$)' "$escaped"
}

verdict=idle          # idle | busy | unknown
reasons=()

note_busy() { verdict=busy; reasons+=("$1"); }
note_unknown() { [ "$verdict" = "busy" ] || verdict=unknown; reasons+=("$1"); }

# --- 1. what is running now -------------------------------------------------
# `docker top` needs nothing installed in the image and reads the host's view,
# so it also sees a job someone started by hand with `docker exec`.
if ! raw="$(docker top "$CONTAINER" 2>/dev/null)"; then
    if [ -z "$(docker ps --filter "name=^/${CONTAINER}$" --format '{{.Names}}' 2>/dev/null)" ]; then
        # No container at all is a real state, and during a deploy it is the
        # normal one - but it is also the moment a supervisor is waiting to
        # refill it, so the lock below still decides.
        say "container ${CONTAINER}: not running"
    else
        note_unknown "docker top ${CONTAINER} could not be read - a job may be running right now"
    fi
else
    # A table that is only a header is a failed probe, not an idle container:
    # this image always runs gunicorn.
    rows="$(printf '%s\n' "$raw" | awk 'NR > 1')"
    if [ -z "$rows" ]; then
        note_unknown "docker top ${CONTAINER} returned no process rows at all - the probe did not work"
    else
        if [ -n "$MODULE" ]; then
            match="$(printf '%s\n' "$rows" | grep -E -- "$(module_regex "$MODULE")" || true)"
        else
            match="$(printf '%s\n' "$rows" | grep -E -- 'python.*utils[./]' || true)"
        fi
        if [ -n "$match" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                note_busy "running now: $(printf '%s' "$line" | awk '{ $1=$2=$3=$4=$5=$6=$7=""; sub(/^ +/, ""); print }')"
            done <<<"$match"
        fi
    fi
fi

# --- 2. what is expected shortly -------------------------------------------
# The supervisor's lock is the only thing that knows a respawn is coming. It
# is taken once at startup and released by an EXIT trap, so it spans the whole
# kill->respawn gap - which is exactly the window `docker top` reads as empty.
#
# Its judgement about a stale lock is copied, not re-derived: the supervisor
# refuses to start on ANY existing lock file, live pid or dead, because taking
# one over cannot be made atomic and losing that race starts a second paid
# backfill (#319). A stale lock therefore blocks here too - if it did not,
# this tool would call a state "safe" that the supervisor calls "stop".
lock_glob="${REPO_ROOT}/data/.supervisor.${CONTAINER}.${MODULE:-*}.lock"
lock_seen=0
for lock in $lock_glob; do
    [ -e "$lock" ] || continue
    lock_seen=1
    owner="$(cat "$lock" 2>/dev/null || true)"
    module_of_lock="$(basename "$lock" .lock)"
    # Quoted separately: an unquoted expansion inside ${..#..} is a glob, so a
    # container name carrying a metacharacter would strip the wrong prefix.
    module_of_lock="${module_of_lock#.supervisor."${CONTAINER}".}"
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
        note_busy "supervised: ${module_of_lock} is watched by pid ${owner}; a respawn is expected even when nothing is running"
    else
        note_busy "stale supervisor lock: ${lock} (pid ${owner:-unknown}, not running). The supervisor refuses to start on this too - remove it by hand once sure, per #319"
    fi
done
[ "$lock_seen" = "1" ] || say "no supervisor lock for ${MODULE:-any module}"

# --- 3. what started and never cleaned up ----------------------------------
# Informational only. A marker outlives its process by design, so its presence
# means "a run started and did not clean up" - equally true of a live job and
# a corpse. It must never be read as mutual exclusion.
for marker in "${REPO_ROOT}"/data/.inflight/*.json; do
    [ -e "$marker" ] || continue
    say "marker: $(basename "$marker") - a run started and did not clean up (a report, not a lock)"
done

for r in ${reasons+"${reasons[@]}"}; do say "  $r"; done

case "$verdict" in
    idle) say "VERDICT: idle - nothing running for ${MODULE:-any utils module}, and no respawn expected"; exit 0 ;;
    busy) say "VERDICT: busy - do not start ${MODULE:-a backfill} now"; exit 1 ;;
    *) say "VERDICT: unknown - a source could not be read, which is not the same as idle; treat as busy"; exit 2 ;;
esac
