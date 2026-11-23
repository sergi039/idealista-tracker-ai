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
#     as it was - no container stopped or started, no file written;
#   * every run of --check, ready or not: docker is asked only to read (any
#     other verb fails the run), and nothing under the checkout changes - not
#     a file, not a timestamp, not the git index;
#   * every mount of every container is compared with the state directory by
#     real path, and two filesystems are told apart by mount point too;
#   * the move (the stub's `up` mounts what `.env` names); there is no
#     `--rollback` in this version;
#   * --apply holds both locks through its preflight, and refuses a safety-copy
#     directory on another filesystem before anything is touched;
#   * a failure at EACH step after the stop, signals included - a hung
#     `docker compose` call too - ends on the original directory (SIGKILL runs
#     no trap: it leaves the state whole in the directory `.env` names).

set -euo pipefail
unset IDEALISTA_DATA_DIR
# The harness's own `git status` must not write the index either; the script
# under test runs without this (run_move) and has to say it itself.
export GIT_OPTIONAL_LOCKS=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_SCRIPT="${MOVE_SCRIPT_UNDER_TEST:-${SCRIPT_DIR}/move_data_dir.sh}"
REAL_LIB_DIR="${MOVE_LIB_UNDER_TEST:-${SCRIPT_DIR}/autopilot/lib}"

ROOT="$(mktemp -d)"
ROOT="$(cd "$ROOT" && pwd -P)"
cleanup() { local rc=$?; chmod -R u+rwx "$ROOT" 2>/dev/null; rm -rf "$ROOT"; exit "$rc"; }
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
# State: $STUB/running (true|false), $STUB/mount (host directory at /app/data),
# $STUB/containers (docker ps -a), $STUB/mounts.<name> (Type, Name, Destination,
# Source, tab-separated; for the app, its mounts besides /app/data; a
# $STUB/raw-mounts.<name> is answered verbatim instead),
# $STUB/volume.<name> (the local driver's device option), $STUB/image (the
# app's image id), $STUB/ref-env (the app's REFERENCE_DATA_DIR),
# $STUB/image-root (that image's file tree, with nothing mounted), $STUB/user
# (the app's Config.User), $STUB/image-user (the image's own USER),
# $STUB/image-volumes (its Config.Volumes), $STUB/unreadable-by (a user who
# cannot read the image's reference files) and $STUB/live-ref (the directory
# the running app sees at its REFERENCE_DATA_DIR, when a mount covers the
# image's; absent, it sees the image's).
#
# A check may only ask; a move (STUB_MODE=write) may also stop,
# start and `exec cat`. Anything else is recorded in forbidden and refused -
# and a stop is still obeyed, so a check that stopped the app leaves it stopped.
printf '%s | %s=%s\n' "$*" "IDEALISTA_DATA_DIR" "${IDEALISTA_DATA_DIR-<unset>}" >>"$STUB/../docker.log"
# An `inspect` that reads its stdin, as a CLI may: it must not eat the list
# the check is looping over.
[ "$1" != "inspect" ] || [ ! -e "$STUB/eat-stdin" ] || cat >/dev/null
hook() { [ -e "$STUB/fail.$1" ] || return 1; [ -e "$STUB/keep.$1" ] || rm -f "$STUB/fail.$1"; return 0; }
# A call that hangs, once (with hang.wedged, every stop and up after it): it
# records its PID and waits until it is killed (or its world is removed).
hang_once() {
    [ -e "$STUB/hang.$1" ] || return 0
    rm -f "$STUB/hang.$1"
    [ ! -e "$STUB/hang.wedged" ] || : >"$STUB/hang.stop" >"$STUB/hang.up"
    printf '%s\n' "$$" >>"$STUB/hang.pid"
    while [ -d "$STUB" ]; do sleep 0.1; done
    exit 1
}
forbidden() {
    printf '%s\n' "$*" >>"$STUB/../forbidden"
    case " $* " in *" stop "* | *" kill "* | *" down "*) printf 'false\n' >"$STUB/running" ;; esac
    exit 97
}
case "$1" in
    compose)
        all="$*"
        shift
        while [ $# -gt 0 ]; do
            case "$1" in
                -f | -p | --file | --project-name | --project-directory | --env-file) shift 2 ;;
                -*) shift ;;
                *) break ;;
            esac
        done
        case "${1:-}" in
            config) ;;
            stop | up) [ "${STUB_MODE:-check}" = "write" ] || forbidden "$all" ;;
            *) forbidden "$all" ;;
        esac
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
            stop)
                hang_once stop
                hook stop && exit 1
                grep -q '^IDEALISTA_DATA_DIR=' "$STUB_REPO/.env" || : >"$STUB/stop-before-intent"
                printf 'false\n' >"$STUB/running"
                ;;
            up)
                hang_once up
                # Failed half-way: the old container gone, no new one.
                if hook up-gone; then : >"$STUB/gone"; : >"$STUB/containers"; exit 1; fi
                hook up && exit 1
                source="$(sed -n 's/^IDEALISTA_DATA_DIR=//p' "$STUB_REPO/.env" | tail -n 1)"
                [ -n "$source" ] || source="$STUB_REPO/data"
                # create_host_path: false - a missing source is an error.
                [ -d "$source" ] || { echo "stub compose: bind source $source does not exist" >&2; exit 1; }
                printf '%s\n' "$source" >"$STUB/mount"
                printf 'true\n' >"$STUB/running"
                if [ -e "$STUB/gone" ]; then rm -f "$STUB/gone"; printf '%s\n' "$STUB_APP" >"$STUB/containers"; fi
                ;;
        esac
        ;;
    inspect)
        [ "$2" = "-f" ] && [ $# -eq 4 ] || forbidden "$*"
        format="$3"
        name="$4"
        case "$format" in
            *State.Running*)
                hook inspect-running && exit 1
                if [ "$(cat "$STUB/running")" = "false" ]; then hook inspect-stopped && exit 1; fi
                [ ! -e "$STUB/gone" ] || { echo "Error: No such object: $name" >&2; exit 1; }
                cat "$STUB/running"
                ;;
            '{{json .Mounts}}')
                hook "inspect-mounts.$name" && exit 1
                if [ -e "$STUB/raw-mounts.$name" ]; then cat "$STUB/raw-mounts.$name"; exit 0; fi
                {
                    if [ "$name" = "$STUB_APP" ]; then printf 'bind\t\t/app/data\t%s\n' "$(cat "$STUB/mount")"; fi
                    cat "$STUB/mounts.$name" 2>/dev/null || true
                } | python3 -c 'import json, sys; print(json.dumps([dict(zip(("Type", "Name", "Destination", "Source"), l.rstrip("\n").split("\t"))) for l in sys.stdin]))'
                ;;
            '{{.Image}}')
                cat "$STUB/image"
                ;;
            '{{.Config.User}}')
                cat "$STUB/user"
                ;;
            *'"REFERENCE_DATA_DIR="'*)
                # The value, the template's text after its last {{end}}, and
                # the newline docker ends every answer with.
                cat "$STUB/ref-env"
                printf '%s\n' "${3##*\{\{end\}\}}"
                ;;
            *'eq .Destination "/app/data"'*)
                cat "$STUB/mount"
                ;;
            *) forbidden "$*" ;;
        esac
        ;;
    volume)
        [ "$2" = "inspect" ] && [ "$3" = "-f" ] && [ $# -eq 5 ] || forbidden "$*"
        hook "volume.$5" && exit 1
        cat "$STUB/volume.$5" 2>/dev/null || printf '\n'
        ;;
    image)
        [ "$2 $3 $4" = "inspect -f {{json .Config.Volumes}}" ] && [ "$5" = "$(cat "$STUB/image")" ] && [ $# -eq 5 ] \
            || forbidden "$*"
        cat "$STUB/image-volumes"
        ;;
    run)
        # A throwaway container of the app's image with nothing mounted,
        # read-only and offline, as --user or the image's own USER: the
        # probe's own script runs against the image's tree. Docker would give
        # an image that declares a VOLUME an anonymous one, so that is refused.
        args="$*"
        shift
        [ "$1 $2 $3 $4" = "--rm --read-only --network none" ] || forbidden "$args"
        shift 4
        as="$(cat "$STUB/image-user")"
        [ "$1" != "--user" ] || { as="$2"; shift 2; }
        [ "$1 $2 $3" = "--entrypoint sh -e" ] && [ "${4%%=*}" = "REFERENCE_DATA_DIR" ] \
            && [ "$5" = "$(cat "$STUB/image")" ] && [ "$6" = "-c" ] && [ "$8" = "sh" ] || forbidden "$args"
        case "$(cat "$STUB/image-volumes")" in null | "{}") ;; *) forbidden "an anonymous volume: $args" ;; esac
        dir="${4#REFERENCE_DATA_DIR=}"
        script="$7"
        shift 8
        state="$1"
        shift
        root="$(cd "$STUB/image-root" && pwd -P)"
        # A user who may not read the files sees a directory without them.
        if [ "$as" = "$(cat "$STUB/unreadable-by" 2>/dev/null)" ]; then
            mkdir -p "$STUB/as-denied"
            REFERENCE_DATA_DIR="$STUB/as-denied" sh -c "$script" sh "$root$state" "$@"
        else
            REFERENCE_DATA_DIR="$root$dir" sh -c "$script" sh "$root$state" "$@"
        fi
        ;;
    exec)
        # The move's round-trip probe, only in a move.
        if [ "$3" = "cat" ]; then
            [ "${STUB_MODE:-check}" = "write" ] && [ "$2" = "$STUB_APP" ] && [ $# -eq 4 ] || forbidden "$*"
            hang_once exec
            [ "$(cat "$STUB/running")" = "true" ] || exit 1
            [ -e "$STUB/stale" ] && [ -e "$STUB_REPO/data/.moved-to" ] && { echo stale; exit 0; }
            cat "$(cat "$STUB/mount")/${4#/app/data/}"
            exit 0
        fi
        # The running app looks at its own REFERENCE_DATA_DIR: exactly
        # `exec <app> sh -c <script> sh <files>`, nothing else.
        [ "$2" = "$STUB_APP" ] && [ "$3 $4" = "sh -c" ] && [ "$6" = "sh" ] || forbidden "$*"
        hook exec && exit 1
        script="$5"
        shift 6
        if [ -e "$STUB/live-ref" ]; then live="$STUB/live-ref"; else live="$(cd "$STUB/image-root" && pwd -P)$(cat "$STUB/ref-env")"; fi
        REFERENCE_DATA_DIR="$live" sh -c "$script" sh "$@"
        ;;
    ps)
        [ "$*" = "ps -a --format {{.Names}}" ] || forbidden "$*"
        hook ps && exit 1
        # Once: may another process take the two locks now?
        if [ -e "$STUB/probe-locks" ]; then
            rm -f "$STUB/probe-locks"
            python3 -c 'import fcntl, sys
for name, path in (("deploy", sys.argv[1]), ("merge", sys.argv[2])):
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(name, "free")
        except BlockingIOError:
            print(name, "held")' "$STUB/../deploy.lock" "$STUB/../merge.lock" >"$STUB/locks-at-preflight"
        fi
        cat "$STUB/containers"
        ;;
    *) forbidden "$*" ;;
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

    # df answers through the real one, except that a path named in
    # $STUB/df-elsewhere is reported on another mount point, and
    # $STUB/df-fails makes it answer nothing.
    cat >"${W}/bin/df" <<STUB
#!/bin/bash
[ ! -e "\$STUB/df-fails" ] || exit 1
# Once: did this call inherit a descriptor numbered as the locks' (8, 9)?
if [ -e "\$STUB/probe-fds" ]; then
    rm -f "\$STUB/probe-fds"
    for fd in 8 9; do [ ! -e "/dev/fd/\$fd" ] || printf 'fd%s ' "\$fd"; done >"\$STUB/fds-at-preflight"
fi
out="\$("$(command -v df)" "\$@")" || exit \$?
if [ -e "\$STUB/df-elsewhere" ] && [ "\${!#}" = "\$(cat "\$STUB/df-elsewhere")" ]; then
    printf '%s\n' "\$out" | awk 'NR == 1 { print; next } { \$NF = "/Volumes/Elsewhere"; print }'
else
    printf '%s\n' "\$out"
fi
STUB
    chmod +x "${W}/bin/df"
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

    # A tracked file whose timestamp no longer matches the index: `git status`
    # would refresh the index and write it back, unless told not to.
    touch -t 202001010000 "$REPO/docker-compose.yml"

    printf 'true\n' >"$STUB/running"
    printf '%s\n' "$REPO/data" >"$STUB/mount"
    # Mounts that have nothing to do with the state, and must not stop the
    # move: the app's other ones, a database on a named volume, a sibling
    # whose source merely starts with the same characters.
    printf 'idealista-app\nidealista-db\nidealista-backup\n' >"$STUB/containers"
    printf 'bind\t\t/app/code\t%s\ntmpfs\t\t/run\t\n' "$REPO/tools" >"$STUB/mounts.idealista-app"
    printf 'volume\tpgdata\t/var/lib/postgresql/data\t/var/lib/docker/volumes/pgdata/_data\n' >"$STUB/mounts.idealista-db"
    printf 'bind\t\t/backup\t%s\n' "$REPO/data-old" >"$STUB/mounts.idealista-backup"
    # The image carries the reference files outside /app/data.
    printf 'sha256:stubimage\n' >"$STUB/image"
    printf '/opt/idealista/reference_data' >"$STUB/ref-env"
    mkdir -p "$STUB/image-root/opt/idealista/reference_data"
    for name in $REFERENCE; do printf '{}\n' >"$STUB/image-root/opt/idealista/reference_data/$name"; done
    : >"$STUB/user"
    printf 'appuser' >"$STUB/image-user"
    printf 'null' >"$STUB/image-volumes"
    printf '200' >"$STUB/page"
    printf '0\n' >"$STUB/status-rc"
    : >"${W}/docker.log"
    write_stubs
    snapshot_original
}

# A file's mode, in octal - from python: `stat -f` means something else on Linux.
mode_of() { python3 -c 'import os, stat, sys; print("%o" % stat.S_IMODE(os.stat(sys.argv[1]).st_mode))' "$1"; }

# What "exactly as it was" means: the state directory's content and .env.
tree_of() { ( cd "$1" && find . -type f ! -name '*.log' -exec shasum -a 256 {} + | LC_ALL=C sort ); }
snapshot_original() {
    tree_of "$REPO/data" >"${W}/original.tree"
    shasum -a 256 "$REPO/.env" | cut -d' ' -f1 >"${W}/original.env"
}

# Every entry under the checkout - .git, data and the working tree - with its
# type, size and modification time, and the index by content.
checkout_snapshot() {
    python3 - "$REPO" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
def line(path):
    try:
        s = os.lstat(path)
    except OSError as error:
        print(path, "unreadable", error.errno)
        return
    print(path, oct(s.st_mode), s.st_size, s.st_mtime_ns)
line(root)
for top, dirs, files in os.walk(root):
    dirs.sort()
    for name in sorted(dirs + files):
        line(os.path.join(top, name))
with open(os.path.join(root, ".git", "index"), "rb") as index:
    print("index sha256", hashlib.sha256(index.read()).hexdigest())
PY
}

# check, or write for a move: what the stub lets docker do.
stub_mode() {
    local arg
    for arg in "$@"; do case "$arg" in --apply|--rollback) echo write; return ;; esac; done
    echo check
}

run_move() {
    # run_move [VAR=value ...] -- <script arguments>
    # Fails the scenario outright if the run asked docker for what its mode
    # may not, or - a check - wrote anything under the checkout.
    local envs=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
    shift
    local mode
    mode="$(stub_mode "$@")"
    checkout_snapshot >"${W}/before.snapshot"
    set +e
    # stdin is /dev/null: a reader outside a loop gets end of file, not this
    # harness's own stdin, so a check that reads stdin fails instead of hanging.
    env -u IDEALISTA_DATA_DIR -u GIT_OPTIONAL_LOCKS \
        PATH="${W}/bin:${PATH}" \
        STUB="$STUB" STUB_REPO="$REPO" STUB_APP="idealista-app" STUB_MODE="$mode" \
        MOVE_BACKUP_ROOT="$BACKUPS" \
        MOVE_DEPLOY_LOCK="${W}/deploy.lock" MOVE_MERGE_LOCK="${W}/merge.lock" \
        MOVE_HEALTH_URL="http://127.0.0.1:1/api/healthz" \
        MOVE_HEALTH_TIMEOUT=2 MOVE_HEALTH_POLL=1 \
        ${envs[@]+"${envs[@]}"} \
        /bin/bash "$REPO/tools/move_data_dir.sh" "$@" </dev/null >"${W}/out" 2>&1
    RC=$?
    set -e
    [ ! -s "${W}/forbidden" ] || fail "the ${mode} asked docker for what it may not: $(head -n 1 "${W}/forbidden")"
    [ "$mode" = "check" ] || return 0
    checkout_snapshot >"${W}/after.snapshot"
    diff "${W}/before.snapshot" "${W}/after.snapshot" >"${W}/written" \
        || fail "the check wrote under the checkout: $(grep '^[<>]' "${W}/written" | head -n 2 | tr '\n' ' ')"
}

# The script under a ten-second bound: RC is its exit status, or 124 if it was
# still running then (a read of a FIFO waits for a writer for ever).
run_move_bounded() {
    local pid waited=0 writer
    set +e
    env -u IDEALISTA_DATA_DIR PATH="${W}/bin:${PATH}" STUB="$STUB" STUB_REPO="$REPO" STUB_APP="idealista-app" \
        STUB_MODE="$(stub_mode "$@")" \
        MOVE_BACKUP_ROOT="$BACKUPS" MOVE_DEPLOY_LOCK="${W}/deploy.lock" MOVE_MERGE_LOCK="${W}/merge.lock" \
        MOVE_HEALTH_URL="http://127.0.0.1:1/api/healthz" MOVE_HEALTH_TIMEOUT=2 MOVE_HEALTH_POLL=1 \
        /bin/bash "$REPO/tools/move_data_dir.sh" "$@" >"${W}/out" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 100 ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    RC=0
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        RC=124
        # Let whatever is still reading a FIFO see end-of-file and go.
        for writer in "$REPO/data/.moved-to" "$REPO/data/.deployed_sha"; do
            [ -p "$writer" ] || continue
            ( : >"$writer" ) 2>/dev/null &
            sleep 1
            kill "$!" 2>/dev/null
            wait "$!" 2>/dev/null
        done
    fi
    # The script's own exit status, from `wait` and nothing else.
    local status=0
    wait "$pid" 2>/dev/null || status=$?
    [ "$RC" = "124" ] || RC=$status
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
    [ "$(mode_of "$REPO/.env")" = "600" ] || fail "${label}: .env lost its mode"
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
    # No compose call that acts saw the variable (`config` only renders; the
    # preflight sets it there on purpose).
    grep -E '^compose .* (stop|up) ' "${W}/docker.log" | grep -v 'IDEALISTA_DATA_DIR=<unset>' >/dev/null \
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
# One stop, then one start that builds nothing, after .env was written: the
# stub's `up` read the destination from .env, which is what expect_moved saw.
calls="$(sed -nE 's/^compose .* (stop|up) (.*) \| IDEALISTA.*/\1 \2/p' "${W}/docker.log" | tr '\n' ';')"
[ "$calls" = "stop app;up -d --no-build app;" ] || fail "scenario 2: compose calls: ${calls}"
[ ! -e "$STUB/stop-before-intent" ] || fail "scenario 2: the app was stopped before .env named the destination"
# The one reader agrees with what was mounted.
[ "$(env -u IDEALISTA_DATA_DIR PATH="${W}/bin:${PATH}" STUB="$STUB" STUB_REPO="$REPO" /bin/bash "$REPO/tools/autopilot/lib/data_dir.sh" "$REPO")" = "$DEST" ] \
    || fail "scenario 2: data_dir.sh does not resolve the destination"
pass "the move: state outside the checkout, app on it, safety copy verified, .env intact"

# --- 3. --rollback brings back everything, including later writes ----------
printf '4750\n' >"$DEST/.last_seen_uid_properties"
printf '{"api":"places","units":2}\n' >>"$DEST/google_spend.jsonl"
printf 'session=local_newer\n' >"$REPO/data/.orchestrator"
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
# Run again: the same answer, and nothing is touched to give it.
: >"${W}/docker.log"
run_move -- --rollback
[ "$RC" = "0" ] && grep -q "^ALREADY ROLLED BACK:" "${W}/out" || fail "scenario 3: a second --rollback: exit ${RC}"
grep -E '^compose .* (stop|up) ' "${W}/docker.log" >/dev/null && fail "scenario 3: a second --rollback stopped or started the app"
[ "$(cat "$REPO/data/.last_seen_uid_properties")" = "4750" ] || fail "scenario 3: a second --rollback changed the state"
pass "--rollback returns the directory with everything written since, and a second one says it is already done"

# --- 3b. a tombstone that is not a file does not hang the script ------------
# A FIFO of that name used to hang `sed`. Both modes answer within ten
# seconds and touch nothing; the instrument is checked first (exit 0 is 0).
new_world
run_move_bounded --help
[ "$RC" = "0" ] || fail "scenario 3b: the bounded run reported exit ${RC} for --help"
mkfifo "$REPO/data/.moved-to"
run_move_bounded --rollback
[ "$RC" != "124" ] || fail "scenario 3b: --rollback was still waiting on a FIFO tombstone after ten seconds"
[ "$RC" = "1" ] && grep -q "NOT ROLLED BACK" "${W}/out" && grep -q "cannot find the moved directory" "${W}/out" \
    || fail "scenario 3b: --rollback over a FIFO tombstone: exit ${RC}"
run_move_bounded --apply "$DEST"
[ "$RC" != "124" ] || fail "scenario 3b: --apply was still waiting on a FIFO tombstone after ten seconds"
[ "$RC" = "1" ] && grep -q "the state was moved before" "${W}/out" \
    || fail "scenario 3b: --apply over a FIFO tombstone: exit ${RC}"
rm -f "$REPO/data/.moved-to"
[ "$(cat "$STUB/running")" = "true" ] && [ ! -e "$DEST" ] || fail "scenario 3b: something was touched"
pass "a FIFO named as the tombstone is refused by both modes, and neither waits on it"

# --- 3c. --rollback: the tombstone when .env lost the line; refusals --------
# (One world each: the safety copy is named to the second.)
moved_world() {
    new_world
    run_move -- --apply "$DEST"
    [ "$RC" = "0" ] || fail "$1: the move that sets the scene failed (exit ${RC})"
    : >"${W}/docker.log"
}
not_rolled_back() {
    # not_rolled_back <label> <text>: refused before the app was stopped.
    [ "$RC" = "1" ] && grep -q "NOT ROLLED BACK" "${W}/out" && grep -q -- "$2" "${W}/out" \
        || fail "$1: exit ${RC}, expected a refusal mentioning '$2'"
    grep -E '^compose .* (stop|up) ' "${W}/docker.log" >/dev/null && fail "$1: the app was stopped"
    [ -d "$DEST" ] && [ "$(cat "$STUB/mount")" = "$DEST" ] || fail "$1: the state was moved"
}
moved_world "scenario 3c"
grep -v '^IDEALISTA_DATA_DIR=' "$REPO/.env" >"${W}/env.without" && cat "${W}/env.without" >"$REPO/.env"
run_move -- --rollback
[ "$RC" = "0" ] && grep -q "^ROLLED BACK:" "${W}/out" \
    || fail "scenario 3c: --rollback without the .env line: exit ${RC}"
[ ! -e "$DEST" ] && [ ! -e "$REPO/data/.moved-to" ] || fail "scenario 3c: the state was not brought back"
[ "$(cat "$STUB/running")" = "true" ] && [ "$(cat "$STUB/mount")" = "$REPO/data" ] \
    || fail "scenario 3c: the app is not running on <repo>/data"
# The tombstone and .env name two different directories.
moved_world "scenario 3c"
mkdir "${W}/elsewhere"
sed "s|^IDEALISTA_DATA_DIR=.*|IDEALISTA_DATA_DIR=${W}/elsewhere|" "$REPO/.env" >"${W}/env.other" && cat "${W}/env.other" >"$REPO/.env"
run_move -- --rollback
not_rolled_back "scenario 3c (two records disagree)" "make them agree by hand"
# Both directories hold files and no tombstone says which is the state (#709
# review, finding 2).
moved_world "scenario 3d"
rm "$REPO/data/.moved-to"; echo theirs >"$REPO/data/x"
run_move -- --rollback
not_rolled_back "scenario 3d (both hold files)" "hold files"
# A backup root on another filesystem: the aside would fail after the stop
# (#709 review, finding 3).
moved_world "scenario 3e"
printf '%s\n' "$BACKUPS" >"$STUB/df-elsewhere"
run_move -- --rollback
not_rolled_back "scenario 3e (backup root elsewhere)" "is not on the mounted filesystem of"
# A tombstone with no .env line, its path one trailing space away from another
# directory (#712 review, finding 1): never trimmed into that directory.
moved_world "scenario 3f"
grep -v '^IDEALISTA_DATA_DIR=' "$REPO/.env" >"${W}/env.without" && cat "${W}/env.without" >"$REPO/.env"
mkdir "${W}/archive" && echo theirs >"${W}/archive/x"
printf 'moved_to=%s \nmoved_at=x\n' "${W}/archive" >"$REPO/data/.moved-to"
run_move -- --rollback
not_rolled_back "scenario 3f (moved_to with a trailing space)" "does not record a destination the way"
[ "$(cat "${W}/archive/x")" = "theirs" ] || fail "scenario 3f: the other directory was touched"
# The state directory on another filesystem than the checkout, the backup root
# on the same (#712 review, finding 2): the rename back would fail after the stop.
moved_world "scenario 3g"
printf '%s\n' "$DEST" >"$STUB/df-elsewhere"
run_move -- --rollback
not_rolled_back "scenario 3g (state elsewhere)" "the rename back would fail"
# A FIFO named as the tombstone in a populated checkout data, .env naming the
# moved directory (#712 review 2, finding 1): no tombstone, so no rollback.
moved_world "scenario 3h"
rm "$REPO/data/.moved-to"; mkfifo "$REPO/data/.moved-to"
echo 100 >"$REPO/data/.last_seen_uid_properties"
run_move_bounded --rollback
[ "$RC" != "124" ] || fail "scenario 3h: --rollback was still waiting on a FIFO tombstone after ten seconds"
not_rolled_back "scenario 3h (FIFO tombstone, .env names the moved directory)" "is not a regular file"
[ -p "$REPO/data/.moved-to" ] && [ "$(cat "$REPO/data/.last_seen_uid_properties")" = "100" ] \
    || fail "scenario 3h: the checkout data was touched"
rm -f "$REPO/data/.moved-to"
# The moved directory gone, the checkout data empty, no tombstone (#712 review
# 2, finding 2): an empty directory is not the restored state.
moved_world "scenario 3i"
mv "$DEST" "${W}/gone"
rm -rf "$REPO/data" && mkdir "$REPO/data"
run_move -- --rollback
[ "$RC" = "1" ] && grep -q "NOT ROLLED BACK" "${W}/out" && grep -q "is empty and" "${W}/out" \
    || fail "scenario 3i (empty checkout data, moved directory gone): exit ${RC}, expected a refusal"
grep -E '^compose .* (stop|up) ' "${W}/docker.log" >/dev/null && fail "scenario 3i: the app was stopped or started"
grep -q '^IDEALISTA_DATA_DIR=' "$REPO/.env" && [ -z "$(ls -A "$REPO/data")" ] \
    || fail "scenario 3i: the .env line or the checkout data was touched"
pass "--rollback finds the moved directory from the tombstone, and refuses before the stop what it cannot undo"

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
    run_move "${compose_switch}=other" -- --rollback
    [ "$RC" = "1" ] && grep -q "${compose_switch} is set" "${W}/out" \
        || fail "rollback: an exported ${compose_switch} was accepted"
done

new_world; mkdir "$DEST"
refusal "destination exists" "already exists"
rmdir "$DEST"; expect_untouched "preflight: destination exists"

# The safety copy in the destination, or the destination in its directory.
new_world
for backups in "$DEST" "${DEST}/b" "$W"; do
    refusal "backup root ${backups#"$W"}" "one holds the other" "MOVE_BACKUP_ROOT=${backups}"
done
expect_untouched "preflight: backup root and destination overlap"
# The safety copy in the directory it copies (#711 review, :716).
new_world
refusal "backup root in the source" "one holds the other" "MOVE_BACKUP_ROOT=${REPO}/data/backups"
expect_untouched "preflight: backup root in the source"

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

# The mount holds all four; only the image's own tree counts.
new_world; rm "$STUB/image-root/opt/idealista/reference_data/top_agencies.json"
refusal "image without reference files" "does not carry top_agencies.json under /opt/idealista/reference_data"
expect_untouched "preflight: old image"
new_world; printf '/app/data/reference' >"$STUB/ref-env"
refusal "reference files read from the state mount" "not from outside /app/data"
expect_untouched "preflight: reference files under the mount"
new_world; : >"$STUB/ref-env"
refusal "no REFERENCE_DATA_DIR" "has no REFERENCE_DATA_DIR"
expect_untouched "preflight: no REFERENCE_DATA_DIR"
# Other spellings of a directory under the mount, and a link into it.
for spelling in /app/./data/reference //app/data/reference /opt/../app/data/reference /app/data/; do
    new_world; printf '%s' "$spelling" >"$STUB/ref-env"
    refusal "reference files read from ${spelling}" "not from outside /app/data"
done
new_world; mkdir -p "$STUB/image-root/app/data"
mv "$STUB/image-root/opt/idealista/reference_data" "$STUB/image-root/app/data/reference"
ln -s ../../app/data/reference "$STUB/image-root/opt/idealista/reference_data"
refusal "reference directory links into the mount" "in the image, not from outside /app/data"
expect_untouched "preflight: reference directory links into the mount"
# A single file linked under the mount is hidden from the app as well.
new_world; mkdir -p "$STUB/image-root/app/data"
mv "$STUB/image-root/opt/idealista/reference_data/top_agencies.json" "$STUB/image-root/app/data/image-only.json"
ln -s ../../../app/data/image-only.json "$STUB/image-root/opt/idealista/reference_data/top_agencies.json"
refusal "a reference file links into the mount" "which the state mount hides from the app"
# A chain whose middle link lies under the mount ends outside it, and the app
# still cannot follow it: every hop counts, not where the chain ends.
new_world; mkdir -p "$STUB/image-root/app/data" "$STUB/image-root/opt/bundled"
mv "$STUB/image-root/opt/idealista/reference_data/top_agencies.json" "$STUB/image-root/opt/bundled/top_agencies.json"
ln -s ../../../app/data/agency-link "$STUB/image-root/opt/idealista/reference_data/top_agencies.json"
ln -s ../../opt/bundled/top_agencies.json "$STUB/image-root/app/data/agency-link"
refusal "a two-hop link through the mount" "/app/data/agency-link under /app/data, which the state mount hides"
expect_untouched "preflight: a two-hop link through the mount"
# So does a parent directory a link passes through.
new_world; mkdir -p "$STUB/image-root/app/data/refs" "$STUB/image-root/srv"
mv "$STUB/image-root/opt/idealista/reference_data/top_agencies.json" "$STUB/image-root/app/data/refs/top_agencies.json"
ln -s ../app/data/refs "$STUB/image-root/srv/refs"
ln -s ../../../srv/refs/top_agencies.json "$STUB/image-root/opt/idealista/reference_data/top_agencies.json"
refusal "a link through a parent under the mount" "which the state mount hides from the app"
# A link that stays outside the mount is followed and passes.
new_world; mkdir -p "$STUB/image-root/opt/bundled"
mv "$STUB/image-root/opt/idealista/reference_data/top_agencies.json" "$STUB/image-root/opt/bundled/top_agencies.json"
ln -s ../../bundled/top_agencies.json "$STUB/image-root/opt/idealista/reference_data/top_agencies.json"
run_move -- --check "$DEST"
[ "$RC" = "0" ] || fail "preflight: a reference file linked outside the mount refused (rc=${RC}): $(grep -m 1 'NOT\|^ *-' "${W}/out")"
# The image carries the files, but the running app mounts an empty, unrelated
# directory over them.
new_world; mkdir "$STUB/live-ref"
refusal "an empty mount over the reference directory" "as it runs now cannot read ine_municipal.json under /opt/idealista/reference_data"
expect_untouched "preflight: an empty mount over the reference directory"
new_world; : >"$STUB/fail.exec"
refusal "docker cannot look into the app" "could not look into idealista-app for its reference files"
# A newline ends the value; $(...) would strip it and check another directory.
new_world; printf '/opt/idealista/reference_data\n' >"$STUB/ref-env"
refusal "REFERENCE_DATA_DIR ends in a newline" "holds a control character or a newline"
new_world; printf '/opt/idealista/reference_data\t' >"$STUB/ref-env"
refusal "REFERENCE_DATA_DIR holds a tab" "holds a control character or a newline"
# The probe reads as the user the app runs as (Compose `user:`), not as the
# image's default.
new_world; printf '1001:1001' >"$STUB/user"; printf '1001:1001' >"$STUB/unreadable-by"
refusal "reference files unreadable by the app's user" "readable by 1001:1001"
new_world; printf '1001:1001' >"$STUB/user"; printf 'appuser' >"$STUB/unreadable-by"
run_move -- --check "$DEST"
[ "$RC" = "0" ] || fail "preflight: files readable by the app's user 1001:1001 refused (rc=${RC})"
# An image that declares a VOLUME is not run: the run would create one.
new_world; printf '{"/app/data":{}}' >"$STUB/image-volumes"
refusal "image declares a volume" "declares volumes"

# A FIFO named .deployed_sha is not read, and nothing waits on it.
new_world; rm "$REPO/data/.deployed_sha"; mkfifo "$REPO/data/.deployed_sha"
run_move_bounded --check "$DEST"
[ "$RC" != "124" ] || fail "preflight: --check was still waiting on a FIFO .deployed_sha after ten seconds"
[ "$RC" = "1" ] && grep -q "deployed_sha is missing or not a regular file" "${W}/out" \
    || fail "preflight: a FIFO .deployed_sha: exit ${RC}"
[ ! -s "${W}/forbidden" ] || fail "preflight: a FIFO .deployed_sha: docker was asked $(head -n 1 "${W}/forbidden")"

# A question docker or git could not answer is a problem, not a "no".
new_world; : >"$STUB/fail.ps"
refusal "docker cannot list the containers" "could not list the containers"
expect_untouched "preflight: docker ps fails"

new_world; : >"$STUB/fail.inspect-mounts.idealista-db"
refusal "a sibling's mounts cannot be read" "could not say what container idealista-db mounts"
expect_untouched "preflight: docker inspect of a sibling fails"

# An answer that cannot be read to its end is not a list of no mounts.
new_world; printf '[{"Type": "bind", "Destination": "/app/data", "Source": "%s"\n' "$REPO/data" >"$STUB/raw-mounts.idealista-db"
refusal "a sibling's mounts are cut off" "could not be read in full"
expect_untouched "preflight: a sibling's mounts are cut off"

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

# Two filesystems are two mount points even on one device; a df that does not
# answer is not "the same".
new_world; printf '%s\n' "$W" >"$STUB/df-elsewhere"
refusal "destination on another mount point" "not on the same mounted filesystem"
expect_untouched "preflight: another mount point"
new_world; : >"$STUB/df-fails"
refusal "df answers nothing" "could not establish which filesystem"
expect_untouched "preflight: df fails"
# The safety copy's directory as well (#709 review, finding 3): a failed move
# renames the rebuilt directory into it, which fails across filesystems with
# the app already stopped. --check and --apply refuse it before anything.
new_world; mkdir "$BACKUPS"; printf '%s\n' "$BACKUPS" >"$STUB/df-elsewhere"
refusal "backup root on another mount point" "is not on the mounted filesystem of"
run_move -- --apply "$DEST"
[ "$RC" = "1" ] && grep -q "NOT MOVED" "${W}/out" && grep -q "is not on the mounted filesystem of" "${W}/out" \
    || fail "preflight: --apply accepted a backup root on another mount point (rc=${RC})"
rmdir "$BACKUPS" || fail "preflight: something was written under a backup root on another mount point"
expect_untouched "preflight: backup root on another mount point"
pass "every preflight refusal leaves the deployment untouched"

# --- 4b. every mount of every container, by real path --------------------
# mounted <label> <container> <mount line>: that one mount, added to the
# ready world, is refused as overlapping the state directory. The line names
# @REPO@ and @W@, filled in once the new world exists - a ${REPO} expanded at
# the call site would still name the previous world.
mounted() {
    local label="$1" container="$2" line="$3"
    new_world
    line="${line//|/$'\t'}"
    line="${line//@REPO@/$REPO}"
    line="${line//@W@/$W}"
    grep -Fx -- "$container" "$STUB/containers" >/dev/null || printf '%s\n' "$container" >>"$STUB/containers"
    printf '%s\n' "$line" >>"$STUB/mounts.${container}"
    refusal "$label" "${4:-inside it, or holds it}"
    expect_untouched "mounts: ${label}"
}
mounted "a sibling mounts the directory" idealista-app-run-abc "bind||/app/data|@REPO@/data"
mounted "a sibling mounts a directory inside it" idealista-app-run-abc "bind||/app/data/.inflight|@REPO@/data/.inflight"
mounted "a sibling mounts a directory that holds it" idealista-tools "bind||/work|@W@"
new_world; ln -s "$REPO" "${W}/link-to-repo"
printf 'idealista-tools\n' >>"$STUB/containers"
printf 'bind\t\t/work\t%s\n' "${W}/link-to-repo/data/.inflight" >"$STUB/mounts.idealista-tools"
refusal "a sibling mounts it through a link" "inside it, or holds it"
expect_untouched "mounts: through a link"
mounted "Docker Desktop's spelling of the path" idealista-tools "bind||/state|/host_mnt@REPO@/data/attachments"
mounted "the app mounts part of it a second time" idealista-app "bind||/app/attachments|@REPO@/data/attachments"
mounted "a mount of a kind the check does not know" idealista-tools "npipe||/pipe|/var/run/x" "does not know"
# A path may hold a "|": the mounts are read as JSON, not split on it.
new_world; printf 'idealista-tools\n' >>"$STUB/containers"
printf 'bind\t\t/a|b\t%s\n' "$REPO/data" >"$STUB/mounts.idealista-tools"
refusal "a destination with a pipe in it" "inside it, or holds it"
expect_untouched "mounts: a pipe in a path"
new_world; printf '%s\n' "$REPO/data" >"$STUB/volume.pgdata"
refusal "a volume created over the directory" "inside it, or holds it"
expect_untouched "mounts: volume over the directory"
new_world; : >"$STUB/fail.volume.pgdata"
refusal "a volume that cannot be described" "could not describe volume pgdata"
expect_untouched "mounts: volume inspect fails"
new_world; : >"$STUB/eat-stdin"
printf 'idealista-zz-last\n' >>"$STUB/containers"
printf 'bind\t\t/state\t%s\n' "$REPO/data" >"$STUB/mounts.idealista-zz-last"
refusal "the last container, behind a docker that reads stdin" "inside it, or holds it"
expect_untouched "mounts: docker reads stdin"
pass "a mount of the directory, inside it or above it, in any container, refuses"

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
# And the preflight is what stands before --apply, not only before --check.
new_world; printf 'false\n' >"$STUB/running"
run_move -- --apply "$DEST"
[ "$RC" = "1" ] && grep -q "NOT MOVED" "${W}/out" || fail "scenario 5: --apply went past a failed preflight (rc=${RC})"
printf 'true\n' >"$STUB/running"; expect_untouched "scenario 5 (--apply after a failed preflight)"
# Both locks are held while the preflight asks (#709 review, finding 1): of
# two --apply runs only one gets as far as its checks, and what they saw is
# what the move acts on. The preflight's children get neither descriptor; a
# --check takes no lock.
new_world; : >"$STUB/probe-locks"; : >"$STUB/probe-fds"
run_move -- --apply "$DEST"
expect_moved "scenario 5 (the locks during the preflight)"
[ "$(cat "$STUB/locks-at-preflight")" = "$(printf 'deploy held\nmerge held')" ] \
    || fail "scenario 5: during the --apply preflight: $(tr '\n' ',' <"$STUB/locks-at-preflight")"
[ -e "$STUB/fds-at-preflight" ] && [ ! -s "$STUB/fds-at-preflight" ] \
    || fail "scenario 5: a preflight child inherited the lock descriptors: $(cat "$STUB/fds-at-preflight" 2>/dev/null || echo 'not asked')"
new_world; : >"$STUB/probe-locks"
run_move -- --check "$DEST"
[ "$RC" = "0" ] && [ "$(cat "$STUB/locks-at-preflight")" = "$(printf 'deploy free\nmerge free')" ] \
    || fail "scenario 5: during --check (rc=${RC}): $(tr '\n' ',' <"$STUB/locks-at-preflight")"
pass "a deploy in progress, or a failed preflight, stops the move before it starts; --apply checks under both locks"

# --- 6. failures after the app is stopped: each ends on the original -------
recovered() {
    local label="$1"
    [ "$RC" = "1" ] || fail "${label}: exit ${RC}, expected 1 (recovered)"
    grep -q "^RECOVERED:" "${W}/out" || fail "${label}: no RECOVERED line"
    expect_original "$label"
    no_leak "$label"
}

# 6a. The stop succeeds and its confirmation fails (plan review, F4).
# Preflight asks once before the stop; only the probe AFTER the stop fails.
new_world; : >"$STUB/fail.inspect-stopped"
run_move -- --apply "$DEST"
recovered "scenario 6a (stop confirmed by a failing probe)"
grep -q "FAILED in phase 'stopping'" "${W}/out" || fail "scenario 6a: the failure was not attributed to the stopping phase"

# 6b. `compose stop` keeps failing: nothing to undo, and recovery stops nothing.
new_world; : >"$STUB/fail.stop"; : >"$STUB/keep.stop"
run_move -- --apply "$DEST"
recovered "scenario 6b (compose stop keeps failing)"
grep -q "nothing was moved" "${W}/out" || fail "scenario 6b: recovery did not recognise that nothing was moved"
[ "$(grep -c ' stop app' "${W}/docker.log")" = "1" ] \
    || fail "scenario 6b: recovery tried to stop an app that was never taken down"

cp_appends() {
    # cp_appends <file>: a cp that appends a line to <file> in the copy it made.
    printf '#!/bin/bash\n/bin/cp "$@" || exit $?\nfor last in "$@"; do :; done\n[ -f "$last/%s" ] && echo more >>"$last/%s"\nexit 0\n' "$1" "$1" >"${W}/bin/cp"
    chmod +x "${W}/bin/cp"
}

# 6c. The safety copy does not match (a state file changed under the copy).
new_world; cp_appends google_spend.jsonl
run_move -- --apply "$DEST"
recovered "scenario 6c (safety copy differs)"
grep -q "google_spend.jsonl" "${W}/out" || fail "scenario 6c: the differing file was not named"

# 6d. A log that grew during the copy is NOT a failed copy.
new_world; cp_appends autopilot-deploy.log
run_move -- --apply "$DEST"
[ "$RC" = "0" ] || fail "scenario 6d: a log appended during the copy failed the move (rc=${RC})"
grep -q "only logs differ" "${W}/out" || fail "scenario 6d: the tolerated difference was not reported"

# The rename is `python3 -c ...os.rename...`: a python3 stub for that alone.
REAL_PYTHON="$(command -v python3)"
rename_stub() {
    # rename_stub <shell run instead of the rename; "$@" are python's arguments>
    printf '#!/bin/bash\ncase "${1:-} ${2:-}" in\n"-c "*os.rename*)\n%s\n;;\nesac\nexec "%s" "$@"\n' "$1" "$REAL_PYTHON" >"${W}/bin/python3"
    chmod +x "${W}/bin/python3"
}

# 6e. The rename fails.
new_world
rename_stub 'exit 1'
run_move -- --apply "$DEST"
rm -f "${W}/bin/python3"
recovered "scenario 6e (rename fails)"

# 6f. The rename succeeds and the directory is then found changed (F1).
new_world
# Once: the directory that arrives is not the directory that left.
rename_stub '"'"$REAL_PYTHON"'" "$@" || exit $?
for last in "$@"; do :; done
if [ -f "$last/google_spend.jsonl" ] && [ ! -e "$STUB/tampered-once" ]; then
    : >"$STUB/tampered-once"
    : >"$last/.extra-after-rename"
fi
exit 0'
run_move -- --apply "$DEST"
rm -f "${W}/bin/python3"
# The directory renamed back is the one that arrived, the extra file and all.
rm "$REPO/data/.extra-after-rename" || fail "scenario 6f: the directory was not renamed back"
recovered "scenario 6f (the moved directory differs)"

# 6f2. Something makes <repo>/data again right after the rename (#711 review,
# :495): it is set aside, never taken for the state, and the state comes back.
new_world
rename_stub '"'"$REAL_PYTHON"'" "$@" || exit $?
[ -e "$STUB/made-once" ] || { : >"$STUB/made-once"; mkdir "'"$REPO"'/data" && echo theirs >"'"$REPO"'/data/x"; }
exit 0'
run_move -- --apply "$DEST"
rm -f "${W}/bin/python3"
recovered "scenario 6f2 (<repo>/data made again after the rename)"
aside="$(find "$BACKUPS" -maxdepth 1 -name 'checkout-data-at-rollback-*' -type d | head -n 1)"
[ -n "$aside" ] && [ "$(cat "$aside/x")" = "theirs" ] || fail "scenario 6f2: the directory made meanwhile was not kept aside"

# 6g. `compose up` fails after the switch.
new_world; : >"$STUB/fail.up"
run_move -- --apply "$DEST"
recovered "scenario 6g (compose up fails after the switch)"
aside="$(find "$BACKUPS" -maxdepth 1 -name 'checkout-data-at-rollback-*' -type d | head -n 1)"
[ -n "$aside" ] && [ -f "$aside/.moved-to" ] || fail "scenario 6g: the rebuilt directory was not kept aside"

# 6h. Healthy, and the page redirects: not accepted - nor once back ("incomplete").
new_world; printf '302' >"$STUB/page"
run_move -- --apply "$DEST"
grep -q "answered 302" "${W}/out" || fail "scenario 6h: a redirecting page was accepted as a working deployment (rc=${RC})"
[ "$RC" = "3" ] || fail "scenario 6h: exit ${RC}, expected 3 (the page still does not render)"
grep -q "^RECOVERY INCOMPLETE" "${W}/out" || fail "scenario 6h: a deployment whose page redirects was called recovered"
printf '200' >"$STUB/page"
expect_original "scenario 6h (page redirects after the switch)"
no_leak "scenario 6h"

# 6i. The container does not read the directory that was mounted.
new_world; : >"$STUB/stale"
run_move -- --apply "$DEST"
recovered "scenario 6i (the container reads another directory)"

# 6j. SIGTERM while `docker compose` hangs (F5; #709 review, finding 5): the
# trap runs at once, recovery ends the hung call by its PID and finishes. The
# test never releases the hang.
term_during_hang() {
    # term_during_hang <label>: --apply until the stub hangs, then SIGTERM; RC.
    env -u IDEALISTA_DATA_DIR PATH="${W}/bin:${PATH}" STUB="$STUB" STUB_REPO="$REPO" STUB_APP="idealista-app" STUB_MODE=write \
        MOVE_BACKUP_ROOT="$BACKUPS" MOVE_DEPLOY_LOCK="${W}/deploy.lock" MOVE_MERGE_LOCK="${W}/merge.lock" \
        MOVE_HEALTH_URL="http://127.0.0.1:1/api/healthz" MOVE_HEALTH_TIMEOUT=2 MOVE_HEALTH_POLL=1 MOVE_COMPOSE_LIMIT_S=2 \
        /bin/bash "$REPO/tools/move_data_dir.sh" --apply "$DEST" </dev/null >"${W}/out" 2>&1 &
    local mover=$! waited=0 pid
    for _ in $(seq 1 200); do [ -s "$STUB/hang.pid" ] && break; sleep 0.05; done
    [ -s "$STUB/hang.pid" ] || fail "$1: the move never reached the hung call"
    kill -TERM "$mover"
    while kill -0 "$mover" 2>/dev/null && [ "$waited" -lt 300 ]; do sleep 0.1; waited=$((waited + 1)); done
    if kill -0 "$mover" 2>/dev/null; then
        kill -KILL "$mover" $(cat "$STUB/hang.pid") 2>/dev/null || true
        wait "$mover" 2>/dev/null || true
        fail "$1: the move was still running 30 seconds after SIGTERM"
    fi
    RC=0; wait "$mover" || RC=$?
    for pid in $(cat "$STUB/hang.pid"); do
        ! kill -0 "$pid" 2>/dev/null || { kill -KILL "$pid"; fail "$1: the hung docker call outlived the move"; }
    done
}
for hung in stop:stopping up:starting; do
    new_world; : >"$STUB/hang.${hung%%:*}"
    term_during_hang "scenario 6j (${hung%%:*})"
    recovered "scenario 6j (SIGTERM during a hung ${hung%%:*})"
    grep -q "FAILED in phase '${hung#*:}' (exit 143)" "${W}/out" || fail "scenario 6j (${hung%%:*}): the signal was not what ended the move"
done
# 6j3. The same while the round-trip probe hangs (#711 review, :465): a
# $(docker exec) defers the trap until it returns, so it runs on the clock too.
new_world; : >"$STUB/hang.exec"
term_during_hang "scenario 6j3"
recovered "scenario 6j3 (SIGTERM during a hung probe)"

# 6j2. Docker stays hung (#711 review): recovery's own `compose stop` ends on
# MOVE_COMPOSE_LIMIT_S, and the move exits 3 with the commands.
new_world; : >"$STUB/hang.up"; : >"$STUB/hang.wedged"
term_during_hang "scenario 6j2"
[ "$RC" = "3" ] && grep -q "stop app' did not finish in 2 s" "${W}/out" && grep -q "Production may be DOWN" "${W}/out" \
    || fail "scenario 6j2: exit ${RC}, expected 3 with the manual commands after a recovery stop that hangs"
# The printed commands, run by hand here and after 6n's SIGKILL.
grep -E '^    (\[|mkdir|rmdir|mv|git) ' "${W}/out" | sed 's/^    //' >"${ROOT}/by-hand"
printed_in="$W"
by_hand() {
    sed "s|${printed_in}|${W}|g" "${ROOT}/by-hand" | bash -e >/dev/null 2>&1 \
        && [ "$(cat "$REPO/data/.last_seen_uid_properties")" = "4711" ] && [ ! -e "$DEST" ] \
        || fail "$1: the printed commands do not put the state back in <repo>/data"
}
by_hand "scenario 6j2 (the rebuilt directory)"

# 6k. `compose up` fails and leaves no container (#671 review): recovery read
# the failing inspect as "may be running".
new_world; : >"$STUB/fail.up-gone"
run_move -- --apply "$DEST"
recovered "scenario 6k (compose up leaves no container)"

# 6l. A directory appears at the destination after the preflight (#671
# review): `mv` nested the state in it. Now refused, empty or not, untouched.
for content in "" "theirs"; do
    new_world
    printf '#!/bin/bash\nmkdir -p "%s"\n[ -z "%s" ] || printf theirs >"%s/a-file"\nexec /bin/cp "$@"\n' "$DEST" "$content" "$DEST" >"${W}/bin/cp"
    chmod +x "${W}/bin/cp"
    run_move -- --apply "$DEST"
    [ "$RC" = "1" ] && grep -q "^RECOVERED:" "${W}/out" && grep -q "created after the preflight" "${W}/out" \
        || fail "scenario 6l (${content:-empty}): exit ${RC}, expected a recovered refusal"
    [ -d "$DEST" ] && [ "$(cat "$DEST/a-file" 2>/dev/null)" = "$content" ] && [ ! -e "$DEST/data" ] \
        || fail "scenario 6l (${content:-empty}): the directory that appeared was touched"
    rm -rf "$DEST"
    expect_original "scenario 6l (${content:-empty} directory at the destination)"
    no_leak "scenario 6l"
done

# 6m. The same between that check and the rename: rename(2) replaces an
# empty directory exactly, and fails on one with content, left alone.
new_world
rename_stub "/bin/mkdir -p '${DEST}'"
run_move -- --apply "$DEST"
rm -f "${W}/bin/python3"
expect_moved "scenario 6m (an empty directory appeared under the rename)"
[ ! -e "$DEST/data" ] || fail "scenario 6m: the state was moved inside the directory that appeared"

new_world
rename_stub "[ -e '${DEST}' ] || { /bin/mkdir -p '${DEST}' && printf theirs >'${DEST}/a-file'; }"
run_move -- --apply "$DEST"
rm -f "${W}/bin/python3"
[ "$RC" = "1" ] && grep -q "^RECOVERED:" "${W}/out" || fail "scenario 6m: exit ${RC}, expected a recovered failure"
[ "$(cat "$DEST/a-file")" = "theirs" ] && [ ! -e "$DEST/data" ] || fail "scenario 6m: the directory that appeared was touched"
rm -rf "$DEST"
expect_original "scenario 6m (a directory with content appeared under the rename)"

# 6n. SIGKILL runs no trap (#671 review): what it leaves is the state whole in
# one directory, and .env naming that directory. Undoing it is by hand in this
# version.
killed_at() {
    # killed_at <program> <argument>: that call SIGKILLs the script, its parent.
    printf '#!/bin/bash\nfor arg in "$@"; do [ "$arg" != "%s" ] || { kill -KILL "$PPID"; exit 0; }; done\nexec /bin/%s "$@"\n' "$2" "$1" >"${W}/bin/$1"
    chmod +x "${W}/bin/$1"
}
# 6n. Killed right after the rename, before <repo>/data is rebuilt.
new_world
killed_at mkdir "$REPO/data"
run_move -- --apply "$DEST"
rm -f "${W}/bin/mkdir"
[ "$RC" = "137" ] || fail "scenario 6n: exit ${RC}, expected 137 (killed)"
[ ! -e "$REPO/data" ] && [ "$(cat "$DEST/.last_seen_uid_properties")" = "4711" ] && [ "$(cat "$STUB/running")" = "false" ] \
    || fail "scenario 6n: the kill did not land between the rename and the rebuild"
[ "$(sed -n 's/^IDEALISTA_DATA_DIR=//p' "$REPO/.env")" = "$DEST" ] \
    || fail "scenario 6n: after the kill .env does not name the directory that holds the state"
by_hand "scenario 6n (killed before the rebuild)"
# 6n2. And when something made <repo>/data again since (#711 review, :495).
new_world
killed_at mkdir "$REPO/data"
run_move -- --apply "$DEST"
rm -f "${W}/bin/mkdir"
mkdir "$REPO/data"; echo theirs >"$REPO/data/x"
by_hand "scenario 6n2 (killed before the rebuild, <repo>/data made again)"
rolled_back() {
    local label="$1"
    [ "$RC" = "0" ] || fail "${label}: --rollback exit ${RC}"
    grep -q "^ROLLED BACK:" "${W}/out" || fail "${label}: no ROLLED BACK line"
    expect_original "$label"
    no_leak "$label"
}
# 6n3. The same kill, undone by --rollback.
new_world
killed_at mkdir "$REPO/data"
run_move -- --apply "$DEST"
rm -f "${W}/bin/mkdir"
[ "$RC" = "137" ] && [ ! -e "$REPO/data" ] || fail "scenario 6n3: exit ${RC}, or the kill did not land before the rebuild"
run_move -- --rollback
rolled_back "scenario 6n3 (SIGKILL after the rename, then --rollback)"

# 6o. Killed right after the stop, before anything is renamed.
new_world
killed_at cp "$REPO/data"
run_move -- --apply "$DEST"
rm -f "${W}/bin/cp"
[ "$RC" = "137" ] || fail "scenario 6o: exit ${RC}, expected 137 (killed)"
[ -d "$REPO/data" ] && [ ! -e "$DEST" ] && [ "$(cat "$STUB/running")" = "false" ] \
    || fail "scenario 6o: the kill did not land between the stop and the rename"
run_move -- --rollback
grep -q "no rename to undo" "${W}/out" || fail "scenario 6o: --rollback did not recognise a move that never renamed"
rolled_back "scenario 6o (SIGKILL after the stop, then --rollback)"

# 6q. Killed between `mkdir <repo>/data` and its tombstone: an empty directory.
new_world
printf '#!/bin/bash\nfor arg in "$@"; do [ "$arg" != "%s" ] || { kill -KILL "$PPID"; exit 0; }; done\nexec "%s" "$@"\n' "$REPO/data/.moved-to" "$REAL_PYTHON" >"${W}/bin/python3"
chmod +x "${W}/bin/python3"
run_move -- --apply "$DEST"
rm -f "${W}/bin/python3"
[ "$RC" = "137" ] && [ -d "$REPO/data" ] && [ -z "$(ls -A "$REPO/data")" ] || fail "scenario 6q: exit ${RC}, or the kill did not leave an empty <repo>/data"
run_move -- --rollback
rolled_back "scenario 6q (SIGKILL before the tombstone, then --rollback)"

# 6r. Nothing to undo and the app down: started, not "already rolled back".
new_world; printf 'false\n' >"$STUB/running"
run_move -- --rollback
rolled_back "scenario 6r (nothing to undo, app stopped)"

# 6p. --rollback killed right after its rename back (#709 review, finding 4):
# a merge changed a reference file meanwhile, and a newer .orchestrator sits in
# the rebuilt directory set aside. The second --rollback restores both, alone.
new_world
run_move -- --apply "$DEST"
[ "$RC" = "0" ] || fail "scenario 6p: the move that sets the scene failed (exit ${RC})"
printf '{"changed":1}\n' >"$REPO/data/ine_municipal.json"
git -C "$REPO" add data/ine_municipal.json
git -C "$REPO" commit --quiet -m "a merge"
printf 'session=local_newer\n' >"$REPO/data/.orchestrator"
# One mtime, two contents (#712 review, finding 3): decided by content.
touch -t 202001010000 "$REPO/data/.orchestrator" "$DEST/.orchestrator"
printf '#!/bin/bash\nfor arg in "$@"; do [ "$arg" != "checkout" ] || { kill -KILL "$PPID"; exit 0; }; done\nexec "%s" "$@"\n' "$(command -v git)" >"${W}/bin/git"
chmod +x "${W}/bin/git"
run_move -- --rollback
rm -f "${W}/bin/git"
[ "$RC" = "137" ] && [ -f "$REPO/data/.last_seen_uid_properties" ] && [ ! -e "$DEST" ] && grep -q '^IDEALISTA_DATA_DIR=' "$REPO/.env" \
    || fail "scenario 6p: exit ${RC}, or the kill did not land between the rename back and the .env line"
run_move -- --rollback
[ "$RC" = "0" ] && grep -q "^ROLLED BACK:" "${W}/out" || fail "scenario 6p: the second --rollback: exit ${RC}"
[ "$(cat "$STUB/running")" = "true" ] && [ "$(cat "$STUB/mount")" = "$REPO/data" ] && ! grep -q '^IDEALISTA_DATA_DIR=' "$REPO/.env" \
    || fail "scenario 6p: the second --rollback did not finish the first"
[ "$(cat "$REPO/data/ine_municipal.json")" = '{"changed":1}' ] && [ -z "$(git -C "$REPO" status --porcelain)" ] \
    || fail "scenario 6p: the second --rollback left the reference files as the moved directory had them"
[ "$(cat "$REPO/data/.orchestrator")" = "session=local_newer" ] || fail "scenario 6p: the newer .orchestrator was lost"
# A later --rollback (app stopped) keeps a .orchestrator written since.
printf 'session=local_later\n' >"$REPO/data/.orchestrator"
printf 'false\n' >"$STUB/running"
run_move -- --rollback
[ "$RC" = "0" ] && [ "$(cat "$REPO/data/.orchestrator")" = "session=local_later" ] \
    || fail "scenario 6p: a later --rollback put back the .orchestrator of an aside already restored (exit ${RC})"
# 6q. The same kill with identical .orchestrator copies (#712 review 2,
# finding 3): the second --rollback copies nothing, and marks the aside all
# the same, so a later one keeps a .orchestrator written since.
new_world
run_move -- --apply "$DEST"
[ "$RC" = "0" ] || fail "scenario 6q: the move that sets the scene failed (exit ${RC})"
cp -p "$DEST/.orchestrator" "$REPO/data/.orchestrator"
printf '#!/bin/bash\nfor arg in "$@"; do [ "$arg" != "checkout" ] || { kill -KILL "$PPID"; exit 0; }; done\nexec "%s" "$@"\n' "$(command -v git)" >"${W}/bin/git"
chmod +x "${W}/bin/git"
run_move -- --rollback
rm -f "${W}/bin/git"
[ "$RC" = "137" ] && [ ! -e "$DEST" ] || fail "scenario 6q: exit ${RC}, or the kill did not land after the rename back"
run_move -- --rollback
[ "$RC" = "0" ] && grep -q "^ROLLED BACK:" "${W}/out" || fail "scenario 6q: the second --rollback: exit ${RC}"
printf 'session=local_later\n' >"$REPO/data/.orchestrator"
printf 'false\n' >"$STUB/running"
run_move -- --rollback
[ "$RC" = "0" ] && [ "$(cat "$REPO/data/.orchestrator")" = "session=local_later" ] \
    || fail "scenario 6q: a later --rollback put back an identical aside copy over a newer .orchestrator (exit ${RC})"

pass "a failure at every step after the stop ends with the app running on the original directory"

# --- 7. recovery that cannot finish says so, with the commands -------------
new_world; : >"$STUB/fail.up"; : >"$STUB/keep.up"
run_move -- --apply "$DEST"
[ "$RC" = "3" ] || fail "scenario 7: exit ${RC}, expected 3 (recovery incomplete)"
grep -q "^RECOVERY INCOMPLETE" "${W}/out" || fail "scenario 7: no RECOVERY INCOMPLETE line"
grep -q "docker compose -f docker-compose.yml up -d --no-build app" "${W}/out" || fail "scenario 7: the manual commands were not printed"
# The data is back where it was even though the app could not be started.
[ "$(cat "$REPO/data/.last_seen_uid_properties")" = "4711" ] && [ ! -e "$DEST" ] \
    || fail "scenario 7: the state was not returned before the start failed"
grep -q '^IDEALISTA_DATA_DIR=' "$REPO/.env" && fail "scenario 7: .env still names the variable"
pass "a recovery that cannot start the app returns the data, exits 3 and prints the manual commands"

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
