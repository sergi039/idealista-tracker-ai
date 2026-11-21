#!/bin/bash
# The check before the move reads and reports, and changes nothing (#639).
#
# Drives the real tools/move_data_dir.sh against a throwaway repository and a
# stub docker that answers what the check asks: what Compose would mount, is
# the container running, what it mounts, can it read its reference files.
#
# What is pinned:
#   * a ready deployment is called ready, and --check is the default;
#   * every refusal names its reason, exits 1, and leaves everything exactly
#     as it was - no container stopped or started, no file written.

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
        while [ $# -gt 0 ] && [ "$1" != "stop" ] && [ "$1" != "up" ] && [ "$1" != "config" ]; do shift; done
        case "${1:-}" in
            config)
                # What Compose would bind at /app/data, the question the
                # reader and the preflight ask: the environment first, then
                # .env - unless the compose file does not interpolate the
                # variable, in which case it is ./data whatever they say.
                source=""
                if grep -v '^[[:space:]]*#' "$STUB_REPO/docker-compose.yml" | grep -F 'source: ${IDEALISTA_DATA_DIR' >/dev/null; then
                    source="${IDEALISTA_DATA_DIR:-}"
                    [ -n "$source" ] || source="$(sed -n 's/^IDEALISTA_DATA_DIR=//p' "$STUB_REPO/.env" | tail -n 1)"
                fi
                [ -n "$source" ] || source="$STUB_REPO/data"
                printf '{"services":{"app":{"volumes":[{"type":"bind","source":"%s","target":"/app/data"}]}}}\n' "$source"
                ;;
        esac
        ;;
    inspect)
        format="$3"
        name="$4"
        case "$format" in
            *State.Running*)
                hook inspect-running && exit 1
                [ ! -e "$STUB/gone" ] || { echo "Error: No such object: $name" >&2; exit 1; }
                cat "$STUB/running"
                ;;
            *Destination*)
                cat "$STUB/mount"
                ;;
            *)
                [ "$name" = "$STUB_APP" ] || { hook inspect-mounts && exit 1; }
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
    ps) hook ps && exit 1; cat "$STUB/containers" ;;
esac
STUB
    chmod +x "${W}/bin/docker"
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
    # What the stub's `compose config` reads to decide whether the variable
    # is mounted.
    printf 'services:\n  app:\n    volumes:\n      - type: bind\n        source: ${IDEALISTA_DATA_DIR:-./data}\n        target: /app/data\n' >"$REPO/docker-compose.yml"
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

# --- 4. preflight refusals: each leaves everything untouched ---------------
refusal() {
    # refusal <label> <text expected in the output> [VAR=value ...] (after the
    # caller has arranged the world)
    local label="$1" text="$2"
    shift 2
    run_move "$@" -- --check "$DEST"
    [ "$RC" = "1" ] || fail "preflight '${label}': exit ${RC}, expected 1"
    grep -q "NOT MOVED" "${W}/out" || fail "preflight '${label}': no NOT MOVED line"
    grep -q -- "$text" "${W}/out" || fail "preflight '${label}': the output does not mention '${text}'"
}

new_world
refusal "exported variable" "is set in this shell's environment" "IDEALISTA_DATA_DIR=${W}/elsewhere"
expect_untouched "preflight: exported variable"
for compose_switch in COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROJECT_NAME; do
    new_world
    refusal "exported ${compose_switch}" "${compose_switch} is set in this shell's environment" "${compose_switch}=other"
    expect_untouched "preflight: exported ${compose_switch}"
done

new_world; mkdir "$DEST"
refusal "destination exists" "already exists"
rmdir "$DEST"; expect_untouched "preflight: destination exists"

new_world
run_move -- --check "relative/path"
[ "$RC" = "1" ] && grep -q "absolute path" "${W}/out" || fail "preflight: a relative destination was accepted"
run_move -- --check "$REPO/state"
[ "$RC" = "1" ] && grep -q "inside the repository" "${W}/out" || fail "preflight: a destination inside the repository was accepted"
run_move -- --check "${W}/with space"
[ "$RC" = "1" ] && grep -q "plain path" "${W}/out" || fail "preflight: a destination the .env reader refuses was accepted"
expect_untouched "preflight: bad destinations"

new_world
printf 'services:\n  app:\n    volumes:\n      - ./data:/app/data\n' >"$REPO/docker-compose.yml"
git -C "$REPO" commit --quiet -am "a compose file from before the mount"
git -C "$REPO" rev-parse HEAD >"$REPO/data/.deployed_sha"; snapshot_original
refusal "compose file without the mount" "would mount"
expect_untouched "preflight: compose file without the mount"

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

# A question docker or git could not answer is a problem, not a "no".
new_world; : >"$STUB/fail.ps"
refusal "docker cannot list the containers" "could not list the containers"
expect_untouched "preflight: docker ps fails"

new_world; printf 'idealista-app\nidealista-app-run-abc\n' >"$STUB/containers"; : >"$STUB/fail.inspect-mounts"
refusal "a sibling's mounts cannot be read" "could not say what container"
printf 'idealista-app\n' >"$STUB/containers"; expect_untouched "preflight: docker inspect of a sibling fails"

new_world
printf '#!/bin/bash\nfor arg in "$@"; do [ "$arg" != "status" ] || exit 1; done\nexec "%s" "$@"\n' "$(command -v git)" >"${W}/bin/git"
chmod +x "${W}/bin/git"
refusal "git status fails" "git could not say"
rm -f "${W}/bin/git"; expect_untouched "preflight: git status fails"

new_world; printf 'false\n' >"$STUB/running"
refusal "app not running" "is not running"
printf 'true\n' >"$STUB/running"; expect_untouched "preflight: app not running"

new_world; ln -s /etc "$REPO/data/link"
refusal "a symbolic link in the directory" "symbolic link"
rm "$REPO/data/link"; expect_untouched "preflight: symlink"

# The tombstone is the fact that the entry exists: a FIFO of that name is one,
# and nothing waits on it.
new_world; mkfifo "$REPO/data/.moved-to"
refusal "a FIFO named as the tombstone" "the state was moved before"
rm -f "$REPO/data/.moved-to"; expect_untouched "preflight: FIFO tombstone"

if [ "$(id -u)" != "0" ]; then
    # A directory that may not be searched hides its tombstone from the shell,
    # and from find - whose failure used to end the script without a word.
    new_world; chmod 600 "$REPO/data"
    run_move -- --check "$DEST"
    chmod 700 "$REPO/data"
    [ "$RC" = "1" ] && grep -q "NOT MOVED" "${W}/out" || fail "preflight 'unsearchable directory': exit ${RC}"
    grep -q "cannot be searched by this user" "${W}/out" && grep -q "could not be searched in full" "${W}/out" \
        || fail "preflight 'unsearchable directory': the two reasons are not both listed"
    expect_untouched "preflight: unsearchable directory"
fi

new_world; git -C "$REPO" checkout --quiet -b other
refusal "wrong branch" "is not on main"
git -C "$REPO" checkout --quiet main; expect_untouched "preflight: wrong branch"
pass "every preflight refusal leaves the deployment untouched"

printf '\nALL %d GROUPS PASSED\n' "$passed"
