#!/bin/bash
# May the deployment's state directory be moved out of the git working copy?
# (#639)
#
# On the machine that runs the app, `<repo>/data` is bound at /app/data: the
# mail cursor, the Google spend ledger, attachments, score snapshots, the
# in-flight markers, the watcher's own markers. It is also a directory inside a
# checkout where sessions run git and pytest. Moving it to a directory outside
# the repository means pointing the one variable that names it,
# IDEALISTA_DATA_DIR in `<repo>/.env`, at the new place - with the app stopped
# for about a minute.
#
#   move_data_dir.sh --check  /absolute/destination   # read-only: may it run?
#   move_data_dir.sh --apply  /absolute/destination   # do it (owner's command)
#   move_data_dir.sh --rollback                       # move it back
#
# `--check` (the default) only reads. `--apply` is one rename(2) on the same
# volume after a checksummed safety copy. `--rollback` finds the directory by
# .env (or the tombstone), refuses before it stops anything, and renames it
# back; it is idempotent, so a second run finishes what a killed first left.
# It exits 4 when the app is back on <repo>/data but the marker that keeps a
# set-aside .orchestrator from being restored twice could not be written.
#
# `--apply` takes the deploy and merge locks BEFORE its preflight, so a second
# `--apply` stops at the lock or, once the first is done, at the preflight -
# never on checks read before the first one moved anything.
#
# Once the app is stopped, every exit but success (a failed step, Ctrl-C, a
# closed terminal, `kill`) runs `recover`: by what is on disk, idempotently,
# back to <repo>/data with the app started. SIGKILL runs no trap: the `.env`
# line naming the destination is written BEFORE the stop, and what is left is
# undone by hand with the commands `recover` prints.
#
# `docker compose` runs without IDEALISTA_DATA_DIR: only `.env` decides.

set -euo pipefail

# `git status` refreshes the index and writes it back when a tracked file's
# timestamps changed - an optional lock. A question must not write: off, for
# every git this script and the tools it runs start.
export GIT_OPTIONAL_LOCKS=0

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
REPO="${MOVE_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd -P)}"
LIB_DIR="${SCRIPT_DIR}/autopilot/lib"

KEY="IDEALISTA_DATA_DIR"
TOMBSTONE=".moved-to"
BRANCH="${MOVE_BRANCH:-main}"
COMPOSE_FILE="${MOVE_COMPOSE_FILE:-docker-compose.yml}"
APP="${MOVE_APP_CONTAINER:-${COMPOSE_CONTAINER_PREFIX:-idealista}-app}"
CONTAINER_STATE="/app/data"
HEALTH_URL="${MOVE_HEALTH_URL:-http://127.0.0.1:5001/api/healthz}"
HEALTH_TIMEOUT="${MOVE_HEALTH_TIMEOUT:-180}"
HEALTH_POLL="${MOVE_HEALTH_POLL:-5}"
# Recovery ignores signals, so a clock ends each of its `docker compose` calls.
COMPOSE_LIMIT_S="${MOVE_COMPOSE_LIMIT_S:-120}"
BACKUP_ROOT="${MOVE_BACKUP_ROOT:-${HOME}/IdealistaRank-backups}"
# The watcher's and the merge bot's lock files, held for the whole move: no
# tick builds and no merge pass writes while the state is in two places.
DEPLOY_LOCK="${MOVE_DEPLOY_LOCK:-/tmp/idealista-autopilot-deploy.lock.d}"
MERGE_LOCK="${MOVE_MERGE_LOCK:-/tmp/idealista-autopilot-merge.lock.d}"
STATUS_CMD="${MOVE_STATUS_CMD:-${REPO}/tools/backfill_status.sh}"
# The four committed reference files (docs/rules/reference-data.md). They stay
# tracked in <repo>/data and the image carries its own copy.
REFERENCE_FILES="ine_municipal.json hospitals_cnh.json sepe_unemployment.json top_agencies.json"

# The directory inside the checkout, from the one file that names it.
SRC="$(/bin/bash "${LIB_DIR}/data_dir.sh" --default "$REPO")" || {
    printf 'cannot read %s/data_dir.sh - is this the repository that carries it?\n' "$LIB_DIR" >&2
    exit 2
}
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PHASE="preflight"
DEST=""
ASIDE=""
WORK=""
# --apply: "0" until its rename succeeded, "1" after.
RENAMED=""
# --rollback: "1" when .env names nothing and <repo>/data carries no tombstone.
NOTHING_TO_UNDO=""
# The set-aside directory whose .orchestrator.restored marker could not be
# written, if any: the rollback then ends with exit 4, never 0.
MARK_FAILED=""
# The `docker compose` call --apply is waiting on, while it runs.
BG_PID=""

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage:
  move_data_dir.sh [--check] /absolute/destination   read-only: may the move run?
  move_data_dir.sh --apply   /absolute/destination   move the state directory
  move_data_dir.sh --rollback                        move it back into the checkout
EOF
}

# --- small readers ----------------------------------------------------------

# Never from stdin: the readers below run inside `while read` loops fed by a
# here-string, and a command that read stdin would eat the rest of the list.
# Closed for every child: a helper that outlived this script would hold the
# deploy lock with nobody behind it, and every later watcher tick would skip.
dk() { docker "$@" </dev/null 8>&- 9>&-; }

# The same call, started in the background and waited for. Bash runs a trap
# only once a foreground command returns: a `compose up` that hangs would hold
# SIGTERM back for ever. `wait` returns on a trapped signal at once, and
# `recover` ends the call by its saved PID (`exec`: the PID is docker's own).
compose_waited() {
    local rc=0
    ( cd "$REPO" && exec env -u "$KEY" docker compose -f "$COMPOSE_FILE" "$@" ) </dev/null 8>&- 9>&- &
    BG_PID=$!
    wait "$BG_PID" || rc=$?
    BG_PID=""
    return "$rc"
}

# TERM, ten seconds, then KILL: recovery must not wait on it either.
end_waited() {
    local waited=0
    [ -n "$BG_PID" ] || return 0
    kill -TERM "$BG_PID" 2>/dev/null || true
    while kill -0 "$BG_PID" 2>/dev/null && [ "$waited" -lt 100 ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    kill -KILL "$BG_PID" 2>/dev/null || true
    wait "$BG_PID" 2>/dev/null || true
    note "ended the interrupted call (pid ${BG_PID})"
    BG_PID=""
}

# run_bounded <label> <stdout file> <command...>: in the background, polled,
# 124 once COMPOSE_LIMIT_S has passed. Polling, not `wait`: recovery ignores
# signals, and a probe under a trapped signal must not hang either.
run_bounded() {
    local label="$1" out="$2" rc=0 waited=0
    shift 2
    "$@" </dev/null >"$out" 2>/dev/null 8>&- 9>&- &
    BG_PID=$!
    while kill -0 "$BG_PID" 2>/dev/null && [ "$waited" -lt $((COMPOSE_LIMIT_S * 10)) ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    if kill -0 "$BG_PID" 2>/dev/null; then
        say "ERROR: '${label}' did not finish in ${COMPOSE_LIMIT_S} s"
        end_waited
        return 124
    fi
    wait "$BG_PID" || rc=$?
    BG_PID=""
    return "$rc"
}
compose_in_repo() { cd "$REPO" && exec env -u "$KEY" docker compose -f "$COMPOSE_FILE" "$@"; }
compose_bounded() { run_bounded "docker compose $*" /dev/null compose_in_repo "$@"; }
# A bounded call's stdout, for $(...); its status, or 124.
bounded_out() {
    local label="$1" tmp rc=0
    shift
    tmp="$(mktemp "${TMPDIR:-/tmp}/idealista-move-out.XXXXXX")" || return 1
    run_bounded "$label" "$tmp" "$@" >&2 || rc=$?
    cat "$tmp"
    rm -f "$tmp"
    return "$rc"
}

# 0 running, 1 stopped, 2 could not tell. "Could not tell" is its own answer:
# a probe that failed must never read as "stopped".
app_running() {
    local out names
    if ! out="$(dk inspect -f '{{.State.Running}}' "$APP" 2>/dev/null)"; then
        # inspect fails the same way for a daemon that does not answer and
        # for a container that does not exist - which is what a `compose up`
        # that failed while replacing it leaves. A container that is not
        # there is not running: said only when the daemon's own list was
        # read and does not have the name.
        names="$(dk ps -a --format '{{.Names}}' 2>/dev/null)" || return 2
        if printf '%s\n' "$names" | grep -Fx -- "$APP" >/dev/null; then
            return 2
        fi
        return 1
    fi
    case "$out" in
        true) return 0 ;;
        false) return 1 ;;
        *) return 2 ;;
    esac
}

mount_source() {
    dk inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' "$APP" 2>/dev/null
}

phys() { ( cd "$1" 2>/dev/null && pwd -P ); }

# One container's mounts from `{{json .Mounts}}` on stdin: per mount the word
# "mount", then Type, Name, Destination and Source, each NUL-terminated, and
# "end" once all were read. A path may hold any character but NUL; a list
# that stops before "end" was not read.
MOUNTS_PY='import json, sys
for m in json.load(sys.stdin) or []:
    f = ["mount"] + [m.get(k) or "" for k in ("Type", "Name", "Destination", "Source")]
    if any("\0" in v for v in f):
        sys.exit(1)
    sys.stdout.write("".join(v + "\0" for v in f))
sys.stdout.write("end\0")'

# What is wrong with the reference files the app reads, or nothing. The
# directory is the container's own REFERENCE_DATA_DIR (that one variable is
# read, no other). Two looks, and both must pass:
#   * the image: a throwaway container of the same image with nothing mounted
#     (an exec would look through the state mount), as the user the app runs
#     as, read-only, without a network, and only for an image that declares no
#     VOLUME (a run would create an anonymous volume). The probe walks the
#     directory's and every file's path one component and one link hop at a
#     time - `readlink` without -f, at most 40 hops - and refuses when any
#     component, any parent a link passes through, or any intermediate link
#     lies under the state directory: the mount hides each of them;
#   * the app as it runs (`docker exec`, its own environment and user): a
#     mount over the directory - an empty bind of something else - hides the
#     image's copy even though the image carries it.
# The spelling is normalised (`/app/./data`, `//app/data`). The value is read
# up to a "." docker prints after it, so a trailing newline is not stripped by
# $(...); any control character refuses.
reference_gap() {
    local image raw dir norm user volumes found rc=0
    local -a as=()
    image="$(dk inspect -f '{{.Image}}' "$APP" 2>/dev/null)" && [ -n "$image" ] \
        || { say "docker could not say which image ${APP} runs"; return; }
    raw="$(dk inspect -f '{{range .Config.Env}}{{if ge (len .) 19}}{{if eq (slice . 0 19) "REFERENCE_DATA_DIR="}}{{slice . 19}}{{end}}{{end}}{{end}}.' "$APP" 2>/dev/null)" \
        && [ "${raw%.}" != "$raw" ] || { say "docker could not say where ${APP} reads its reference files"; return; }
    dir="${raw%.}"
    case "$dir" in
        *[[:cntrl:]]*) say "the REFERENCE_DATA_DIR of ${APP} holds a control character or a newline; it is not checked"; return ;;
    esac
    [ -n "$dir" ] || { say "${APP} has no REFERENCE_DATA_DIR, so it reads its reference files from ${CONTAINER_STATE}"; return; }
    norm="$(python3 -c 'import posixpath, sys; p = posixpath.normpath(sys.argv[1]); print("/" + p.lstrip("/") if p[:1] == "/" else p)' "$dir" 2>/dev/null)" \
        || { say "the REFERENCE_DATA_DIR of ${APP} (${dir}) could not be read as a path"; return; }
    case "$norm" in
        "$CONTAINER_STATE" | "$CONTAINER_STATE"/* | [!/]*) say "${APP} reads its reference files from ${dir}, not from outside ${CONTAINER_STATE}"; return ;;
    esac
    # Compose `user:` lands here; empty means the image's own USER, which is
    # also what a run without --user gets.
    user="$(dk inspect -f '{{.Config.User}}' "$APP" 2>/dev/null)" \
        || { say "docker could not say which user ${APP} runs as"; return; }
    [ -z "$user" ] || as=(--user "$user")
    volumes="$(dk image inspect -f '{{json .Config.Volumes}}' "$image" 2>/dev/null)" \
        || { say "docker could not say whether the image of ${APP} declares volumes"; return; }
    case "$volumes" in
        null | "{}") ;;
        *) say "the image of ${APP} declares volumes (${volumes}); a probe container would create one, so its reference files were not looked for"; return ;;
    esac
    # shellcheck disable=SC2016,SC2086
    found="$(bounded_out "docker run ${image}" docker run --rm --read-only --network none ${as[@]+"${as[@]}"} --entrypoint sh -e "REFERENCE_DATA_DIR=${dir}" "$image" \
        -c 'state=$1; shift; cd -- "$REFERENCE_DATA_DIR" 2>/dev/null || exit 3
w() { rest=$1; cur=; k=0
while :; do rest=${rest#"${rest%%[!/]*}"}; [ -n "$rest" ] || return 0
c=${rest%%/*}; case $rest in */*) rest=${rest#*/} ;; *) rest= ;; esac
case $c in .) continue ;; ..) cur=${cur%/*}; continue ;; esac
p=$cur/$c; case "$p/" in "$state"/*) p=$p${rest:+/$rest}; return 5 ;; esac
[ -L "$p" ] || { cur=$p; continue; }
k=$((k + 1)); [ "$k" -le 40 ] || return 4
t=$(readlink -- "$p" && printf .) || return 4; t=${t%?.}
case $t in /*) cur= ;; esac; rest=$t${rest:+/$rest}; done; }
w "$REFERENCE_DATA_DIR" || { e=$?; [ "$e" = 5 ] || exit 6; printf %s "$p"; exit 2; }
for n; do test -r "$n" || { printf %s "$n"; exit 1; }
w "$REFERENCE_DATA_DIR/$n" || { e=$?; [ "$e" = 5 ] || { printf %s "$n"; exit 4; }; printf "%s -> %s" "$n" "$p"; exit 5; }; done' sh "$CONTAINER_STATE" $REFERENCE_FILES)" || rc=$?
    case "$rc:${found:+set}" in
        0:*) live_reference_gap "$dir" ;;
        1:set) say "the image of ${APP} does not carry ${found} under ${dir} readable by ${user:-its default user}; deploy the build that does first" ;;
        2:set) say "${APP} reads its reference files from ${dir}, which is ${found} in the image, not from outside ${CONTAINER_STATE}" ;;
        3:*) say "the image of ${APP} has no directory ${dir} that ${user:-its default user} can enter" ;;
        4:set) say "the image of ${APP} has ${found} under ${dir}, but its link could not be resolved" ;;
        5:set) say "the image of ${APP} links ${found} under ${CONTAINER_STATE}, which the state mount hides from the app" ;;
        6:*) say "the image of ${APP} has a link on the way to ${dir} that could not be resolved" ;;
        *) say "docker could not run the image of ${APP} to look for its reference files (exit ${rc})" ;;
    esac
}

# The second look: can the app, as it runs now, read every reference file in
# its directory? `docker exec` with the container's own environment and user;
# a read and nothing else.
live_reference_gap() {
    local found rc=0
    # shellcheck disable=SC2016,SC2086
    found="$(bounded_out "docker exec ${APP} sh" docker exec "$APP" sh -c 'cd -- "$REFERENCE_DATA_DIR" 2>/dev/null || exit 3
for n; do test -r "$n" || { printf %s "$n"; exit 1; }; done' sh $REFERENCE_FILES)" || rc=$?
    case "$rc:${found:+set}" in
        0:*) ;;
        1:set) say "${APP} as it runs now cannot read ${found} under ${1}, although its image carries it: a mount hides it" ;;
        3:*) say "${APP} as it runs now has no directory ${1} it can enter, although its image has" ;;
        *) say "docker could not look into ${APP} for its reference files (exit ${rc})" ;;
    esac
}

device_of() {
    python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_dev)' "$1" 2>/dev/null
}

# Where the filesystem a path is on is mounted (`df -P`, the last column). Two
# directories of one device can still be two mounts - a bind mount of a
# subdirectory on Linux - and a rename across them fails with EXDEV.
mount_point_of() {
    df -P "$1" 2>/dev/null | awk 'NR == 2 { for (i = 6; i <= NF; i++) printf "%s%s", $i, (i < NF ? " " : "\n") }'
}

# Does a container's mount source lie inside the state directory, or hold it?
# `yes` or `no`, by real paths. Docker Desktop spells a host path
# /host_mnt/<path> in places: both spellings are asked.
overlaps_state() {
    python3 -c 'import os, sys
state = os.path.realpath(sys.argv[1])
sources = {sys.argv[2]}
if sys.argv[2].startswith("/host_mnt/"):
    sources.add(sys.argv[2][len("/host_mnt"):])
hit = False
for source in sources:
    source = os.path.realpath(source)
    inside = source.startswith(state.rstrip("/") + "/")
    holds = state.startswith(source.rstrip("/") + "/")
    hit = hit or source == state or inside or holds
print("yes" if hit else "no")' "${2:-$SRC}" "$1" </dev/null 2>/dev/null
}

# rename(2) and nothing else: `mv` onto a directory that exists moves the
# source INSIDE it. A rename replaces an empty directory and fails otherwise.
rename_exact() {
    python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$1" "$2" 2>/dev/null
}

env_line_count() {
    if [ ! -f "${REPO}/.env" ]; then
        printf '0\n'
        return 0
    fi
    grep -c "^${KEY}=" "${REPO}/.env" || true
}

# The value the one reader gives, with this process's own export out of the
# way: the question is what `.env` says.
resolved_data_dir() {
    env -u "$KEY" /bin/bash "${LIB_DIR}/data_dir.sh" "$REPO"
}

# The tombstone is the FACT that an entry of that name exists - a file, a
# directory, a FIFO, a link whose target is gone - read the same way by
# lib/data_dir.sh and app.py. Never decided by reading it.
has_tombstone() {
    [ -e "${1}/${TOMBSTONE}" ] || [ -L "${1}/${TOMBSTONE}" ]
}

# The moved_to= line of a tombstone that is a regular file, else nothing; exit
# 2 when that line is not exactly what step 4 writes (an absolute plain path,
# no trailing slash): it is never trimmed into a guess.
tombstone_destination() {
    python3 - "${1}/${TOMBSTONE}" 2>/dev/null <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    if not stat.S_ISREG(os.lstat(path).st_mode):
        sys.exit(0)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            sys.exit(0)
        raw = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
except OSError:
    sys.exit(0)
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError:
    sys.exit(2)
for line in text.split("\n"):
    if line.startswith("moved_to="):
        value = line[len("moved_to="):]
        plain = all(ch.isprintable() and not ch.isspace() and ch not in "\"'#$\\" for ch in value)
        if not (plain and value.startswith("/") and (value == "/" or not value.endswith("/")) and len(value) <= 512):
            sys.exit(2)
        print(value)
        break
PY
}

# The state, the destination and the backup root: no one inside another.
refuse_overlap() {
    [ -z "$1" ] || [ -z "$2" ] || [ "$(overlaps_state "$1" "$2")" = "no" ] \
        || problem "${1} and ${2}: one holds the other; the destination and MOVE_BACKUP_ROOT go beside ${SRC}, not in it"
}

# Write or remove the one line, never printing `.env`: same-mode temp, fsync,
# atomic rename (a half-written `.env` stops the deployment).
set_env_line() {
    python3 - "${REPO}/.env" "$KEY" "${1:-}" <<'PY'
import os
import stat
import sys
import tempfile

path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "rb") as handle:
    lines = handle.read().splitlines(keepends=True)
prefix = (key + "=").encode()
kept = [line for line in lines if not line.startswith(prefix)]
if kept and not kept[-1].endswith(b"\n"):
    kept[-1] += b"\n"
if value:
    kept.append(prefix + value.encode() + b"\n")
mode = stat.S_IMODE(os.stat(path).st_mode)
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "wb") as handle:
        handle.writelines(kept)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except OSError:
        pass
    raise
PY
}

# Every regular file's sha256, plus the name of everything that is not one, so
# an empty directory that went missing is a difference too.
manifest() {
    ( cd "$1" && {
        find . -type f -exec shasum -a 256 {} +
        find . ! -type f -print | sed 's/^/entry  /'
    } | LC_ALL=C sort ) >"$2"
}

# Equal, or different only in `*.log` files: host processes this script does
# not stop append to logs here. Anything else that differs is a failed copy.
manifests_agree() {
    local first="$1" second="$2" label="$3" differing
    if cmp -s "$first" "$second"; then
        return 0
    fi
    differing="$(diff "$first" "$second" | grep '^[<>]' | sed 's/^[<>] *[^ ]*  *//' | LC_ALL=C sort -u || true)"
    if [ -z "$differing" ]; then
        return 0
    fi
    if printf '%s\n' "$differing" | grep -v '\.log$' >/dev/null; then
        say "ERROR: ${label}: these entries differ and are not logs:"
        printf '%s\n' "$differing" | grep -v '\.log$' | sed 's/^/    /'
        return 1
    fi
    note "${label}: only logs differ (a host process appended meanwhile):"
    printf '%s\n' "$differing" | sed 's/^/      /'
    return 0
}

take_lock() {
    # take_lock <fd> <file>: flock(2), the same primitive lib/lock.sh uses, on
    # a descriptor this shell keeps open until it exits.
    # shellcheck disable=SC2034  # `file` is read by the eval below
    local fd="$1" file="$2"
    eval "exec ${fd}>\"\$file\"" || return 1
    python3 -c 'import fcntl, sys
try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' "$fd"
}

take_both_locks() {
    if ! take_lock 9 "$DEPLOY_LOCK"; then
        say "STOP: the deploy watcher holds ${DEPLOY_LOCK} - a deploy is in progress. Nothing was touched."
        exit 1
    fi
    if ! take_lock 8 "$MERGE_LOCK"; then
        say "STOP: the merge bot holds ${MERGE_LOCK} - a merge pass is in progress. Nothing was touched."
        exit 1
    fi
    note "holding the deploy lock and the merge lock until this script exits"
}

# --- is the app serving from the directory it should? ------------------------

verify_serving() {
    # verify_serving <expected host directory>
    local expected="$1" source waited=0 url token probe seen gap
    source="$(mount_source)" || true
    if [ -z "$source" ] || [ "$(phys "$source")" != "$(phys "$expected")" ]; then
        say "ERROR: ${APP} mounts '${source:-nothing}' at ${CONTAINER_STATE}, expected ${expected}"
        return 1
    fi
    note "mount: ${source} -> ${CONTAINER_STATE}"

    until curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
        waited=$((waited + HEALTH_POLL))
        if [ "$waited" -gt "$HEALTH_TIMEOUT" ]; then
            say "ERROR: ${HEALTH_URL} did not answer within ${HEALTH_TIMEOUT}s"
            return 1
        fi
        sleep "$HEALTH_POLL"
    done
    note "healthz: ok"

    # A green healthz is not acceptance (lib/render_check.sh): a page that
    # renders a template must answer 200, and a 3xx is the failure looked for.
    # shellcheck disable=SC1091
    . "${LIB_DIR}/render_check.sh"
    url="$(deploy_render_url "$(deploy_render_origin "$HEALTH_URL")")"
    if [ -z "$url" ]; then
        say "ERROR: the page check is switched off (DEPLOY_RENDER_PATH is empty); a move cannot be called verified without it"
        return 1
    fi
    if ! deploy_render_ok "$url"; then
        say "ERROR: ${url} answered ${DEPLOY_RENDER_STATUS:-nothing}, not 200"
        return 1
    fi
    note "page: ${url} -> 200"

    # A token written here on the host is read back through the container:
    # stronger than the mail cursor, which the scheduler may advance at once.
    token="move-probe-${STAMP}-$$"
    probe="${expected}/.move-probe-${STAMP}"
    printf '%s\n' "$token" >"$probe"
    seen="$(bounded_out "docker exec ${APP} cat" docker exec "$APP" cat "${CONTAINER_STATE}/.move-probe-${STAMP}")" || true
    rm -f "$probe"
    if [ "$seen" != "$token" ]; then
        say "ERROR: a file written to ${expected} on the host is not what ${APP} reads at ${CONTAINER_STATE}"
        return 1
    fi
    note "the container reads the host directory (probe round-trip)"

    gap="$(reference_gap)"
    if [ -n "$gap" ]; then
        say "ERROR: ${gap}"
        return 1
    fi
    note "reference files: in the image, outside ${CONTAINER_STATE}"
    return 0
}

# --- back to the directory inside the checkout -------------------------------

manual_recovery() {
    local back=""
    if [ "$RENAMED" = "1" ] || [ "$MODE" = "rollback" ]; then
        back="    mkdir -p ${BACKUP_ROOT}
    [ ! -d ${DEST:-DESTINATION} ] || [ ! -e ${SRC} ] || rmdir ${SRC} 2>/dev/null || mv ${SRC} ${BACKUP_ROOT}/checkout-data-at-rollback-${STAMP}
    [ ! -d ${DEST:-DESTINATION} ] || mv ${DEST:-DESTINATION} ${SRC}
"
    fi
    cat <<EOF

  Production may be DOWN. Nothing below was done for you; run it by hand, in
  this order, and stop at the first command that fails:

    cd ${REPO}
    env -u ${KEY} docker compose -f ${COMPOSE_FILE} stop app
${back}    [ -d ${SRC} -a ! -e ${SRC}/${TOMBSTONE} ]    # must succeed: the state is back
    git -C ${REPO} checkout -- data
    # then remove the line '${KEY}=...' from ${REPO}/.env
    env -u ${KEY} docker compose -f ${COMPOSE_FILE} up -d --no-build app
    curl -s -o /dev/null -w '%{http_code}\n' ${HEALTH_URL%/api/healthz}/properties   # must print 200

  A safety copy from before the move, if taken, is under
  ${BACKUP_ROOT}/data-pre-move-* (older than anything written since).
EOF
}

# The tracked reference files as HEAD has them (a merge may have updated one
# while the old copies sat in the moved directory), and the .orchestrator the
# rebuilt directory got meanwhile: this run's aside, or a killed rollback's -
# by content, never by mtime, and a killed rollback's only until it is restored
# once (the marker), so a later --rollback keeps a newer one.
mark_orchestrator_restored() {
    if ! : >"${1}/.orchestrator.restored"; then
        MARK_FAILED="$1"
        note "ERROR: could not write ${1}/.orchestrator.restored; a later --rollback would put that copy over a newer .orchestrator"
    fi
}

# The newest directory a rollback set aside, or nothing.
last_aside() {
    local last aside=""
    for last in "${BACKUP_ROOT}"/checkout-data-at-rollback-*; do [ -d "$last" ] && aside="$last"; done
    printf '%s' "$aside"
}

# The state directory, not a directory that merely holds files (a .DS_Store, the
# four reference files): --apply refuses to move one without a .deployed_sha,
# so every state it moved carries one, and the checkout's leftovers do not.
holds_state() {
    [ -f "${1}/.deployed_sha" ] && [ ! -L "${1}/.deployed_sha" ]
}

refresh_checkout_files() {
    local aside="$ASIDE"
    git -C "$REPO" checkout -- data >/dev/null 2>&1 \
        || note "WARNING: 'git checkout -- data' failed; check the four reference files by hand"
    [ -n "$aside" ] || aside="$(last_aside)"
    [ -n "$aside" ] && [ -f "${aside}/.orchestrator" ] || return 0
    if [ "$aside" != "$ASIDE" ]; then
        [ ! -e "${aside}/.orchestrator.restored" ] || return 0
        if [ -e "${SRC}/.orchestrator" ] && cmp -s "${aside}/.orchestrator" "${SRC}/.orchestrator"; then
            # Already in place: marked all the same, or a later --rollback
            # would put this copy over a newer .orchestrator.
            mark_orchestrator_restored "$aside"
            return 0
        fi
    fi
    if cp -p "${aside}/.orchestrator" "${SRC}/.orchestrator"; then
        mark_orchestrator_restored "$aside"
    else
        note "WARNING: could not restore the newer .orchestrator from ${aside}"
    fi
}

# Idempotent, and driven by what is on disk rather than by PHASE. Returns 0
# only when the app is serving from <repo>/data again.
restore_original() {
    local running_rc=0 rebuilt=0

    # Nothing was moved (or this run had not renamed yet: what is at the
    # destination is somebody else's, left alone). Only the `.env` line is
    # undone and nothing is stopped: the app may still be running, and `up`
    # leaves a running container alone.
    if [ -d "$SRC" ] && ! has_tombstone "$SRC" \
        && { [ -z "$DEST" ] || [ "$RENAMED" = "0" ] || { [ ! -e "$DEST" ] && [ ! -L "$DEST" ]; }; }; then
        note "nothing was moved; making sure ${APP} is up on ${SRC}"
        if [ "$(env_line_count)" != "0" ]; then
            set_env_line "" || {
                say "ERROR: could not remove the ${KEY} line from ${REPO}/.env"
                return 1
            }
            note "removed the ${KEY} line from .env"
        fi
        [ "$MODE" != "rollback" ] || refresh_checkout_files
        if ! compose_bounded up -d --no-build app; then
            say "ERROR: 'docker compose up -d --no-build app' failed"
            return 1
        fi
        verify_serving "$SRC"
        return
    fi

    compose_bounded stop app
    [ "$?" != "124" ] || return 1
    app_running || running_rc=$?
    if [ "$running_rc" != "1" ]; then
        say "ERROR: cannot confirm ${APP} is stopped (probe answered ${running_rc}); not moving a directory under a container that may be running"
        return 1
    fi

    if [ -n "$DEST" ] && [ -d "$DEST" ]; then
        if [ -e "$SRC" ]; then
            if has_tombstone "$SRC" || [ "$RENAMED" = "1" ]; then
                # This run renamed the state away: whatever stands here now
                # was made since.
                rebuilt=1
            elif rmdir "$SRC" 2>/dev/null; then
                # An empty directory: the rebuild got as far as mkdir.
                rebuilt=0
            else
                say "ERROR: both ${SRC} (without ${TOMBSTONE}) and ${DEST} exist; cannot tell which holds the state"
                return 1
            fi
        fi
        if [ "$rebuilt" = "1" ]; then
            ASIDE="${BACKUP_ROOT}/checkout-data-at-rollback-${STAMP}"
            mkdir -p "$BACKUP_ROOT" || return 1
            rename_exact "$SRC" "$ASIDE" || {
                say "ERROR: could not move the rebuilt ${SRC} aside to ${ASIDE}"
                return 1
            }
            note "the rebuilt ${SRC} is kept at ${ASIDE}"
        fi
        rename_exact "$DEST" "$SRC" || {
            say "ERROR: could not rename ${DEST} back to ${SRC}"
            return 1
        }
        note "renamed ${DEST} back to ${SRC}"
        refresh_checkout_files
    elif [ ! -d "$SRC" ] || has_tombstone "$SRC"; then
        say "ERROR: ${SRC} is not the original directory and ${DEST:-the destination} does not exist"
        return 1
    fi

    if [ "$(env_line_count)" != "0" ]; then
        set_env_line "" || {
            say "ERROR: could not remove the ${KEY} line from ${REPO}/.env"
            return 1
        }
        note "removed the ${KEY} line from .env"
    fi

    if ! compose_bounded up -d --no-build app; then
        say "ERROR: 'docker compose up -d --no-build app' failed"
        return 1
    fi
    verify_serving "$SRC"
}

recover() {
    local rc=$?
    trap - EXIT
    trap '' HUP INT TERM
    set +e
    end_waited
    [ -n "$WORK" ] && rm -rf "$WORK"
    if [ "$PHASE" = "done" ] || [ "$PHASE" = "preflight" ]; then
        exit "$rc"
    fi
    say ""
    say "FAILED in phase '${PHASE}' (exit ${rc}). Returning production to ${SRC}."
    if restore_original; then
        say "RECOVERED: the app is serving from ${SRC} again. The move did not happen."
        exit 1
    fi
    say "RECOVERY INCOMPLETE."
    manual_recovery
    exit 3
}

die() {
    say "ERROR: $*"
    exit 1
}

# --- preflight: read-only, and every problem is listed, not just the first ---

PROBLEMS=0
problem() {
    PROBLEMS=$((PROBLEMS + 1))
    say "  STOP: $*"
}
fine() { say "  ok:   $*"; }

preflight_common() {
    local tool status_rc=0
    for tool in docker git python3 curl shasum; do
        command -v "$tool" >/dev/null 2>&1 || problem "${tool} is not on PATH"
    done
    if [ -n "${IDEALISTA_DATA_DIR+set}" ]; then
        problem "${KEY} is set in this shell's environment; Compose would let it outrank .env. Run: unset ${KEY}"
    else
        fine "${KEY} is not exported in this shell"
    fi
    # Compose's own switches for whether and which env file it reads and which
    # project a call addresses. Exported here they hold for this script's
    # calls and not for the watcher's: the stop and the start would not be its.
    # COMPOSE_PROJECT_NAME is here and not in lib/data_dir.sh on purpose: it
    # does not move the mount (the reader's question), but it does change
    # which containers `stop` and `up` address (this script's).
    for tool in COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROJECT_NAME; do
        if printenv "$tool" >/dev/null 2>&1; then
            problem "${tool} is set in this shell's environment; Compose here would not address the deployment the watcher runs. Run: unset ${tool}"
        fi
    done
    [ -f "${REPO}/.env" ] || problem "${REPO}/.env does not exist"
    [ -r "${LIB_DIR}/data_dir.sh" ] || problem "${LIB_DIR}/data_dir.sh is missing"
    [ -r "${LIB_DIR}/render_check.sh" ] || problem "${LIB_DIR}/render_check.sh is missing"

    "$STATUS_CMD" >/dev/null 2>&1 || status_rc=$?
    case "$status_rc" in
        0) fine "no backfill is running or expected (${STATUS_CMD##*/})" ;;
        *) problem "${STATUS_CMD##*/} answered ${status_rc} (1 busy, 2 unknown); stopping the app would kill that work. Run it to see what" ;;
    esac
}

check_backup_root() {
    local backup_at here there here_at there_at
    # The safety copy's directory. A failed move keeps the rebuilt ${SRC}
    # aside there by one rename(2): on another filesystem that rename fails
    # (EXDEV) with the app already stopped. Asked of the nearest directory
    # that exists, as `mkdir -p` will create the rest.
    backup_at="$BACKUP_ROOT"
    while [ ! -e "$backup_at" ] && [ "$backup_at" != "/" ]; do
        backup_at="$(dirname "$backup_at")"
    done
    if [ -d "$SRC" ]; then
        here="$(device_of "$SRC")" || here=""
        there="$(device_of "$backup_at")" || there=""
        here_at="$(mount_point_of "$SRC")" || here_at=""
        there_at="$(mount_point_of "$backup_at")" || there_at=""
        if [ ! -d "$backup_at" ]; then
            problem "${backup_at} is not a directory; the safety copy goes under ${BACKUP_ROOT}"
        elif [ -z "$here" ] || [ -z "$there" ] || [ -z "$here_at" ] || [ -z "$there_at" ]; then
            problem "could not establish which filesystem ${SRC} and ${backup_at} are on"
        elif [ "$here" != "$there" ] || [ "$here_at" != "$there_at" ]; then
            problem "${BACKUP_ROOT} is not on the mounted filesystem of ${SRC}; a failed move puts the rebuilt directory there by one rename. Set MOVE_BACKUP_ROOT"
        else
            fine "${BACKUP_ROOT} is on the same device and mount point as ${SRC}"
        fi
    fi
}

preflight_apply() {
    local head deployed parent source count other others mounts dirty running_rc=0 link would_mount gap complete field
    local here there here_at there_at kind volume target
    say "preflight (read-only):"
    preflight_common

    # The destination.
    case "$DEST" in
        /*) ;;
        *) problem "the destination must be an absolute path, got '${DEST}'" ;;
    esac
    case "$DEST" in
        *[[:space:]\"\'\#\$\\]*) problem "the destination must be a plain path: no spaces, quotes, '#', '\$' or backslashes (the .env reader refuses them)" ;;
    esac
    DEST="${DEST%/}"
    if [ -e "$DEST" ] || [ -L "$DEST" ]; then
        problem "${DEST} already exists; the move is a rename onto a name that must be free"
    fi
    parent="$(dirname "$DEST")"
    if [ ! -d "$parent" ] || [ ! -w "$parent" ]; then
        problem "${parent} is not a writable directory"
    else
        case "$(phys "$parent")/" in
            "$(phys "$REPO")"/*) problem "${DEST} is inside the repository; the point is to leave it" ;;
        esac
        if [ -d "$SRC" ]; then
            # A reader that failed answers nothing - and under set -e a
            # failed assignment would end the script without a verdict.
            here="$(device_of "$SRC")" || here=""
            there="$(device_of "$parent")" || there=""
            here_at="$(mount_point_of "$SRC")" || here_at=""
            there_at="$(mount_point_of "$parent")" || there_at=""
            if [ -z "$here" ] || [ -z "$there" ] || [ -z "$here_at" ] || [ -z "$there_at" ]; then
                problem "could not establish which filesystem ${SRC} and ${parent} are on"
            elif [ "$here" != "$there" ] || [ "$here_at" != "$there_at" ]; then
                problem "${parent} and ${SRC} are not on the same mounted filesystem; the move must be one rename"
            else
                # As far as can be asked without writing: the rename itself
                # is the proof, and a failed one is undone.
                fine "${DEST} is free, outside the repository, on the same device and mount point"
            fi
        fi
    fi

    check_backup_root
    refuse_overlap "$SRC" "$DEST"
    refuse_overlap "$SRC" "$BACKUP_ROOT"
    refuse_overlap "$DEST" "$BACKUP_ROOT"

    # The contract, asked of Compose itself: with the variable naming the
    # destination, would it mount the destination? In a checkout whose compose
    # file does not carry the mount the app would come back on ${SRC}, the
    # verification would fail and the move would be undone - after the
    # downtime. Asked here, it costs nothing. The directory need not exist.
    would_mount="$(env "${KEY}=${DEST}" /bin/bash "${LIB_DIR}/data_dir.sh" --compose-source "$REPO" "$COMPOSE_FILE" 2>/dev/null)" || would_mount=""
    if [ "$would_mount" = "$DEST" ]; then
        fine "Compose would mount ${DEST} once .env names it"
    else
        # What Compose rendered is not quoted: it can carry another variable's
        # value, and this output is pasted into reports.
        problem "with ${KEY}=${DEST}, ${COMPOSE_FILE} in ${REPO} would mount something else at /app/data, or Compose cannot say - not ${DEST}"
    fi

    # The checkout.
    if [ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)" != "$BRANCH" ]; then
        problem "${REPO} is not on ${BRANCH}"
    fi
    if ! dirty="$(git -C "$REPO" status --porcelain 2>/dev/null)"; then
        problem "git could not say whether ${REPO} is clean"
    elif [ -n "$dirty" ]; then
        problem "${REPO} has uncommitted or untracked files; 'git checkout -- data' must have nothing to lose"
    else
        fine "the checkout is on ${BRANCH} and clean"
    fi
    count="$(env_line_count)"
    [ "$count" = "0" ] || problem "${REPO}/.env already sets ${KEY} (${count} line(s)); the state may already be moved - see --rollback"
    ! has_tombstone "$SRC" || problem "${SRC}/${TOMBSTONE} exists; the state was moved before"
    [ -d "$SRC" ] || problem "${SRC} is not a directory"
    if [ -d "$SRC" ]; then
        # In a directory this user may not search, a tombstone that is there
        # reads as one that is not.
        [ -x "$SRC" ] || problem "${SRC} cannot be searched by this user; whether it carries ${TOMBSTONE} cannot be established"
        # A find that could not read every directory has not looked: under
        # pipefail its status used to end the script here without a word.
        if ! link="$(find "$SRC" -type l -print 2>/dev/null)"; then
            problem "${SRC} could not be searched in full for symbolic links"
        elif [ -n "$link" ]; then
            problem "${SRC} contains a symbolic link (${link%%$'\n'*}); resolve it by hand first"
        fi
    fi

    # What is serving.
    head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    # Only a regular file is read: a FIFO of that name holds `cat` for ever.
    deployed=""
    [ ! -f "${SRC}/.deployed_sha" ] || deployed="$(cat "${SRC}/.deployed_sha" 2>/dev/null || true)"
    if [ ! -f "${SRC}/.deployed_sha" ]; then
        problem "${SRC}/.deployed_sha is missing or not a regular file: what serves is not known"
    elif [ -z "$head" ] || [ "$deployed" != "$head" ]; then
        problem "a deploy is pending: .deployed_sha is '${deployed:0:7}', HEAD is '${head:0:7}'. Let the watcher finish"
    else
        fine "what serves is HEAD (${head:0:7})"
    fi
    app_running || running_rc=$?
    if [ "$running_rc" != "0" ]; then
        problem "${APP} is not running (probe answered ${running_rc})"
    else
        source="$(mount_source)" || true
        if [ -z "$source" ] || [ "$(phys "$source")" != "$(phys "$SRC")" ]; then
            problem "${APP} mounts '${source:-nothing}' at ${CONTAINER_STATE}, not ${SRC}"
        else
            fine "${APP} mounts ${SRC}"
        fi
        # The image has to carry its own reference files, or the app loses
        # them the moment /app/data stops being the checkout's directory.
        gap="$(reference_gap)"
        if [ -n "$gap" ]; then problem "$gap"; else fine "the image carries the reference files"; fi
    fi
    # Every mount of every container, running or not, against the state
    # directory: a source inside it (a container on data/.inflight can write
    # into the tree that is about to move), the directory itself, or a source
    # that holds it. The one exception is the app's own mount, checked above.
    # A list, a mount or a volume that could not be read is not "nobody".
    if ! others="$(dk ps -a --format '{{.Names}}' 2>/dev/null)"; then
        problem "docker could not list the containers; whether another one mounts ${SRC} is not known"
        others=""
    fi
    while IFS= read -r other; do
        [ -n "$other" ] || continue
        if ! mounts="$(dk inspect -f '{{json .Mounts}}' "$other" 2>/dev/null)"; then
            problem "docker could not say what container ${other} mounts"
            continue
        fi
        complete=no
        while IFS= read -r -d '' field; do
            [ "$field" != "end" ] || { complete=yes; break; }
            if ! { IFS= read -r -d '' kind && IFS= read -r -d '' volume \
                && IFS= read -r -d '' target && IFS= read -r -d '' source; }; then
                break
            fi
            if [ "$other" = "$APP" ] && [ "$target" = "$CONTAINER_STATE" ]; then
                continue
            fi
            case "$kind" in
                bind) ;;
                volume)
                    # Docker's own storage, unless the volume was created
                    # over a host path (the local driver's `device`).
                    if ! source="$(dk volume inspect -f '{{index .Options "device"}}' "$volume" 2>/dev/null)"; then
                        problem "docker could not describe volume ${volume} of container ${other}"
                        continue
                    fi
                    [ -n "$source" ] || continue
                    ;;
                tmpfs) continue ;;
                *)
                    problem "container ${other} has a mount of a kind this check does not know (${kind})"
                    continue
                    ;;
            esac
            case "$(overlaps_state "$source")" in
                no) ;;
                yes) problem "container ${other} mounts ${source} at ${target}: it is ${SRC}, inside it, or holds it. Remove the container first (docker rm ${other})" ;;
                *) problem "could not compare what container ${other} mounts with ${SRC}" ;;
            esac
        done < <(printf '%s' "$mounts" | python3 -c "$MOUNTS_PY" 2>/dev/null)
        [ "$complete" = "yes" ] || problem "the mounts of container ${other} could not be read in full"
    done <<<"$others"
}

preflight_rollback() {
    local value recorded near here there here_at there_at aside
    say "preflight (read-only):"
    preflight_common
    # A killed rollback's .orchestrator not yet marked restored: the marker must
    # be writable, or this run would restore it and a later one again, over a
    # newer .orchestrator.
    aside="$(last_aside)"
    if [ -n "$aside" ] && [ -f "${aside}/.orchestrator" ] && [ ! -e "${aside}/.orchestrator.restored" ] && [ ! -w "$aside" ]; then
        problem "${aside} is not writable, so ${aside}/.orchestrator.restored cannot be written; make it writable (chmod u+w) first"
    fi
    value="$(resolved_data_dir 2>/dev/null || true)"
    if [ "$(env_line_count)" = "0" ] && ! has_tombstone "$SRC"; then
        if [ -d "$SRC" ]; then
            NOTHING_TO_UNDO="1"
            fine "nothing to undo: .env has no ${KEY} line and ${SRC} carries no ${TOMBSTONE}"
        else
            problem "${SRC} is not a directory, and neither .env nor a ${TOMBSTONE} says where the state is"
        fi
        return 0
    fi
    # Step 4 writes the tombstone as a regular file and nothing else does: a
    # FIFO, a directory or a link of that name says nothing about whose
    # files ${SRC} holds.
    if has_tombstone "$SRC" && { [ ! -f "${SRC}/${TOMBSTONE}" ] || [ -L "${SRC}/${TOMBSTONE}" ]; }; then
        problem "${SRC}/${TOMBSTONE} is not a regular file; this script never writes one like it - inspect ${SRC} by hand"
    fi
    recorded="$(tombstone_destination "$SRC")" \
        || problem "${SRC}/${TOMBSTONE} does not record a destination the way this script writes one; read it and set ${KEY} in .env by hand"
    if [ -z "$value" ] || [ "$value" = "$SRC" ]; then
        value="$recorded"
    elif [ -n "$recorded" ] && [ "$recorded" != "$value" ]; then
        problem "${SRC}/${TOMBSTONE} records ${recorded}, .env names ${value}; make them agree by hand first"
    fi
    DEST="${value%/}"
    if [ -n "$DEST" ] && [ -d "$DEST" ]; then
        fine "the state is in ${DEST}"
        if [ -d "$SRC" ] && ! has_tombstone "$SRC" && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
            problem "both ${SRC} (without ${TOMBSTONE}) and ${DEST} hold files; cannot tell which holds the state"
        fi
        # The rename back is one rename(2): the state must be on the mounted
        # filesystem of the checkout, or it fails (EXDEV) with the app stopped.
        near="$REPO"
        [ ! -d "$SRC" ] || near="$SRC"
        here="$(device_of "$near")" || here=""
        there="$(device_of "$DEST")" || there=""
        here_at="$(mount_point_of "$near")"
        there_at="$(mount_point_of "$DEST")"
        if [ -z "$here" ] || [ -z "$there" ] || [ -z "$here_at" ] || [ -z "$there_at" ]; then
            problem "could not establish whether ${DEST} and ${near} are on one filesystem"
        elif [ "$here" != "$there" ] || [ "$here_at" != "$there_at" ]; then
            problem "${DEST} is not on the mounted filesystem of ${near}; the rename back would fail (EXDEV) with the app stopped"
        fi
    elif [ -d "$SRC" ] && ! has_tombstone "$SRC" && holds_state "$SRC"; then
        fine "the state is in ${SRC} - no rename to undo, only the .env line"
    elif [ -d "$SRC" ] && ! has_tombstone "$SRC"; then
        # Step 4 writes the tombstone right after mkdir, so a ${SRC} without
        # one and without .deployed_sha (empty, or a .DS_Store) is not state.
        problem "${SRC} holds no .deployed_sha and '${DEST:-unknown}' is not a directory; the state is in neither - find it before anything is undone"
    else
        problem "cannot find the moved directory ('${DEST:-unknown}' is not a directory)"
    fi
    check_backup_root
    refuse_overlap "$SRC" "$DEST"
    refuse_overlap "$SRC" "$BACKUP_ROOT"
    refuse_overlap "$DEST" "$BACKUP_ROOT"
}

# --- modes -------------------------------------------------------------------

MODE="check"
case "${1:-}" in
    --check) MODE="check"; shift ;;
    --apply) MODE="apply"; shift ;;
    --rollback) MODE="rollback"; shift ;;
    -h | --help) usage; exit 0 ;;
esac

if [ "$MODE" = "rollback" ]; then
    [ $# -eq 0 ] || { usage; exit 2; }
    take_both_locks
    preflight_rollback 8>&- 9>&-
    if [ "$PROBLEMS" != "0" ]; then
        say "NOT ROLLED BACK: ${PROBLEMS} problem(s) above. Nothing was touched."
        exit 1
    fi
    if [ "$NOTHING_TO_UNDO" = "1" ] && app_running \
        && [ "$(phys "$(mount_source)")" = "$(phys "$SRC")" ]; then
        say "ALREADY ROLLED BACK: ${APP} is running on ${SRC}. Nothing was touched."
        exit 0
    fi
    # Not up on it: a rollback that died after its last write. Start it.
    trap recover EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    PHASE="rolling-back"
    say "rolling back: ${DEST:-nothing} -> ${SRC} (the app stops for about a minute)"
    if restore_original; then
        PHASE="done"
        trap - EXIT HUP INT TERM
        if [ -n "$MARK_FAILED" ]; then
            say "ROLLED BACK, MARKER MISSING: the app is serving from ${SRC}, but before any later --rollback run:"
            note "touch ${MARK_FAILED}/.orchestrator.restored"
            exit 4
        fi
        say "ROLLED BACK: the app is serving from ${SRC}."
        [ -z "$ASIDE" ] || say "  the small directory that stood in ${SRC} meanwhile is kept at ${ASIDE}"
        exit 0
    fi
    PHASE="done"
    trap - EXIT HUP INT TERM
    say "ROLLBACK INCOMPLETE."
    manual_recovery
    exit 3
fi

[ $# -eq 1 ] || { usage; exit 2; }
DEST="$1"
# --apply holds both locks from before its preflight to its exit: the checks
# describe the world the move then acts on, and a second --apply stops here.
# The preflight's children do not inherit the descriptors (bash keeps its own
# copies close-on-exec for the call), so none of them can outlive the lock.
[ "$MODE" = "check" ] || take_both_locks
preflight_apply 8>&- 9>&-
if [ "$PROBLEMS" != "0" ]; then
    say "NOT MOVED: ${PROBLEMS} problem(s) above. Nothing was touched."
    exit 1
fi
if [ "$MODE" = "check" ]; then
    say "READY: nothing above stops the move. Nothing was touched."
    say "  not asked, because asking would write: whether the rename itself succeeds. The move finds out, and undoes a failed one."
    say "  to move:  $0 --apply ${DEST}"
    say "  it stops ${APP} for about a minute, copies ${SRC} to ${BACKUP_ROOT}/data-pre-move-<UTC>,"
    say "  writes ${KEY} to .env, renames ${SRC} to ${DEST} and starts the app again."
    exit 0
fi

BACKUP="${BACKUP_ROOT}/data-pre-move-${STAMP}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/idealista-move.XXXXXX")"

# From here on, no exit leaves the app down.
trap recover EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# The intent first, on disk. Until the rename it names a missing directory,
# which the reader and Compose refuse: loud, as a move in progress should be.
RENAMED="0"
PHASE="recording"
say "0/6 recording ${DEST} in .env"
set_env_line "$DEST"

PHASE="stopping"
say "1/6 stopping ${APP} (downtime starts)"
compose_waited stop app
stopped_rc=0
app_running || stopped_rc=$?
[ "$stopped_rc" = "1" ] || die "${APP} is not confirmed stopped (probe answered ${stopped_rc})"
# Only now, with no container writing: what the directory holds.
manifest "$SRC" "${WORK}/before"

PHASE="copying"
say "2/6 safety copy -> ${BACKUP}"
mkdir -p "$BACKUP_ROOT"
[ ! -e "$BACKUP" ] || die "${BACKUP} already exists"
cp -Rp "$SRC" "$BACKUP"
manifest "$BACKUP" "${WORK}/copy"
manifests_agree "${WORK}/before" "${WORK}/copy" "safety copy" \
    || die "the safety copy does not match the directory"

PHASE="renaming"
say "3/6 renaming ${SRC} -> ${DEST}"
# Asked again right before: whatever appeared since the preflight is refused.
# (After this line rename(2) decides: it replaces only an empty directory.)
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    die "${DEST} exists now: it was created after the preflight, and the move needs a free name"
fi
rename_exact "$SRC" "$DEST" || die "could not rename ${SRC} to ${DEST}"
RENAMED="1"
[ ! -e "$SRC" ] || die "${SRC} still exists after the rename"
[ -d "$DEST" ] || die "${DEST} is not a directory after the rename"
manifest "$DEST" "${WORK}/moved"
manifests_agree "${WORK}/before" "${WORK}/moved" "moved directory" \
    || die "the moved directory does not match what was there before"

PHASE="switching"
say "4/6 rebuilding ${SRC}"
# The tombstone first, synced with its directory: a rebuilt <repo>/data always
# carries it, which is how recovery tells it from the original.
mkdir "$SRC"
python3 -c 'import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
os.write(fd, ("moved_to=%s\nmoved_at=%s\n" % (sys.argv[2], sys.argv[3])).encode())
os.fsync(fd)
os.close(fd)
fd = os.open(os.path.dirname(sys.argv[1]), os.O_RDONLY)
os.fsync(fd)
os.close(fd)' "${SRC}/${TOMBSTONE}" "$DEST" "$STAMP"
git -C "$REPO" checkout -- data
if [ -f "${DEST}/.orchestrator" ]; then
    # An agreement between sessions, read from the checkout; not state.
    cp -p "${DEST}/.orchestrator" "${SRC}/.orchestrator"
fi
[ "$(resolved_data_dir)" = "$DEST" ] \
    || die "tools/autopilot/lib/data_dir.sh does not resolve ${DEST} from .env"

PHASE="starting"
say "5/6 starting ${APP}"
compose_waited up -d --no-build app

say "6/6 verifying"
verify_serving "$DEST" || die "the app is not serving correctly from ${DEST}"

PHASE="done"
trap - EXIT HUP INT TERM
rm -rf "$WORK"
say ""
say "MOVED: the deployment's state is in ${DEST}."
say "  .env:        ${KEY}=${DEST}"
say "  safety copy: ${BACKUP} (delete it once you trust the move)"
say "  ${SRC} now holds the four reference files, ${TOMBSTONE} and .orchestrator."
say "  to undo:     $0 --rollback"
