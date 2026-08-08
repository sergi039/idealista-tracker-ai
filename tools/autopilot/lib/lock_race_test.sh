#!/bin/bash
# Regression test for the autopilot lock.
#
# The scenario that matters: several ticks fire at once and exactly one must
# win. An earlier mkdir-plus-stale-PID implementation passed the single-process
# cases and still produced two simultaneous winners here, which is how this
# test earned its place.
#
# Run: bash tools/autopilot/lib/lock_race_test.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${AUTOPILOT_LOCK_LIB:-${HERE}/lock.sh}"
LOCK="${TMPDIR:-/tmp}/autopilot-lock-race-test.lock"
RESULTS="${TMPDIR:-/tmp}/autopilot-lock-race-results.$$"
WORKER="${TMPDIR:-/tmp}/autopilot-lock-race-worker.$$.sh"

ROUNDS=10
CONTENDERS=8
# Long enough that a second winner would overlap the first and be counted.
HOLD_SECONDS=0.4

pass=0
fail=0

check() {
    if [ "$1" = "$2" ]; then
        echo "  ok: $3"
        pass=$((pass + 1))
    else
        echo "  FAIL: $3 (expected '$2', got '$1')"
        fail=$((fail + 1))
    fi
}

cleanup() { rm -f "$LOCK" "$RESULTS" "$WORKER"; }
trap cleanup EXIT

# A separate executable, not a subshell: a subshell would inherit fd 9 and the
# lock with it, which is not what a real concurrent tick looks like.
cat >"$WORKER" <<WORKER_SH
#!/bin/bash
source "${LIB}"
if autopilot_acquire_lock "\$1"; then
    echo won >>"\$2"
    sleep ${HOLD_SECONDS}
fi
WORKER_SH
chmod +x "$WORKER"

# --- 1. a free lock is acquired -------------------------------------------
rm -f "$LOCK"; : >"$RESULTS"
"$WORKER" "$LOCK" "$RESULTS"
check "$(grep -c won "$RESULTS")" "1" "a free lock is acquired"

# --- 2. a held lock is refused --------------------------------------------
rm -f "$LOCK"; : >"$RESULTS"
"$WORKER" "$LOCK" "$RESULTS" &
holder_pid=$!
sleep 0.15
"$WORKER" "$LOCK" "$RESULTS"
wait "$holder_pid"
check "$(grep -c won "$RESULTS")" "1" "a lock held by a live process is refused"

# --- 3. the lock is released when its holder dies -------------------------
# No PID bookkeeping is involved: the kernel drops it when the fd closes.
rm -f "$LOCK"; : >"$RESULTS"
"$WORKER" "$LOCK" "$RESULTS"
"$WORKER" "$LOCK" "$RESULTS"
check "$(grep -c won "$RESULTS")" "2" "a lock left by a dead process is reusable"

# --- 4. the race ----------------------------------------------------------
race_ok=1
for round in $(seq 1 "$ROUNDS"); do
    rm -f "$LOCK"; : >"$RESULTS"
    for _ in $(seq 1 "$CONTENDERS"); do
        "$WORKER" "$LOCK" "$RESULTS" &
    done
    wait

    winners="$(grep -c won "$RESULTS")"
    if [ "$winners" != "1" ]; then
        echo "  FAIL: round ${round}: ${winners} simultaneous winners (expected 1)"
        fail=$((fail + 1))
        race_ok=0
        break
    fi
done
if [ "$race_ok" = "1" ]; then
    echo "  ok: ${ROUNDS} rounds x ${CONTENDERS} concurrent processes, exactly 1 winner each"
    pass=$((pass + 1))
fi

# --- 5. the race, after a holder was killed without cleanup ---------------
# The hard case. A SIGKILLed build (or a machine that slept) leaves whatever
# on-disk state the lock uses, with no chance to clean up. Implementations that
# reclaim such a lock by checking a recorded PID race here: several contenders
# all decide it is stale and all reclaim it. flock has nothing to reclaim - the
# kernel already released it when the process died.
orphan_ok=1
for round in $(seq 1 "$ROUNDS"); do
    rm -f "$LOCK"; : >"$RESULTS"

    # stderr silenced: the shell announces the SIGKILL of the inherited sleep,
    # which is expected here and only clutters the report.
    "$WORKER" "$LOCK" "${RESULTS}.orphan" 2>/dev/null &
    orphan_pid=$!
    sleep 0.15
    # Children inherit fd 9 and with it the lock, so killing only the shell
    # would leave its `sleep` holding the lock legitimately. Kill the whole
    # family to model a machine that lost the process tree outright.
    pkill -9 -P "$orphan_pid" 2>/dev/null
    kill -9 "$orphan_pid" 2>/dev/null
    wait "$orphan_pid" 2>/dev/null

    for _ in $(seq 1 "$CONTENDERS"); do
        "$WORKER" "$LOCK" "$RESULTS" &
    done
    wait

    winners="$(grep -c won "$RESULTS")"
    if [ "$winners" != "1" ]; then
        echo "  FAIL: orphan round ${round}: ${winners} simultaneous winners (expected 1)"
        fail=$((fail + 1))
        orphan_ok=0
        break
    fi
done
rm -f "${RESULTS}.orphan"
if [ "$orphan_ok" = "1" ]; then
    echo "  ok: after a SIGKILLed holder, ${ROUNDS}x${CONTENDERS} contenders still yield 1 winner"
    pass=$((pass + 1))
fi

echo
echo "passed: ${pass}, failed: ${fail}"
[ "$fail" = "0" ]
