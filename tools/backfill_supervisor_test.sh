#!/bin/bash
# Drives the real tools/backfill_supervisor.sh against a stub `docker`.
#
# The three behaviours worth pinning are the three that were wrong in the
# throwaway version used on the mini on 2026-08-14:
#
#   * a budget that counted loop ticks gave up after an hour of healthy
#     running (it must count restarts);
#   * a `docker exec` that fails during a rebuild looked like "no job
#     running", which would start a second copy of a live job;
#   * a reused snapshot path makes the restarted backfill exit instead of
#     run, because the tools refuse to overwrite a rollback point.
#
# The stub docker records every invocation, so a test asserts what the
# supervisor actually asked docker to do rather than that it merely exited 0.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR="${SCRIPT_DIR}/backfill_supervisor.sh"
failures=0

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; failures=$((failures + 1)); }

# scenario: cmdline listing that stub `docker exec` prints, plus whether
# `docker ps` lists the container.
make_stub() {
    local dir="$1" listing="$2" ps_output="$3" exec_rc="${4:-0}"
    mkdir -p "$dir/bin"
    cat >"$dir/bin/docker" <<STUB
#!/bin/bash
echo "\$@" >> "$dir/docker-calls.log"
case "\$1" in
  ps) printf '%s\n' '$ps_output' ;;
  exec)
    if [ "\$2" = "-d" ]; then exit 0; fi
    if [ "$exec_rc" != "0" ]; then exit $exec_rc; fi
    printf '%s\n' '$listing'
    ;;
esac
exit 0
STUB
    chmod +x "$dir/bin/docker"
}

run_supervisor() {
    local dir="$1"; shift
    ( cd "$dir" && PATH="$dir/bin:$PATH" timeout 20 "$SUPERVISOR" \
        --module utils.backfill_pool \
        --snapshot-prefix data/pool_backfill \
        --interval 1 --max-ticks 3 \
        --log "$dir/supervisor.log" \
        --run-log "$dir/run.log" "$@" )
}

echo "backfill_supervisor.sh"

# 1. A live job is left alone.
dir="$(mktemp -d)"
make_stub "$dir" "python -m utils.backfill_pool --snapshot data/x.json" "idealista-app"
run_supervisor "$dir" >/dev/null 2>&1
# "supervisor: restart N/M", not the header's "max 12 restarts".
if grep -q "supervisor: restart " "$dir/supervisor.log" 2>/dev/null; then
    fail "a running job is not restarted" "$(cat "$dir/supervisor.log")"
else
    pass "a running job is not restarted"
fi
rm -rf "$dir"

# 2. A dead job is restarted, with a snapshot path that is not reused.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
run_supervisor "$dir" >/dev/null 2>&1
starts="$(grep -c -- "-d idealista-app" "$dir/docker-calls.log" 2>/dev/null || echo 0)"
snapshots="$(grep -o -- "--snapshot [^ ]*" "$dir/docker-calls.log" 2>/dev/null | sort -u | wc -l | tr -d ' ')"
if [ "$starts" -ge 1 ]; then
    pass "a dead job is restarted"
else
    fail "a dead job is restarted" "docker calls: $(cat "$dir/docker-calls.log" 2>/dev/null)"
fi
if [ "$starts" -le 1 ] || [ "$snapshots" = "$starts" ]; then
    pass "every restart gets a fresh snapshot path"
else
    fail "every restart gets a fresh snapshot path" "$starts starts, $snapshots distinct snapshots"
fi
rm -rf "$dir"

# 3. A container it cannot read is not treated as idle.
dir="$(mktemp -d)"
make_stub "$dir" "" "idealista-app" 1
run_supervisor "$dir" >/dev/null 2>&1
if grep -q "not assuming it is idle" "$dir/supervisor.log" 2>/dev/null &&
   ! grep -q -- "-d idealista-app" "$dir/docker-calls.log" 2>/dev/null; then
    pass "an unreadable container is not treated as idle"
else
    fail "an unreadable container is not treated as idle" "$(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 4. A container that is absent (mid-deploy) is waited for, not restarted into.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" ""
run_supervisor "$dir" >/dev/null 2>&1
if grep -q "absent (deploy in progress)" "$dir/supervisor.log" 2>/dev/null &&
   ! grep -q -- "-d idealista-app" "$dir/docker-calls.log" 2>/dev/null; then
    pass "an absent container is waited for, not started into"
else
    fail "an absent container is waited for, not started into" "$(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 5. A finished run stops the supervisor, whatever the container says.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
mkdir -p "$dir/data"; echo "INFO:__main__:Done: {'processed': 10}" > "$dir/run.log"
run_supervisor "$dir" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && grep -q "reported done" "$dir/supervisor.log" 2>/dev/null &&
   ! grep -q -- "-d idealista-app" "$dir/docker-calls.log" 2>/dev/null; then
    pass "a finished run stops the supervisor without restarting"
else
    fail "a finished run stops the supervisor without restarting" "rc=$rc $(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 6. The restart budget counts restarts, and running out is not silent.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
run_supervisor "$dir" --max-restarts 1 >/dev/null 2>&1
rc=$?
starts="$(grep -c -- "-d idealista-app" "$dir/docker-calls.log" 2>/dev/null || echo 0)"
if [ "$starts" -eq 1 ] && [ "$rc" -eq 1 ] && grep -q "stopping for a human" "$dir/supervisor.log" 2>/dev/null; then
    pass "the restart budget is spent on restarts and its exhaustion is loud"
else
    fail "the restart budget is spent on restarts and its exhaustion is loud" \
         "rc=$rc starts=$starts $(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 7. A missing --module is a usage error, not a silent no-op.
if "$SUPERVISOR" >/dev/null 2>&1; then
    fail "a missing --module fails loudly" "exited 0"
else
    pass "a missing --module fails loudly"
fi

if [ "$failures" -gt 0 ]; then
    printf '\n%d check(s) failed\n' "$failures"
    exit 1
fi
printf '\nall checks passed\n'
