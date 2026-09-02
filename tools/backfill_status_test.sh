#!/bin/bash
# The question `docker top` cannot answer (#338).
#
# The incident this pins: a deploy killed a supervised backfill at 09:01:02, the
# supervisor refilled the container at 09:01:59, and in the 57 seconds between
# them a second session ran `docker top`, saw an empty process list, and started
# a duplicate paid run. The check was correct; it just could not speak about the
# next minute.
#
# Drives the real tools/backfill_status.sh against a stub docker and a stub
# repository root, so every source it consults can be set to a chosen state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_SCRIPT="${SCRIPT_DIR}/backfill_status.sh"

WORK="$(mktemp -d)"
cleanup() { local rc=$?; rm -rf "$WORK"; exit "$rc"; }
trap cleanup EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    printf -- '--- last output ---\n' >&2
    cat "${WORK}/out" >&2 2>/dev/null || true
    exit 1
}

# A repository root whose tools/ holds the script under test, so its own
# REPO_ROOT resolution (dirname/..) lands here rather than in the real repo.
mkdir -p "${WORK}/repo/tools" "${WORK}/repo/data/.inflight" "${WORK}/bin"
cp "$REAL_SCRIPT" "${WORK}/repo/tools/backfill_status.sh"
chmod +x "${WORK}/repo/tools/backfill_status.sh"

cat >"${WORK}/bin/docker" <<'STUB'
#!/bin/bash
case "$1" in
    ps)
        if [ "${DOCKER_PS_RC:-0}" != "0" ]; then
            echo "stub docker: ps failed" >&2
            exit "${DOCKER_PS_RC}"
        fi
        printf '%s\n' "${DOCKER_PS_OUTPUT-}"
        ;;
    top)
        if [ "${DOCKER_TOP_RC:-0}" != "0" ]; then
            echo "stub docker: top failed" >&2
            exit "${DOCKER_TOP_RC}"
        fi
        # `top` takes a container name, and a one-off `docker compose run`
        # sibling holds different processes from the app container. A stub
        # answering the same table for every name could not fail on that.
        case "$2" in
            *-app-run-*) cat "${DOCKER_TOP_SIBLING_FILE:-/dev/null}" 2>/dev/null || true ;;
            *) cat "${DOCKER_TOP_FILE:-/dev/null}" 2>/dev/null || true ;;
        esac
        ;;
esac
exit 0
STUB
chmod +x "${WORK}/bin/docker"

TOP="${WORK}/top.txt"
HEADER='UID PID PPID C STIME TTY TIME CMD'
GUNICORN='appuser 7 1 0 12:00 ? 00:00:03 /app/.venv/bin/python /app/.venv/bin/gunicorn --bind 0.0.0.0:5001 main:app'

set_top() {
    # An `if`, not `[ $# -gt 0 ] && printf`: with no extra rows that list ends
    # in a false test, the function returns 1, and `set -e` kills the suite
    # before its first scenario - "there were no extra rows" reading as "the
    # call failed", which is the shape this whole ticket is about.
    {
        printf '%s\n' "$HEADER" "$GUNICORN"
        if [ $# -gt 0 ]; then printf '%s\n' "$@"; fi
    } >"$TOP"
}
clear_locks() { rm -f "${WORK}"/repo/data/.supervisor.*.lock; }
clear_markers() { rm -f "${WORK}"/repo/data/.inflight/*.json; }

# A pid that is certainly not running: started, reaped, gone.
( exit 0 ) & DEAD_PID=$!
wait "$DEAD_PID" 2>/dev/null || true

SIBLING_TOP="${WORK}/sibling_top.txt"
: >"$SIBLING_TOP"

run_status() {
    set +e
    PATH="${WORK}/bin:${PATH}" \
    DOCKER_TOP_FILE="$TOP" \
    DOCKER_TOP_SIBLING_FILE="$SIBLING_TOP" \
    DOCKER_TOP_RC="${DOCKER_TOP_RC:-0}" \
    DOCKER_PS_RC="${DOCKER_PS_RC:-0}" \
    DOCKER_PS_OUTPUT="${DOCKER_PS_OUTPUT-idealista-app}" \
        bash "${WORK}/repo/tools/backfill_status.sh" --container idealista-app "$@" >"${WORK}/out" 2>&1
    RC=$?
    set -e
}

# --- scenario 1: nothing running, nothing expected -------------------------
set_top; clear_locks; clear_markers
run_status
[ "$RC" = "0" ] || fail "scenario 1: an idle container did not read as idle (rc=$RC)"
grep -q "VERDICT: idle" "${WORK}/out" || fail "scenario 1 did not say idle"
printf 'OK: an idle container with no supervisor reads as idle\n'

# --- scenario 2: a job is running now --------------------------------------
set_top 'appuser 4711 4700 0 12:00 ? 00:00:01 python -m utils.backfill_pool --snapshot data/p.json'
clear_locks; clear_markers
run_status
[ "$RC" = "1" ] || fail "scenario 2: a running job did not read as busy (rc=$RC)"
grep -q "running now: python -m utils.backfill_pool" "${WORK}/out" \
    || fail "scenario 2 did not name the running job"
printf 'OK: a job running now reads as busy, and is named\n'

# --- scenario 3: THE #338 CASE - empty container, live supervisor ----------
# The 57-second window. Nothing is running, and a respawn is one tick away.
# This is the state that read as "safe to start" and cost a duplicate paid run.
set_top; clear_locks; clear_markers
printf '%s\n' "$$" >"${WORK}/repo/data/.supervisor.idealista-app.utils.backfill_pool.lock"
run_status
[ "$RC" = "1" ] || fail "scenario 3: an empty container under a live supervisor read as safe (rc=$RC) - this is #338"
grep -q "a respawn is expected even when nothing is running" "${WORK}/out" \
    || fail "scenario 3 did not explain that a respawn is expected"
printf 'OK: an empty container with a live supervisor is busy, not idle\n'

# --- scenario 4: a stale lock blocks, exactly as the supervisor does -------
# acquire_lock() refuses on ANY existing lock file, live or dead, because
# taking one over cannot be made atomic (#319). Calling this "safe" here would
# make the two tools disagree about one file.
set_top; clear_locks; clear_markers
printf '%s\n' "$DEAD_PID" >"${WORK}/repo/data/.supervisor.idealista-app.utils.backfill_pool.lock"
run_status
[ "$RC" = "1" ] || fail "scenario 4: a stale supervisor lock read as safe (rc=$RC)"
grep -q "stale supervisor lock" "${WORK}/out" || fail "scenario 4 did not name the lock as stale"
printf 'OK: a stale lock blocks here too, the same judgement acquire_lock makes\n'

# --- scenario 5: an unreadable process list is UNKNOWN, never idle ---------
set_top; clear_locks; clear_markers
DOCKER_TOP_RC=1 run_status
[ "$RC" = "2" ] || fail "scenario 5: a failed docker top did not read as unknown (rc=$RC)"
grep -q "VERDICT: unknown" "${WORK}/out" || fail "scenario 5 did not say unknown"
unset DOCKER_TOP_RC
printf 'OK: a docker top that fails is unknown, and unknown blocks\n'

# --- scenario 6: a header with no rows is a failed probe -------------------
printf '%s\n' "$HEADER" >"$TOP"; clear_locks; clear_markers
run_status
[ "$RC" = "2" ] || fail "scenario 6: a header-only table read as idle (rc=$RC)"
printf 'OK: a process table with no rows is a failed probe, not an idle container\n'

# --- scenario 7: a lock for another module does not block this one ---------
# The lock is keyed per (container, module). Reporting it as a general "busy"
# would overstate what it knows.
set_top; clear_locks; clear_markers
printf '%s\n' "$$" >"${WORK}/repo/data/.supervisor.idealista-app.utils.backfill_sea_view.lock"
run_status --module utils.backfill_pool
[ "$RC" = "0" ] || fail "scenario 7: a sea_view supervisor blocked a pool run (rc=$RC)"
run_status
[ "$RC" = "1" ] || fail "scenario 7: without --module, any supervisor should block (rc=$RC)"
printf 'OK: a supervisor lock blocks its own module, and every module when none is named\n'

# --- scenario 8: every spelling python accepts for -m ---------------------
clear_locks; clear_markers
for spelling in \
    'python -m utils.backfill_pool' \
    'python -mutils.backfill_pool' \
    'python -um utils.backfill_pool'; do
    set_top "appuser 4711 4700 0 12:00 ? 00:00:01 ${spelling}"
    run_status --module utils.backfill_pool
    [ "$RC" = "1" ] || fail "scenario 8: '${spelling}' was not recognised as the module running (rc=$RC)"
done
printf 'OK: -m, -mmod and the -um cluster are all read as the module running\n'

# --- scenario 9: a marker is a report, not a lock -------------------------
# A marker outlives its process by design. If its mere presence blocked, the
# tool would refuse forever after any killed run - and would be reading a
# report as mutual exclusion, which is the defect this whole family began as.
set_top; clear_locks; clear_markers
printf '%s\n' '{"module":"backfill_pool","pid":41,"argv":[],"resumable":false}' \
    >"${WORK}/repo/data/.inflight/backfill_pool.41.json"
run_status
[ "$RC" = "0" ] || fail "scenario 9: a leftover marker was read as a lock (rc=$RC)"
grep -q "a report, not a lock" "${WORK}/out" || fail "scenario 9 did not report the marker at all"
printf 'OK: a leftover marker is reported but does not block - it is a report, not a lock\n'

# --- scenario 10: a job moved into its own container is still a job -------
# Once deploys have killed a long run a few times, the operator moves it out:
# `docker compose run --rm --no-deps app python -m utils....` makes a sibling
# named <project>-app-run-<hash>, which no deploy recreates. Measured on the
# mini 2026-08-16: a 292-row recalc_sea_distance ran in
# idealistarank-app-run-63587a11c7b0 while idealista-app held only gunicorn.
# Every check that names one container - docker top idealista-app, the deploy
# watcher's survey, and this tool before this scenario - called that idle.
set_top; clear_locks; clear_markers
{
    printf '%s\n' "$HEADER"
    printf '%s\n' 'appuser 900 1 0 12:00 ? 00:00:07 python -m utils.recalc_sea_distance --only-missing'
} >"$SIBLING_TOP"
DOCKER_PS_OUTPUT="$(printf 'idealista-app\nidealistarank-app-run-63587a11c7b0')" run_status
[ "$RC" = "1" ] || fail "scenario 10: a job in a one-off run container read as idle (rc=$RC)"
grep -q "running now in idealistarank-app-run-63587a11c7b0: python -m utils.recalc_sea_distance" "${WORK}/out" \
    || fail "scenario 10 did not name the container the job actually runs in"
: >"$SIBLING_TOP"
printf 'OK: a job moved into its own compose-run container is still seen, and named\n'

# --- scenario 11: an unreadable container list is UNKNOWN ------------------
# The sibling scan is only as honest as the listing it starts from. A `docker
# ps` that fails must not silently mean "there are no one-off containers".
set_top; clear_locks; clear_markers
DOCKER_PS_RC=1 run_status
[ "$RC" = "2" ] || fail "scenario 11: a failed docker ps read as 'no run containers' (rc=$RC)"
grep -q "docker ps could not be read" "${WORK}/out" \
    || fail "scenario 11 did not say the container list was unreadable"
unset DOCKER_PS_RC
printf 'OK: a docker ps that fails is unknown, not an empty machine\n'

# --- scenario 12: docker is not on the PATH at all -------------------------
# Measured on the mini 2026-08-16: /usr/local/bin is absent from the
# non-interactive ssh PATH, so `ssh host tools/backfill_status.sh` found no
# docker, every probe failed identically, and the empty output of a *failed*
# `docker ps` read as "no such container". The script announced
# `container idealista-app: not running` about a healthy production stack, in
# the same voice as a measurement. The verdict was already right - unknown,
# which blocks - but the line a human reads was a false statement about
# production, and it sent the reader to look at the wrong machine.
#
# "No docker on PATH" has to be *built*, not assumed. A first version of this
# scenario used PATH=/usr/bin:/bin and emptied the fallbacks, which is a fact
# about this Mac and not about Linux: the CI runner ships /usr/bin/docker, so
# the script resolved a real binary, reached a real verdict, and the scenario
# failed there while passing here. That is the machine-dependent-scenario trap
# CLAUDE.md records from the bash-version gate, arriving from the other side.
# So the PATH is a directory built to hold exactly the utilities the script
# needs and nothing else.
set_top; clear_locks; clear_markers
NODOCKER="${WORK}/nodocker"
mkdir -p "$NODOCKER"
for util in awk cat dirname find grep ps sed; do
    util_path="$(command -v "$util" || true)"
    [ -n "$util_path" ] || fail "scenario 12 cannot run: $util not found to build a docker-free PATH"
    ln -sf "$util_path" "${NODOCKER}/${util}"
done
[ -z "$(PATH="$NODOCKER" command -v docker || true)" ] \
    || fail "scenario 12 built a PATH that still has docker on it"
# bash resolved before the PATH is replaced, and invoked absolutely: the
# constructed PATH deliberately holds no shell either.
BASH_BIN="$(command -v bash)"
set +e
PATH="$NODOCKER" BACKFILL_STATUS_DOCKER_FALLBACKS="" \
    "$BASH_BIN" "${WORK}/repo/tools/backfill_status.sh" --container idealista-app >"${WORK}/out" 2>&1
RC=$?
set -e
[ "$RC" = "2" ] || fail "scenario 12: a missing docker did not read as unknown (rc=$RC)"
grep -q "docker not found on PATH" "${WORK}/out" \
    || fail "scenario 12 did not name the missing binary as the cause"
grep -q "container idealista-app: not running" "${WORK}/out" \
    && fail "scenario 12 blamed the container for a PATH problem"
printf 'OK: a missing docker is named as such, not reported as a stopped container\n'

# --- scenario 13: docker only at an absolute path (the mini's shape) --------
# The other half of the same fix: with docker off the PATH but present where
# Docker Desktop puts it, the tool must work normally rather than degrade. A
# fix that only learned to say "not found" would leave every remote check
# permanently unknown, which blocks exactly like busy and would stop every
# backfill on that machine.
set_top; clear_locks; clear_markers
set +e
PATH="/usr/bin:/bin" \
BACKFILL_STATUS_DOCKER_FALLBACKS="${WORK}/bin/docker" \
DOCKER_TOP_FILE="$TOP" \
DOCKER_TOP_SIBLING_FILE="$SIBLING_TOP" \
DOCKER_TOP_RC=0 DOCKER_PS_RC=0 DOCKER_PS_OUTPUT="idealista-app" \
    bash "${WORK}/repo/tools/backfill_status.sh" --container idealista-app >"${WORK}/out" 2>&1
RC=$?
set -e
[ "$RC" = "0" ] || fail "scenario 13: an absolute-path docker did not resolve (rc=$RC)"
grep -q "VERDICT: idle" "${WORK}/out" \
    || fail "scenario 13 did not reach a real verdict through the fallback"
printf 'OK: docker found at its absolute path still measures, it does not degrade\n'

# --- scenario 14: the deploy watcher's stall alarm is surfaced, read-only ---
# data/.deploy_stalled is written by tools/autopilot/deploy_watcher.sh once
# main has been ahead of production for a threshold of refused ticks (#532),
# and this is the script sessions consult before touching the mini - so it
# says so. It changes no verdict: a stalled deployer is not a running backfill.
set_top; clear_locks; clear_markers
printf '%s\n' \
    "STALLED: production is 2 commit(s) behind main after 3 refused tick(s) since 2026-09-01T05:43:14Z - deployed b286668, main 4a69583; last reason: on branch 'codex/issue-473', not 'main'" \
    "deployed=b286668" "main=4a69583" "gap=2" "ticks=3" >"${WORK}/repo/data/.deploy_stalled"
run_status
[ "$RC" = "0" ] || fail "scenario 14: a stalled deploy chain changed the backfill verdict (rc=$RC)"
grep -q "VERDICT: idle" "${WORK}/out" || fail "scenario 14 did not stay idle under a stalled deploy chain"
grep -q "deploy chain: STALLED: production is 2 commit(s) behind main after 3 refused tick(s)" "${WORK}/out" \
    || fail "scenario 14 did not surface the watcher's stall alarm"
rm -f "${WORK}/repo/data/.deploy_stalled"
run_status
grep -q "deploy chain:" "${WORK}/out" && fail "scenario 14 reported a stall after the marker was gone"
printf 'OK: a stalled deploy chain is surfaced, and the backfill verdict is not its business\n'
