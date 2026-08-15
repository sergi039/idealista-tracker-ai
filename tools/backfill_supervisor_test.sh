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
# An independent Tier-2 audit then broke the first version of those fixes, and
# each of its findings is a check here too. Two of them were confirmed by
# probe before being fixed: an inspection that printed a partial listing and
# then FAILED started the paid backfill twice while the real one was alive,
# and a "Done" left in the append-only run log by an earlier run made the
# supervisor exit 0 at its first tick without calling docker at all.
#
# The stub docker records every invocation, so a test asserts what the
# supervisor actually asked docker to do rather than that it merely exited 0.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISOR="${SCRIPT_DIR}/backfill_supervisor.sh"
failures=0

pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; failures=$((failures + 1)); }

# scenario: cmdline listing that stub `docker exec` prints, whether `docker ps`
# lists the container, the status the inspection exits with, and an optional
# line it appends to run.log the first time it is asked (a job finishing while
# the supervisor watches).
make_stub() {
    local dir="$1" listing="$2" ps_output="$3" exec_rc="${4:-0}" done_line="${5:-}"
    mkdir -p "$dir/bin"
    cat >"$dir/bin/docker" <<STUB
#!/bin/bash
echo "\$@" >> "$dir/docker-calls.log"
case "\$1" in
  ps) printf '%s\n' '$ps_output' ;;
  exec)
    if [ "\$2" = "-d" ]; then exit 0; fi
    if [ -n '$done_line' ] && [ ! -f "$dir/.done-written" ]; then
      : > "$dir/.done-written"
      mkdir -p "$dir/data"
      printf '%s\n' '$done_line' >> "$dir/data/run.log"
    fi
    printf '%s\n' '$listing'
    exit $exec_rc
    ;;
esac
exit 0
STUB
    chmod +x "$dir/bin/docker"
}

# The run log lives under data/ because that is the only path bind mounted into
# the container, so the host's data/run.log and the container's /app/data/run.log
# are one file. BACKFILL_LOCK_ROOT is the lock's test seam: in production the
# lock is anchored to the repository so that two supervisors cannot take two
# different locks by passing two different --log paths.
run_supervisor() {
    local dir="$1"; shift
    ( cd "$dir" && PATH="$dir/bin:$PATH" BACKFILL_LOCK_ROOT="$dir/lock" timeout 20 "$SUPERVISOR" \
        --module utils.backfill_pool \
        --snapshot-prefix data/pool_backfill \
        --interval 1 --max-ticks 3 \
        --log "$dir/supervisor.log" \
        --run-log data/run.log "$@" )
}

starts_in() {
    local f="$1/docker-calls.log"
    [ -f "$f" ] || { echo 0; return; }
    grep -c -- "-d idealista-app" "$f" 2>/dev/null || true
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
starts="$(starts_in "$dir")"
snapshots="$(grep -o -- "--snapshot [^ ]*" "$dir/docker-calls.log" 2>/dev/null | sort -u | wc -l | tr -d ' ')"
if [ "$starts" -ge 1 ]; then
    pass "a dead job is restarted"
else
    fail "a dead job is restarted" "docker calls: $(cat "$dir/docker-calls.log" 2>/dev/null)"
fi
# The scenario really performs three restarts, so this compares three paths;
# it is not the starts<=1 escape hatch quietly passing.
if [ "$starts" -ge 2 ] && [ "$snapshots" = "$starts" ]; then
    pass "every restart gets a fresh snapshot path"
else
    fail "every restart gets a fresh snapshot path" "$starts starts, $snapshots distinct snapshots"
fi
rm -rf "$dir"

# 2b. ...including restarts inside one second, which a clock-only name cannot
# distinguish. The backfill would exit on "refusing to overwrite a rollback
# point" instead of running.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
run_supervisor "$dir" --interval 0 >/dev/null 2>&1
starts="$(starts_in "$dir")"
snapshots="$(grep -o -- "--snapshot [^ ]*" "$dir/docker-calls.log" 2>/dev/null | sort -u | wc -l | tr -d ' ')"
if [ "$starts" -ge 2 ] && [ "$snapshots" = "$starts" ]; then
    pass "restarts within one second still get distinct snapshot paths"
else
    fail "restarts within one second still get distinct snapshot paths" \
         "$starts starts, $snapshots distinct snapshots"
fi
rm -rf "$dir"

# 2c. ...and across two invocations that share a second. The restart counter
# restarts at 1 in every run, so without the pid two supervisors started in the
# same second hand the backfill a path it already refused. `date` is stubbed
# here so the collision is deterministic rather than a matter of timing.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
cat >"$dir/bin/date" <<'DATE'
#!/bin/bash
case "$1" in
  +%Y%m%d_%H%M%S) printf '20260815_120000\n' ;;
  *) printf '2026-08-15 12:00:00\n' ;;
esac
DATE
chmod +x "$dir/bin/date"
run_supervisor "$dir" --interval 0 >/dev/null 2>&1
run_supervisor "$dir" --interval 0 >/dev/null 2>&1
starts="$(starts_in "$dir")"
snapshots="$(grep -o -- "--snapshot [^ ]*" "$dir/docker-calls.log" 2>/dev/null | sort -u | wc -l | tr -d ' ')"
if [ "$starts" -ge 4 ] && [ "$snapshots" = "$starts" ]; then
    pass "two supervisors in one second do not reuse a snapshot path"
else
    fail "two supervisors in one second do not reuse a snapshot path" \
         "$starts starts, $snapshots distinct snapshots"
fi
rm -rf "$dir"

# 3. A container it cannot read is not treated as idle.
dir="$(mktemp -d)"
make_stub "$dir" "" "idealista-app" 1
run_supervisor "$dir" >/dev/null 2>&1
if grep -q "not assuming it is idle" "$dir/supervisor.log" 2>/dev/null &&
   [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "an unreadable container is not treated as idle"
else
    fail "an unreadable container is not treated as idle" "$(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 3b. ...and "unreadable" is decided by the exit status, not by whether
# anything came out. A partial listing from a FAILING inspection is the case
# that started two paid backfills against a live one.
dir="$(mktemp -d)"
make_stub "$dir" "/app/.venv/bin/python /app/.venv/bin/gunicorn " "idealista-app" 1
run_supervisor "$dir" >/dev/null 2>&1
if grep -q "not assuming it is idle" "$dir/supervisor.log" 2>/dev/null &&
   [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "a failing inspection that still printed output is not idle either"
else
    fail "a failing inspection that still printed output is not idle either" \
         "starts=$(starts_in "$dir") $(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 4. A container that is absent (mid-deploy) is waited for, not restarted into.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" ""
run_supervisor "$dir" >/dev/null 2>&1
if grep -q "absent (deploy in progress)" "$dir/supervisor.log" 2>/dev/null &&
   [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "an absent container is waited for, not started into"
else
    fail "an absent container is waited for, not started into" "$(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 5. A run that finishes WHILE the supervisor watches stops it, without a restart.
dir="$(mktemp -d)"
# No quotes in the appended line: the stub embeds it in a single-quoted printf.
make_stub "$dir" "python -m utils.backfill_pool --snapshot data/x.json" "idealista-app" 0 \
    "INFO:__main__:Done: 10 processed"
run_supervisor "$dir" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && grep -q "reported done" "$dir/supervisor.log" 2>/dev/null &&
   [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "a finished run stops the supervisor without restarting"
else
    fail "a finished run stops the supervisor without restarting" "rc=$rc $(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 5b. A "Done" left by an EARLIER run in the append-only log is not this run's.
# Before this was scoped, the second supervision of any module exited 0 at its
# first tick having called docker zero times, and logged it as success.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
mkdir -p "$dir/data"; echo "INFO:__main__:Done: {'processed': 40}" > "$dir/data/run.log"
run_supervisor "$dir" >/dev/null 2>&1
rc=$?
if grep -q "reported done" "$dir/supervisor.log" 2>/dev/null || [ "$(starts_in "$dir")" -eq 0 ]; then
    fail "an earlier run's Done does not end this one" \
         "rc=$rc starts=$(starts_in "$dir") $(cat "$dir/supervisor.log" 2>/dev/null)"
else
    pass "an earlier run's Done does not end this one"
fi
rm -rf "$dir"

# 6. The restart budget counts restarts, and running out is not silent.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
run_supervisor "$dir" --max-restarts 1 >/dev/null 2>&1
rc=$?
starts="$(starts_in "$dir")"
if [ "$starts" -eq 1 ] && [ "$rc" -eq 1 ] && grep -q "stopping for a human" "$dir/supervisor.log" 2>/dev/null; then
    pass "the restart budget is spent on restarts and its exhaustion is loud"
else
    fail "the restart budget is spent on restarts and its exhaustion is loud" \
         "rc=$rc starts=$starts $(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 6b. A non-integer budget is refused rather than quietly disabling itself:
# `[ 0 -ge abc ]` reads as false, which restarts a paid job every tick.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
run_supervisor "$dir" --max-restarts abc >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "a non-integer budget is refused, not silently unlimited"
else
    fail "a non-integer budget is refused, not silently unlimited" "rc=$rc starts=$(starts_in "$dir")"
fi
rm -rf "$dir"

# 7. A missing --module is a usage error, before any docker call.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
( cd "$dir" && PATH="$dir/bin:$PATH" BACKFILL_LOCK_ROOT="$dir/lock" timeout 20 "$SUPERVISOR" \
    --snapshot-prefix data/p ) >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ] && [ ! -f "$dir/docker-calls.log" ]; then
    pass "a missing --module fails loudly, before touching docker"
else
    fail "a missing --module fails loudly, before touching docker" \
         "rc=$rc docker calls: $(cat "$dir/docker-calls.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 8. A missing --snapshot-prefix is refused. Without it every restart omits
# --snapshot, which the backfills require, so the budget drains against runs
# that exit at once.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
( cd "$dir" && PATH="$dir/bin:$PATH" BACKFILL_LOCK_ROOT="$dir/lock" timeout 20 "$SUPERVISOR" \
    --module utils.backfill_pool --interval 1 --max-ticks 2 \
    --log "$dir/supervisor.log" --run-log data/run.log ) >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ ! -f "$dir/docker-calls.log" ]; then
    pass "a missing --snapshot-prefix is refused before any restart"
else
    fail "a missing --snapshot-prefix is refused before any restart" \
         "rc=$rc docker calls: $(cat "$dir/docker-calls.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 9. An absolute --run-log is refused: the container would redirect to
# /app/<path> while the host reads <path>, two different files.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
( cd "$dir" && PATH="$dir/bin:$PATH" BACKFILL_LOCK_ROOT="$dir/lock" timeout 20 "$SUPERVISOR" \
    --module utils.backfill_pool --snapshot-prefix data/p --interval 1 --max-ticks 2 \
    --log "$dir/supervisor.log" --run-log "$dir/run.log" ) >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ ! -f "$dir/docker-calls.log" ]; then
    pass "an absolute --run-log is refused"
else
    fail "an absolute --run-log is refused" "rc=$rc"
fi
rm -rf "$dir"

# 10. A longer module name is not this module. `utils.backfill_pool` used to
# match `utils.backfill_pool_v2` (substring, and `.` is a regex wildcard), so
# an unrelated job made the watched one look alive forever.
dir="$(mktemp -d)"
make_stub "$dir" "python -m utils.backfill_pool_v2 --snapshot data/x.json" "idealista-app"
run_supervisor "$dir" >/dev/null 2>&1
if [ "$(starts_in "$dir")" -ge 1 ]; then
    pass "a longer module name does not count as this module running"
else
    fail "a longer module name does not count as this module running" \
         "$(cat "$dir/supervisor.log" 2>/dev/null)"
fi
rm -rf "$dir"

# 11. Two supervisors for one module and container do not both run: they would
# restart the job independently and bill the same rows twice.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
mkdir -p "$dir/lock"
printf '%s\n' "$$" > "$dir/lock/.supervisor.idealista-app.utils.backfill_pool.lock"
run_supervisor "$dir" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "a second supervisor for the same job refuses to start"
else
    fail "a second supervisor for the same job refuses to start" \
         "rc=$rc starts=$(starts_in "$dir")"
fi
rm -rf "$dir"

# 12. A lock that cannot be created at all is not a lock that is free. mkdir
# fails for two reasons, and only "already exists" means somebody holds it —
# here a plain file sits at the lock path. That used to fall through to the
# stale-lock takeover, write no pid, and supervise unlocked: the same fail-open
# as reading a failed inspection as "idle".
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
: > "$dir/lock"   # a FILE where the lock directory must go
run_supervisor "$dir" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "a lock that cannot be created stops the supervisor"
else
    fail "a lock that cannot be created stops the supervisor" \
         "rc=$rc starts=$(starts_in "$dir")"
fi
rm -rf "$dir"

# 13. `-m mod`, `-mmod` and `-um mod` are one invocation to python. Anchoring
# on the literal "-m " missed the last two, so a live paid backfill read as
# idle and a second copy was started against it.
for form in "python -um utils.backfill_pool --snapshot data/x.json" \
            "python -mutils.backfill_pool --snapshot data/x.json"; do
    dir="$(mktemp -d)"
    make_stub "$dir" "$form" "idealista-app"
    run_supervisor "$dir" >/dev/null 2>&1
    if [ "$(starts_in "$dir")" -eq 0 ]; then
        pass "a live job started as '${form%% --*}' is left alone"
    else
        fail "a live job started as '${form%% --*}' is left alone" \
             "starts=$(starts_in "$dir")"
    fi
    rm -rf "$dir"
done

# 14. A run log outside data/ is refused. Only ./data is bind mounted, so
# `--run-log run.log` writes /app/run.log in the container while the host reads
# ./run.log — completion is never seen and a finished job is restarted until
# the budget is gone.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
( cd "$dir" && PATH="$dir/bin:$PATH" BACKFILL_LOCK_ROOT="$dir/lock" timeout 20 "$SUPERVISOR" \
    --module utils.backfill_pool --snapshot-prefix data/p --interval 1 --max-ticks 2 \
    --log "$dir/supervisor.log" --run-log run.log ) >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ ! -f "$dir/docker-calls.log" ]; then
    pass "a run log outside data/ is refused"
else
    fail "a run log outside data/ is refused" "rc=$rc"
fi
rm -rf "$dir"

# 15. An empty --done-pattern is refused: grep matches an empty pattern against
# every line, so the first progress line would report the paid job as finished.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
run_supervisor "$dir" --done-pattern "" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ ! -f "$dir/docker-calls.log" ]; then
    pass "an empty --done-pattern is refused"
else
    fail "an empty --done-pattern is refused" "rc=$rc"
fi
rm -rf "$dir"

# 16. A lock naming no pid is NOT stale. A supervisor that has just created its
# lock is momentarily in that state, and taking it over is how two paid
# backfills start.
dir="$(mktemp -d)"
make_stub "$dir" "sh -c sleep 1" "idealista-app"
mkdir -p "$dir/lock"
: > "$dir/lock/.supervisor.idealista-app.utils.backfill_pool.lock"
run_supervisor "$dir" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 2 ] && [ "$(starts_in "$dir")" -eq 0 ]; then
    pass "a lock naming no pid is not taken over"
else
    fail "a lock naming no pid is not taken over" "rc=$rc starts=$(starts_in "$dir")"
fi
rm -rf "$dir"

if [ "$failures" -gt 0 ]; then
    printf '\n%d check(s) failed\n' "$failures"
    exit 1
fi
printf '\nall checks passed\n'
