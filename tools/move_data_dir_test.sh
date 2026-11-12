#!/bin/bash
# Moving the deployment's state directory must never leave the app down or the
# state in two places (#639).
#
# Drives the real tools/move_data_dir.sh against a throwaway repository, a
# stateful stub docker and a stub curl. The stub docker behaves like Compose
# where it matters: `up` reads IDEALISTA_DATA_DIR from the repository's .env to
# decide what is mounted, refuses a source that does not exist, and records
# whether the variable leaked into its environment.
#
# What is pinned, in the order the review of the plan asked for it:
#   * the happy path, and that --check touches nothing;
#   * every preflight refusal leaves everything exactly as it was;
#   * (in the change that follows this one) a failure at EACH step after the
#     app is stopped ends with the app running on the original directory;
#   * an exported IDEALISTA_DATA_DIR can neither start the move nor steer it;
#   * --rollback returns everything, including what was written after the move.

set -euo pipefail
unset IDEALISTA_DATA_DIR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_SCRIPT="${MOVE_SCRIPT_UNDER_TEST:-${SCRIPT_DIR}/move_data_dir.sh}"
REAL_LIB_DIR="${MOVE_LIB_UNDER_TEST:-${SCRIPT_DIR}/autopilot/lib}"

ROOT="$(mktemp -d)"
ROOT="$(cd "$ROOT" && pwd -P)"
cleanup() { local rc=$?; rm -rf "$ROOT"; exit "$rc"; }
trap cleanup EXIT

passed=0
fail() {
    printf 'FAIL: %s\n' "$*" >&2
    printf -- '--- script output ---\n' >&2
    cat "${W}/out" >&2 2>/dev/null || true
    printf -- '--- docker calls ---\n' >&2
    cat "${W}/docker.log" >&2 2>/dev/null || true
    exit 1
}
pass() { passed=$((passed + 1)); printf 'OK: %s\n' "$*"; }

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

REFERENCE="ine_municipal.json hospitals_cnh.json sepe_unemployment.json top_agencies.json"

write_stubs() {
    mkdir -p "${W}/bin"
    cat >"${W}/bin/docker" <<'STUB'
#!/bin/bash
# State: $STUB/running (true|false), $STUB/mount (host directory at /app/data).
printf '%s | %s=%s\n' "$*" "IDEALISTA_DATA_DIR" "${IDEALISTA_DATA_DIR-<unset>}" >>"$STUB/../docker.log"
hook() { [ -e "$STUB/fail.$1" ] || return 1; [ -e "$STUB/keep.$1" ] || rm -f "$STUB/fail.$1"; return 0; }
case "$1" in
    compose)
        shift
        while [ $# -gt 0 ] && [ "$1" != "stop" ] && [ "$1" != "up" ]; do shift; done
        case "${1:-}" in
            stop)
                hook stop && exit 1
                printf 'false\n' >"$STUB/running"
                ;;
            up)
                while [ -e "$STUB/hang.up" ]; do sleep 0.1; done
                hook up && exit 1
                source="$(sed -n 's/^IDEALISTA_DATA_DIR=//p' "$STUB_REPO/.env" | tail -n 1)"
                [ -n "$source" ] || source="$STUB_REPO/data"
                # create_host_path: false - a missing source is an error.
                [ -d "$source" ] || { echo "stub compose: bind source $source does not exist" >&2; exit 1; }
                printf '%s\n' "$source" >"$STUB/mount"
                printf 'true\n' >"$STUB/running"
                ;;
        esac
        ;;
    inspect)
        format="$3"
        name="$4"
        case "$format" in
            *State.Running*)
                hook inspect-running && exit 1
                cat "$STUB/running"
                ;;
            *Destination*)
                cat "$STUB/mount"
                ;;
            *)
                if [ "$name" = "$STUB_APP" ]; then cat "$STUB/mount"; else cat "$STUB/mounts.$name" 2>/dev/null || true; fi
                ;;
        esac
        ;;
    exec)
        [ "$(cat "$STUB/running")" = "true" ] || exit 1
        shift 2
        case "$1" in
            cat) cat "$(cat "$STUB/mount")/${2#/app/data/}" ;;
            sh) [ ! -e "$STUB/no-reference" ] ;;
        esac
        ;;
    ps) cat "$STUB/containers" ;;
esac
STUB
    cat >"${W}/bin/curl" <<'STUB'
#!/bin/bash
running="$(cat "$STUB/running")"
for arg in "$@"; do
    if [ "$arg" = '%{http_code}' ]; then
        if [ "$running" = "true" ]; then cat "$STUB/page"; else printf '000'; fi
        exit 0
    fi
done
[ "$running" = "true" ] && [ ! -e "$STUB/unhealthy" ]
STUB
    chmod +x "${W}/bin/docker" "${W}/bin/curl"
}

# A fresh world per scenario: repository, state, backups, stub state.
new_world() {
    W="$(mktemp -d "${ROOT}/w.XXXXXX")"
    REPO="${W}/IdealistaRank"
    DEST="${W}/IdealistaRank-data"
    BACKUPS="${W}/backups"
    STUB="${W}/stub"
    mkdir -p "$REPO/data/.inflight" "$REPO/tools/autopilot/lib" "$STUB"
    git init --quiet --initial-branch=main "$REPO"
    {
        printf 'data/*\n.env\n'
        for name in $REFERENCE; do printf '!data/%s\n' "$name"; done
    } >"$REPO/.gitignore"
    for name in $REFERENCE; do printf '{"file":"%s"}\n' "$name" >"$REPO/data/$name"; done
    cp "$REAL_SCRIPT" "$REPO/tools/move_data_dir.sh"
    cp "${REAL_LIB_DIR}/data_dir.sh" "${REAL_LIB_DIR}/render_check.sh" "$REPO/tools/autopilot/lib/"
    printf '#!/bin/bash\nexit "$(cat "%s/status-rc")"\n' "$STUB" >"$REPO/tools/backfill_status.sh"
    chmod +x "$REPO/tools/move_data_dir.sh" "$REPO/tools/backfill_status.sh"
    git -C "$REPO" add -A
    git -C "$REPO" commit --quiet -m "first"

    # The deployment's state, as the container left it.
    printf '4711\n' >"$REPO/data/.last_seen_uid_properties"
    printf '{"held":[4690]}\n' >"$REPO/data/.last_seen_uid_properties.held.json"
    printf '{"api":"places","units":1}\n' >"$REPO/data/google_spend.jsonl"
    printf '{"rows":3}\n' >"$REPO/data/pool_weight_enable_snapshot.json"
    printf 'tick\n' >"$REPO/data/autopilot-deploy.log"
    printf 'session=local_test\n' >"$REPO/data/.orchestrator"
    git -C "$REPO" rev-parse HEAD >"$REPO/data/.deployed_sha"
    printf 'DB_PASSWORD=hunter2\nAPP_HOST_PORT=5001\n' >"$REPO/.env"
    chmod 600 "$REPO/.env"

    printf 'true\n' >"$STUB/running"
    printf '%s\n' "$REPO/data" >"$STUB/mount"
    printf 'idealista-app\n' >"$STUB/containers"
    printf '200' >"$STUB/page"
    printf '0\n' >"$STUB/status-rc"
    : >"${W}/docker.log"
    write_stubs
    snapshot_original
}

# What "exactly as it was" means: the state directory's content and .env.
tree_of() { ( cd "$1" && find . -type f ! -name '*.log' -exec shasum -a 256 {} + | LC_ALL=C sort ); }
snapshot_original() {
    tree_of "$REPO/data" >"${W}/original.tree"
    shasum -a 256 "$REPO/.env" | cut -d' ' -f1 >"${W}/original.env"
}

run_move() {
    # run_move [VAR=value ...] -- <script arguments>
    local envs=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
    shift
    set +e
    env -u IDEALISTA_DATA_DIR \
        PATH="${W}/bin:${PATH}" \
        STUB="$STUB" STUB_REPO="$REPO" STUB_APP="idealista-app" \
        MOVE_BACKUP_ROOT="$BACKUPS" \
        MOVE_DEPLOY_LOCK="${W}/deploy.lock" MOVE_MERGE_LOCK="${W}/merge.lock" \
        MOVE_HEALTH_URL="http://127.0.0.1:1/api/healthz" \
        MOVE_HEALTH_TIMEOUT=2 MOVE_HEALTH_POLL=1 \
        ${envs[@]+"${envs[@]}"} \
        /bin/bash "$REPO/tools/move_data_dir.sh" "$@" >"${W}/out" 2>&1
    RC=$?
    set -e
}

expect_original() {
    # expect_original <label>: the state is in <repo>/data, untouched, .env is
    # byte-identical, nothing is at the destination, and the app is running on
    # the original directory.
    local label="$1"
    [ -d "$REPO/data" ] || fail "${label}: <repo>/data is gone"
    [ ! -e "$REPO/data/.moved-to" ] || fail "${label}: a tombstone was left in <repo>/data"
    [ ! -e "$DEST" ] || fail "${label}: the destination still exists"
    tree_of "$REPO/data" | cmp -s - "${W}/original.tree" \
        || fail "${label}: the state directory is not what it was"
    [ "$(shasum -a 256 "$REPO/.env" | cut -d' ' -f1)" = "$(cat "${W}/original.env")" ] \
        || fail "${label}: .env was changed"
    [ "$(cat "$STUB/running")" = "true" ] || fail "${label}: the app was left stopped"
    [ "$(cat "$STUB/mount")" = "$REPO/data" ] || fail "${label}: the app is not on the original directory"
    [ -z "$(git -C "$REPO" status --porcelain)" ] || fail "${label}: the checkout is dirty"
}

expect_untouched() {
    # Stricter: docker was never asked to change anything.
    expect_original "$1"
    grep -E '^compose .* (stop|up) ' "${W}/docker.log" >/dev/null \
        && fail "$1: docker compose stop/up was called"
    [ ! -e "$BACKUPS" ] || fail "$1: a backup directory was created"
    return 0
}

expect_moved() {
    local label="$1" name
    [ "$RC" = "0" ] || fail "${label}: exit ${RC}"
    grep -q "^MOVED:" "${W}/out" || fail "${label}: no MOVED line"
    tree_of "$DEST" | cmp -s - "${W}/original.tree" || fail "${label}: the moved directory is not what was there before"
    [ "$(sed -n 's/^IDEALISTA_DATA_DIR=//p' "$REPO/.env")" = "$DEST" ] || fail "${label}: .env does not name the destination"
    grep -q '^DB_PASSWORD=hunter2$' "$REPO/.env" || fail "${label}: another line of .env was lost"
    [ "$(stat -f '%Lp' "$REPO/.env" 2>/dev/null || stat -c '%a' "$REPO/.env")" = "600" ] || fail "${label}: .env lost its mode"
    grep -q hunter2 "${W}/out" && fail "${label}: a line of .env was printed"
    [ "$(cat "$STUB/running")" = "true" ] || fail "${label}: the app is not running"
    [ "$(cat "$STUB/mount")" = "$DEST" ] || fail "${label}: the app is not mounted on the destination"
    for name in $REFERENCE; do
        [ -f "$REPO/data/$name" ] || fail "${label}: <repo>/data lost the reference file ${name}"
    done
    grep -q "^moved_to=${DEST}\$" "$REPO/data/.moved-to" || fail "${label}: no tombstone naming the destination"
    [ -f "$REPO/data/.orchestrator" ] || fail "${label}: .orchestrator did not stay readable from the checkout"
    [ ! -e "$REPO/data/.last_seen_uid_properties" ] || fail "${label}: the cursor is still in the checkout"
    [ -z "$(git -C "$REPO" status --porcelain)" ] || fail "${label}: the checkout is dirty after the move"
    [ -z "$(find "$DEST" -name '.move-probe-*')" ] || fail "${label}: the probe file was left in the state directory"
}

no_leak() {
    # No docker compose call ever saw the variable in its environment.
    grep -E '^compose ' "${W}/docker.log" | grep -v 'IDEALISTA_DATA_DIR=<unset>' >/dev/null \
        && fail "$1: docker compose was run with IDEALISTA_DATA_DIR in its environment"
    return 0
}

# --- 1. --check says ready and touches nothing ------------------------------
new_world
run_move -- --check "$DEST"
[ "$RC" = "0" ] || fail "scenario 1: --check refused a ready deployment (rc=${RC})"
grep -q "^READY:" "${W}/out" || fail "scenario 1: no READY line"
expect_untouched "scenario 1"
run_move -- "$DEST"
[ "$RC" = "0" ] && grep -q "^READY:" "${W}/out" || fail "scenario 1: --check is not the default mode"
expect_untouched "scenario 1 (default mode)"
pass "--check is read-only, and it is the default"

# --- 2. the move ------------------------------------------------------------
new_world
run_move -- --apply "$DEST"
expect_moved "scenario 2"
no_leak "scenario 2"
backup="$(find "$BACKUPS" -maxdepth 1 -name 'data-pre-move-*' -type d | head -n 1)"
[ -n "$backup" ] || fail "scenario 2: no safety copy"
tree_of "$backup" | cmp -s - "${W}/original.tree" || fail "scenario 2: the safety copy is not the directory as it was"
# Stop strictly before the rename, start strictly after .env was written: the
# stub's `up` read the destination from .env, which is what expect_moved saw.
stop_at="$(grep -n ' stop app' "${W}/docker.log" | head -n 1 | cut -d: -f1)"
up_at="$(grep -n ' up -d --no-build app' "${W}/docker.log" | head -n 1 | cut -d: -f1)"
[ -n "$stop_at" ] && [ -n "$up_at" ] && [ "$stop_at" -lt "$up_at" ] || fail "scenario 2: stop/up order"
grep -q -- '--build' "${W}/docker.log" | grep -v -- '--no-build' && fail "scenario 2: the move built an image"
# The one reader agrees with what was mounted.
[ "$(env -u IDEALISTA_DATA_DIR /bin/bash "$REPO/tools/autopilot/lib/data_dir.sh" "$REPO")" = "$DEST" ] \
    || fail "scenario 2: data_dir.sh does not resolve the destination"
pass "the move: state outside the checkout, app on it, safety copy verified, .env intact"

# --- 3. --rollback brings back everything, including later writes ----------
printf '4750\n' >"$DEST/.last_seen_uid_properties"
printf '{"api":"places","units":2}\n' >>"$DEST/google_spend.jsonl"
printf 'session=local_newer\n' >"$REPO/data/.orchestrator"
tree_of "$DEST" >"${W}/after-move.tree"
: >"${W}/docker.log"
run_move -- --rollback
[ "$RC" = "0" ] || fail "scenario 3: --rollback exit ${RC}"
grep -q "^ROLLED BACK:" "${W}/out" || fail "scenario 3: no ROLLED BACK line"
[ ! -e "$DEST" ] || fail "scenario 3: the moved directory still exists"
[ "$(cat "$REPO/data/.last_seen_uid_properties")" = "4750" ] || fail "scenario 3: a write made after the move was lost"
grep -q '"units":2' "$REPO/data/google_spend.jsonl" || fail "scenario 3: a ledger line written after the move was lost"
[ "$(cat "$REPO/data/.orchestrator")" = "session=local_newer" ] || fail "scenario 3: the newer .orchestrator was replaced by the old one"
[ ! -e "$REPO/data/.moved-to" ] || fail "scenario 3: the tombstone survived the rollback"
grep -q '^IDEALISTA_DATA_DIR=' "$REPO/.env" && fail "scenario 3: .env still names the variable"
grep -q '^DB_PASSWORD=hunter2$' "$REPO/.env" || fail "scenario 3: another line of .env was lost"
[ "$(cat "$STUB/running")" = "true" ] && [ "$(cat "$STUB/mount")" = "$REPO/data" ] \
    || fail "scenario 3: the app is not running on <repo>/data"
[ -z "$(git -C "$REPO" status --porcelain)" ] || fail "scenario 3: the checkout is dirty"
no_leak "scenario 3"
run_move -- --rollback
[ "$RC" = "1" ] && grep -q "nothing to roll back" "${W}/out" || fail "scenario 3: a second --rollback did not say there is nothing to do"
pass "--rollback returns the directory with everything written since, and is refused when there is nothing to undo"

# --- 4. preflight refusals: each leaves everything untouched ---------------
refusal() {
    # refusal <label> <text expected in the output> [VAR=value ...] (after the
    # caller has arranged the world)
    local label="$1" text="$2"
    shift 2
    run_move "$@" -- --apply "$DEST"
    [ "$RC" = "1" ] || fail "preflight '${label}': exit ${RC}, expected 1"
    grep -q "NOT MOVED" "${W}/out" || fail "preflight '${label}': no NOT MOVED line"
    grep -q -- "$text" "${W}/out" || fail "preflight '${label}': the output does not mention '${text}'"
}

new_world
refusal "exported variable" "is set in this shell's environment" "IDEALISTA_DATA_DIR=${W}/elsewhere"
expect_untouched "preflight: exported variable"

new_world; mkdir "$DEST"
refusal "destination exists" "already exists"
rmdir "$DEST"; expect_untouched "preflight: destination exists"

new_world
run_move -- --apply "relative/path"
[ "$RC" = "1" ] && grep -q "absolute path" "${W}/out" || fail "preflight: a relative destination was accepted"
run_move -- --apply "$REPO/state"
[ "$RC" = "1" ] && grep -q "inside the repository" "${W}/out" || fail "preflight: a destination inside the repository was accepted"
run_move -- --apply "${W}/with space"
[ "$RC" = "1" ] && grep -q "plain path" "${W}/out" || fail "preflight: a destination the .env reader refuses was accepted"
expect_untouched "preflight: bad destinations"

new_world; printf 'x\n' >"$REPO/untracked.txt"
refusal "dirty checkout" "uncommitted or untracked"
rm "$REPO/untracked.txt"; expect_untouched "preflight: dirty checkout"

new_world; printf '0000000000000000000000000000000000000000\n' >"$REPO/data/.deployed_sha"; snapshot_original
refusal "deploy pending" "a deploy is pending"
expect_untouched "preflight: deploy pending"

new_world; printf '1\n' >"$STUB/status-rc"
refusal "backfill busy" "answered 1"
expect_untouched "preflight: backfill busy"
printf '2\n' >"$STUB/status-rc"
refusal "backfill unknown" "answered 2"
expect_untouched "preflight: backfill unknown"

new_world; : >"$STUB/no-reference"
refusal "image without reference files" "does not carry"
expect_untouched "preflight: old image"

new_world; printf 'idealista-app\nidealista-app-run-abc\n' >"$STUB/containers"; printf '%s\n' "$REPO/data" >"$STUB/mounts.idealista-app-run-abc"
refusal "a second container mounts the directory" "also mounts"
expect_untouched "preflight: sibling container"

new_world; printf 'false\n' >"$STUB/running"
refusal "app not running" "is not running"
printf 'true\n' >"$STUB/running"; expect_untouched "preflight: app not running"

new_world; ln -s /etc "$REPO/data/link"
refusal "a symbolic link in the directory" "symbolic link"
rm "$REPO/data/link"; expect_untouched "preflight: symlink"

new_world; git -C "$REPO" checkout --quiet -b other
refusal "wrong branch" "is not on main"
git -C "$REPO" checkout --quiet main; expect_untouched "preflight: wrong branch"
pass "every preflight refusal leaves the deployment untouched"

# --- 5. a held lock stops it before anything is touched --------------------
new_world
python3 - "${W}/deploy.lock" "${W}/held" <<'PY' &
import fcntl, os, sys, time
handle = open(sys.argv[1], "w")
fcntl.flock(handle, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while os.path.exists(sys.argv[2]):
    time.sleep(0.05)
PY
holder=$!
for _ in $(seq 1 100); do [ -e "${W}/held" ] && break; sleep 0.05; done
run_move -- --apply "$DEST"
rm -f "${W}/held"; wait "$holder" 2>/dev/null || true
[ "$RC" = "1" ] && grep -q "deploy is in progress" "${W}/out" || fail "scenario 5: a held deploy lock did not stop the move (rc=${RC})"
expect_untouched "scenario 5"
pass "a deploy in progress stops the move before it starts"

# --- 6, 7. failures after the app is stopped, and a recovery that cannot ---
# --- finish: the next change adds them; this file then counts 8 groups. ------

# --- 8. the locks do not outlive the script --------------------------------
new_world
run_move -- --apply "$DEST"
[ "$RC" = "0" ] || fail "scenario 8: setup move failed"
python3 - "${W}/deploy.lock" "${W}/merge.lock" <<'PY' || fail "scenario 8: a lock is still held after the script exited"
import fcntl, sys
for path in sys.argv[1:]:
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
pass "both locks are free once the script has exited"

printf '\nALL %d GROUPS PASSED\n' "$passed"
