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
# One-off `docker compose run` siblings of the app service. Matched by shape
# rather than by prefix - see the note at section 1b.
RUN_CONTAINER_PATTERN="${BACKFILL_STATUS_RUN_PATTERN:--app-run-}"
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

# The docker binary, resolved once. `/usr/local/bin` is absent from the
# non-interactive ssh PATH on the mini, and Docker Desktop installs there, so
# `ssh host 'tools/backfill_status.sh'` found no `docker` at all while an
# interactive login shell found it immediately -- which is why this was
# invisible to anyone who tested by ssh-ing in and typing (measured
# 2026-08-16). Every probe below then failed identically, and the empty output
# of a *failed* `docker ps` read as "there is no such container": the script
# printed `container idealista-app: not running` about a healthy production
# stack, with the same confidence as a measurement.
#
# Resolving it once means the failure is diagnosed once, in the one place that
# can tell "docker is missing" from "the container is gone".
#
# The two absolute candidates live in a variable so a test can empty it. Left
# hardcoded, a "docker is missing" scenario would find this Mac's real
# /usr/local/bin/docker and pass for the wrong reason here while proving the
# opposite on a Linux runner - the machine-dependent-scenario trap that
# CLAUDE.md records from the bash-version gate.
DOCKER_FALLBACKS="${BACKFILL_STATUS_DOCKER_FALLBACKS-/usr/local/bin/docker /Applications/Docker.app/Contents/Resources/bin/docker}"
DOCKER=""
for candidate in \
    "${BACKFILL_STATUS_DOCKER:-}" \
    "$(command -v docker 2>/dev/null || true)" \
    $DOCKER_FALLBACKS; do
    [ -n "$candidate" ] || continue
    [ -x "$candidate" ] || continue
    DOCKER="$candidate"
    break
done

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
if [ -z "$DOCKER" ]; then
    # Not a fact about the container: nothing was asked about it. Naming the
    # cause here is the whole point - `container X: not running` sent a reader
    # to look at production when the defect was this script's PATH.
    note_unknown "docker not found on PATH (non-interactive ssh?) - nothing about the container was measured"
elif ! raw="$("$DOCKER" top "$CONTAINER" 2>/dev/null)"; then
    # `docker ps` failing and `docker ps` answering "none" are different facts,
    # and the empty string is what both look like. Take the exit status, not
    # the output: a daemon that is down must not read as a container that is
    # absent, which is the same conflation one layer in.
    if names="$("$DOCKER" ps --filter "name=^/${CONTAINER}$" --format '{{.Names}}' 2>/dev/null)"; then
        if [ -z "$names" ]; then
            # No container at all is a real state, and during a deploy it is the
            # normal one - but it is also the moment a supervisor is waiting to
            # refill it, so the lock below still decides.
            say "container ${CONTAINER}: not running"
        else
            note_unknown "docker top ${CONTAINER} could not be read - a job may be running right now"
        fi
    else
        note_unknown "docker ps could not be read (is the daemon running?) - the container was not measured"
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

# --- 1b. what is running in a one-off container ----------------------------
# Once a long job has been killed by a deploy a few times, the operator moves
# it out of the app container: `docker compose run --rm --no-deps app python -m
# utils....` gets a sibling named `<project>-app-run-<hash>`, which no deploy
# recreates. That is the right answer to being killed, and it hides the job
# from every check that names one container - `docker top idealista-app`, the
# deploy watcher's survey, and section 1 above. Measured on the mini
# 2026-08-16: a 292-row `recalc_sea_distance` ran in
# `idealistarank-app-run-63587a11c7b0` while `idealista-app` held only
# gunicorn.
#
# It bites the *careful* operator hardest, which is why it belongs here and not
# in a follow-up: a one-off container is exactly where long work goes once
# someone has been burned.
#
# The name is matched on `-app-run-`, not built from a prefix: the app
# container is `${COMPOSE_CONTAINER_PREFIX}-app` (`idealista-app`) while the
# one-off is named for the compose *project* (`idealistarank-app-run-...`), so
# they do not share a prefix, and reading .env to learn the second is both
# forbidden here and unnecessary.
if [ -z "$DOCKER" ]; then
    # Already reported by section 1 with its cause; not repeated here. The
    # verdict is `unknown` either way, and `unknown` blocks like `busy`.
    :
elif ! run_containers="$("$DOCKER" ps --format '{{.Names}}' 2>/dev/null)"; then
    note_unknown "docker ps could not be read - a one-off run container may be writing right now"
else
    for sibling in $(printf '%s\n' "$run_containers" | grep -E -- "$RUN_CONTAINER_PATTERN" || true); do
        if ! sibling_rows="$("$DOCKER" top "$sibling" 2>/dev/null | awk 'NR > 1')"; then
            note_unknown "docker top ${sibling} could not be read - a one-off run container may be writing right now"
            continue
        fi
        if [ -n "$MODULE" ]; then
            sibling_match="$(printf '%s\n' "$sibling_rows" | grep -E -- "$(module_regex "$MODULE")" || true)"
        else
            sibling_match="$(printf '%s\n' "$sibling_rows" | grep -E -- 'python.*utils[./]' || true)"
        fi
        if [ -n "$sibling_match" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                note_busy "running now in ${sibling}: $(printf '%s' "$line" | awk '{ $1=$2=$3=$4=$5=$6=$7=""; sub(/^ +/, ""); print }')"
            done <<<"$sibling_match"
        fi
    done
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
