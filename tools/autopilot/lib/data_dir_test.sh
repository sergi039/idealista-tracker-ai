#!/bin/bash
# One home for the host path of the deployment's state (#639).
#
# Drives the real lib/data_dir.sh against throwaway repository roots. What is
# pinned here is what every host tool relies on:
#   * with the variable named nowhere the answer is `<repo>/data`, and Docker
#     is not asked;
#   * with IDEALISTA_DATA_DIR named anywhere the answer is what Compose binds
#     at /app/data, because this reader parses neither file;
#   * an answer that is not exactly one existing host directory is refused -
#     never created, never replaced by `<repo>/data`.
#
# Compose is played here by a stub that prints what a scenario stages, so no
# Docker is needed. That the real `docker compose config` and the reader agree
# is shown against Compose itself in tests/test_data_dir_single_home.py.
#
# Runs under the interpreter the LaunchAgent uses (/bin/bash, 3.2 on the mini)
# unless DATA_DIR_TEST_BASH says otherwise.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${SCRIPT_DIR}/data_dir.sh"
SHELL_UNDER_TEST="${DATA_DIR_TEST_BASH:-/bin/bash}"

WORK="$(cd "$(mktemp -d)" && pwd -P)"
cleanup() {
    local rc=$?
    chmod -R u+rwx "$WORK" 2>/dev/null || true
    rm -rf "$WORK"
    exit "$rc"
}
trap cleanup EXIT

passed=0
fail() {
    printf 'FAIL: %s\n' "$*" >&2
    printf -- '--- stdout ---\n%s\n--- stderr ---\n%s\n' "${OUT-}" "${ERR-}" >&2
    exit 1
}
pass() { passed=$((passed + 1)); printf 'ok   %s\n' "$*"; }

# The stand-in for `docker`: logs its arguments and working directory, prints
# the staged configuration, exits with the staged status.
STUB="${WORK}/docker"
cat >"$STUB" <<EOF
#!/bin/bash
printf '%s | cwd=%s | %s=%s\\n' "\$*" "\$PWD" IDEALISTA_DATA_DIR "\${IDEALISTA_DATA_DIR-<unset>}" >>"${WORK}/docker.log"
cat "${WORK}/compose.json"
exit "\$(cat "${WORK}/compose.rc")"
EOF
chmod +x "$STUB"

# compose_says <volumes as JSON>: what the stub will answer. The environment
# block stands for the values of .env a real rendering carries.
compose_says() {
    printf '{"services":{"app":{"environment":{"DB_PASSWORD":"hunter2"},"volumes":%s}}}\n' "$1" >"${WORK}/compose.json"
    printf '0\n' >"${WORK}/compose.rc"
}
bind() { printf '{"type":"bind","source":"%s","target":"%s"}' "$1" "${2:-/app/data}"; }
asked() { [ -s "${WORK}/docker.log" ]; }

# resolve <repo> [VAR=value ...]: runs the lib with a clean variable unless
# the caller sets one. Leaves OUT, ERR and RC.
resolve() {
    local repo="$1"
    shift
    : >"${WORK}/docker.log"
    RC=0
    OUT="$(env -u IDEALISTA_DATA_DIR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE DOCKER_BIN="$STUB" "$@" "$SHELL_UNDER_TEST" "$LIB" "$repo" 2>"${WORK}/err")" || RC=$?
    ERR="$(cat "${WORK}/err")"
}

# write_compose <volumes block>: a docker-compose.yml for REPO whose app
# service has the given volumes.
write_compose() {
    printf 'services:\n  app:\n    image: busybox\n    volumes:\n%s\n' "$1" >"${REPO}/docker-compose.yml"
}

# A checkout as this commit ships it: the bind is the variable.
new_repo() {
    REPO="$(mktemp -d "${WORK}/repo.XXXXXX")"
    mkdir -p "${REPO}/data"
    write_compose '      - ${IDEALISTA_DATA_DIR:-./data}:/app/data'
}

expect_dir() {
    [ "$RC" = "0" ] || fail "$1: expected success, rc=${RC}"
    [ "$OUT" = "$2" ] || fail "$1: expected '$2', got '${OUT}'"
    pass "$1"
}

expect_refusal() {
    [ "$RC" = "1" ] || fail "$1: expected a refusal (rc 1), rc=${RC}"
    [ -z "$OUT" ] || fail "$1: a refusal must print no path, printed '${OUT}'"
    case "$ERR" in
        *"$2"*) ;;
        *) fail "$1: stderr does not mention '$2'" ;;
    esac
    pass "$1"
}

STATE="${WORK}/state"
OTHER="${WORK}/other"
mkdir -p "$STATE" "$OTHER"
# Unless a scenario says otherwise, Compose would mount STATE.
compose_says "[$(bind "$STATE")]"

# 1. Nothing configured: the path every caller computes by hand today.
new_repo
resolve "$REPO"
expect_dir "no .env and no variable -> <repo>/data" "${REPO}/data"
asked && fail "Docker was asked although the variable is named nowhere"
pass "with the variable named nowhere Docker is not asked"

# 2. A .env that does not name the variable changes nothing - and its lines,
#    which are secrets on the real machine, never reach the output.
printf 'DB_PASSWORD=hunter2\nAPP_HOST_PORT=5001\n' >"${REPO}/.env"
resolve "$REPO"
expect_dir ".env without the variable -> <repo>/data" "${REPO}/data"
case "${OUT}${ERR}" in
    *hunter2*) fail "a line of .env was printed" ;;
esac
pass "no line of .env is printed"

# 3. The directory does not have to exist yet; callers create it on demand.
REPO="$(mktemp -d "${WORK}/repo.XXXXXX")"
resolve "$REPO"
expect_dir "<repo>/data is not required to exist" "${REPO}/data"

# 4. A trailing slash on the root is not a different checkout.
new_repo
resolve "${REPO}/"
expect_dir "a trailing slash on the root is trimmed" "${REPO}/data"

# 5. The variable in .env: the answer is Compose's, asked the way the deploy
#    watcher runs it - from the checkout, with the one file.
new_repo
printf 'DB_PASSWORD=hunter2\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "the variable in .env -> what Compose binds" "$STATE"
grep -q "^compose --project-directory ${REPO} -f docker-compose.yml config --format json | cwd=${REPO} | IDEALISTA_DATA_DIR=<unset>$" "${WORK}/docker.log" \
    || fail "Compose was not asked from the checkout with the one compose file: $(cat "${WORK}/docker.log")"
[ "$(wc -l <"${WORK}/docker.log" | tr -d ' ')" = "1" ] || fail "Compose was asked more than once"
pass "Compose is asked once, from the checkout, with -f docker-compose.yml"
case "${OUT}${ERR}" in
    *hunter2*) fail "a value of the rendered configuration was printed" ;;
esac
pass "nothing of the rendered configuration is printed"

# 6. The variable exported in this process's environment is refused: an
#    export is seen by this process alone, and the watcher under launchd would
#    name another directory. `.env` naming the same one does not redeem it.
new_repo
resolve "$REPO" "IDEALISTA_DATA_DIR=${OTHER}"
expect_refusal "an exported variable is refused" "IDEALISTA_DATA_DIR is set in this process"
asked && fail "Compose was asked about an exported variable"
printf 'IDEALISTA_DATA_DIR=%s\n' "$OTHER" >"${REPO}/.env"
resolve "$REPO" "IDEALISTA_DATA_DIR=${OTHER}"
expect_refusal "an exported variable is refused beside the same .env" "is set in this process"

# 7. Exported but EMPTY is refused too. To Compose that is not "unset": it
#    outranks .env and the mount renders ./data, for this process alone.
resolve "$REPO" "IDEALISTA_DATA_DIR="
expect_refusal "an empty exported variable is refused beside a .env naming a directory" "an empty value counts"
asked && fail "Compose was asked about an empty exported variable"
rm -f "${REPO}/.env"
resolve "$REPO" "IDEALISTA_DATA_DIR="
expect_refusal "an empty exported variable is refused" "IDEALISTA_DATA_DIR is set in this process"

# 8. Every way .env can carry the name sends the question to Compose - a
#    comment counts too: asking once too often costs a moment, not asking once
#    too rarely costs the state. None of these is the plain line that says
#    what Compose answers (STATE here), so none is interpreted: all refused.
BOM="$(printf '\357\273\277')"
MULTILINE="$(printf 'OTHER="first line\nIDEALISTA_DATA_DIR=%s\n"' "$OTHER")"
# The review's input to #694: to Compose the second line closes NOTE's value
# and then SETS the variable; to a reader of lines it starts with `#`.
QUOTE_TAIL="$(printf "NOTE='first line\n#' IDEALISTA_DATA_DIR=./state")"
BINARY="$(printf 'SECRET=a\377c\nIDEALISTA_DATA_DIR=%s' "$OTHER")"
for content in \
    "IDEALISTA_DATA_DIR=" \
    "export IDEALISTA_DATA_DIR=${OTHER}" \
    "IDEALISTA_DATA_DIR = ${OTHER}" \
    "IDEALISTA_DATA_DIR: ${OTHER}" \
    "IDEALISTA_DATA_DIR=\"${OTHER}\"" \
    "${BOM}IDEALISTA_DATA_DIR=${OTHER}" \
    "IDEALISTA_DATA_DIR_OLD=${OTHER}" \
    "DB_PASSWORD=x # IDEALISTA_DATA_DIR=${OTHER}" \
    "# IDEALISTA_DATA_DIR=${OTHER}" \
    "$QUOTE_TAIL" \
    "$MULTILINE" \
    "$BINARY"; do
    new_repo
    printf 'DB_PASSWORD=x\n%s\n' "$content" >"${REPO}/.env"
    label="$(printf '%s' "$content" | LC_ALL=C tr -c '[:print:]' ' ')"
    resolve "$REPO"
    expect_refusal "not the plain line: ${label%%"${OTHER}"*}" "does not say exactly that"
    asked || fail "Compose was not asked for: ${label}"
done

# 9. COMPOSE_ENV_FILES points Compose at other env files, for this process.
new_repo
resolve "$REPO" "COMPOSE_ENV_FILES=${WORK}/elsewhere.env"
expect_refusal "COMPOSE_ENV_FILES in the environment is refused" "COMPOSE_ENV_FILES is set in this process"
#    COMPOSE_DISABLE_ENV_FILE stops it reading .env. Set to nothing is set.
for switch in COMPOSE_ENV_FILES= COMPOSE_DISABLE_ENV_FILE=1 COMPOSE_DISABLE_ENV_FILE=; do
    resolve "$REPO" "$switch"
    expect_refusal "${switch} in the environment is refused" "${switch%%=*} is set in this process"
done

# 9b. Compose's answer counts only if it is the text of the one line naming
#     the variable. Here Compose says STATE each time, as it would for a
#     process whose HOME, environment or directory make it so - and .env does
#     not say STATE, or says it twice, or not alone on its line.
for line in '~/state' '${STATE_ROOT}/state' './state' "$OTHER" "\"${STATE}\"" "${STATE} # the state" "${STATE}/"; do
    printf 'IDEALISTA_DATA_DIR=%s\n' "$line" >"${REPO}/.env"
    resolve "$REPO"
    expect_refusal "Compose's answer is refused when .env says '${line%%"${WORK}"*}'" "does not say exactly that"
done
printf 'IDEALISTA_DATA_DIR=%s\n# IDEALISTA_DATA_DIR=%s\n' "$STATE" "$OTHER" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "the name on two lines is refused" "names it on 2"
#     A comment is a mention like any other: what looks like one here may be
#     the tail of a quoted value to Compose, and telling the two apart is the
#     parsing this reader does not do. So Compose is asked, and when it says
#     the checkout's own data - as it does for a real comment - that is still
#     not the text of a line, and the answer is a refusal.
printf '# IDEALISTA_DATA_DIR=%s\n' "$OTHER" >"${REPO}/.env"
compose_says "[$(bind "${REPO}/data")]"
resolve "$REPO"
expect_refusal "a .env that names the variable only in a comment is refused" "does not say exactly that"
asked || fail "Compose was not asked about a commented-out line"
compose_says "[$(bind "$STATE")]"
mkdir -p "${WORK}/a\$b"
printf 'IDEALISTA_DATA_DIR=%s\n' "${WORK}/a\$b" >"${REPO}/.env"
compose_says "[$(bind "${WORK}/a\$b")]"
resolve "$REPO"
expect_refusal "an answer with a \$ in it is refused even when .env says the same" "with a '\$' in it"
compose_says "[$(bind "$STATE")]"
printf 'export IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "a line that is not exactly NAME=path is refused" "does not say exactly that"

# 10. A .env that cannot be read cannot be ruled out, so it is not silent.
if [ "$(id -u)" != "0" ]; then
    new_repo
    printf 'DB_PASSWORD=x\n' >"${REPO}/.env"
    chmod 000 "${REPO}/.env"
    resolve "$REPO"
    chmod 600 "${REPO}/.env"
    expect_refusal "an unreadable .env is refused" "cannot be read"
fi
new_repo
mkdir "${REPO}/.env"
resolve "$REPO"
expect_refusal "a .env that is a directory is refused" "cannot be read"

# 11. What Compose answers is used only if it is exactly one existing host
#     directory bound at /app/data. Nothing is created, nothing falls back.
named_repo() {
    new_repo
    printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
}
MISSING="${WORK}/missing"
: >"${WORK}/a-file"

named_repo; compose_says "[$(bind "$MISSING")]"; resolve "$REPO"
expect_refusal "a directory that does not exist is refused" "not an existing directory"
[ ! -e "$MISSING" ] || fail "the missing directory was created"
pass "a missing directory is not created"

named_repo; compose_says "[$(bind "${WORK}/a-file")]"; resolve "$REPO"
expect_refusal "a regular file is refused" "not an existing directory"

named_repo; compose_says "[$(bind "/")]"; resolve "$REPO"
expect_refusal "the filesystem root is refused" "would mount /"

named_repo; compose_says "[$(bind "state")]"; resolve "$REPO"
expect_refusal "a relative source is refused" "not an absolute path"

named_repo; compose_says "[$(bind "${STATE}/")]"; resolve "$REPO"
expect_dir "a trailing slash on the source is trimmed" "$STATE"

named_repo; compose_says "[$(bind "$STATE"),$(bind "$OTHER")]"; resolve "$REPO"
expect_refusal "two mounts at /app/data are refused" "exactly one host directory"

named_repo; compose_says "[$(bind "$STATE"),$(bind "$OTHER" /app/data/attachments)]"; resolve "$REPO"
expect_refusal "a second mount under /app/data is refused" "exactly one host directory"

named_repo; compose_says "[$(bind "$STATE" /app/data/)]"; resolve "$REPO"
expect_refusal "a target that is not literally /app/data is refused" "exactly one host directory"

named_repo; compose_says "[$(bind "$OTHER" /app/logs)]"; resolve "$REPO"
expect_refusal "no mount at /app/data is refused" "exactly one host directory"

named_repo; compose_says '[{"type":"volume","source":"state","target":"/app/data"}]'; resolve "$REPO"
expect_refusal "a named volume at /app/data is refused" "exactly one host directory"

named_repo; printf '{"services":{"worker":{"volumes":[%s]}}}\n' "$(bind "$STATE")" >"${WORK}/compose.json"; resolve "$REPO"
expect_refusal "a mount that belongs to another service is refused" "exactly one host directory"

named_repo; printf 'this is not json\n' >"${WORK}/compose.json"; resolve "$REPO"
expect_refusal "an answer that is not a configuration is refused" "exactly one host directory"

named_repo; compose_says "[$(bind "$STATE")]"; printf '1\n' >"${WORK}/compose.rc"; resolve "$REPO"
expect_refusal "a Compose that fails is refused, whatever it printed" "config' failed"

named_repo; compose_says "[$(bind "$STATE")]"
resolve "$REPO" "DOCKER_BIN=${WORK}/no-such-docker"
expect_refusal "no Docker to ask is refused" "is not on PATH"
[ -z "$(ls -A "${REPO}/data")" ] || fail "something was written into <repo>/data after a refusal"
pass "a refusal leaves <repo>/data alone"

# 12. --compose-source prints what Compose would bind, unchecked, for the
#     compose file it is given.
compose_says "[$(bind "$MISSING")]"
new_repo
: >"${WORK}/docker.log"
RC=0
OUT="$(env -u IDEALISTA_DATA_DIR DOCKER_BIN="$STUB" "$SHELL_UNDER_TEST" "$LIB" --compose-source "$REPO" other.yml 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_dir "--compose-source prints the bind although the directory does not exist" "$MISSING"
grep -q "^compose --project-directory ${REPO} -f other.yml config --format json | cwd=${REPO} " "${WORK}/docker.log" \
    || fail "--compose-source did not ask about the compose file it was given"
pass "--compose-source asks about the compose file it is given"
RC=0
OUT="$(DOCKER_BIN="$STUB" "$SHELL_UNDER_TEST" "$LIB" --compose-source 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_refusal "--compose-source without a repository root is refused" "usage"
compose_says "[$(bind "$STATE")]"

# 12b. The tombstone. tools/move_data_dir.sh leaves `.moved-to` in <repo>/data
#      once the state lives elsewhere. Resolving to that directory means the
#      .env line was lost, and the directory holds no mail cursor: refused,
#      with the place the state went and both ways out.
new_repo
printf 'moved_to=%s\nmoved_at=20261112T000000Z\n' "$STATE" >"${REPO}/data/.moved-to"
resolve "$REPO"
expect_refusal "the default directory is refused once it carries the tombstone" "carries .moved-to"
case "$ERR" in
    *"${STATE}"*"--rollback"*) pass "the refusal names where the state went and how to undo it" ;;
    *) fail "the tombstone refusal does not name the moved directory and --rollback" ;;
esac

# 12c. With the line in place Compose mounts the moved directory, and the
#      tombstone is exactly what is expected in <repo>/data.
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "the moved directory resolves while <repo>/data carries the tombstone" "$STATE"

# 12d. A .env line that points back at the directory the state left is the
#      same mistake arrived at through Compose. (A compose file that binds
#      ./data again while .env names the moved directory is refused earlier:
#      Compose's answer is then not the text of .env.)
printf 'IDEALISTA_DATA_DIR=%s\n' "${REPO}/data" >"${REPO}/.env"
compose_says "[$(bind "${REPO}/data")]"
resolve "$REPO"
expect_refusal "Compose mounting the tombstoned directory is refused" "carries .moved-to"
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "a compose file that binds the directory the state left is refused" "does not say exactly that"
compose_says "[$(bind "$STATE")]"

# 12e. A tombstone that records nothing is still a tombstone.
new_repo
: >"${REPO}/data/.moved-to"
resolve "$REPO"
expect_refusal "an empty tombstone still refuses" "an unrecorded directory"
# A symbolic link whose target is gone: -e says there is nothing there.
rm -f "${REPO}/data/.moved-to"
ln -s "${WORK}/gone" "${REPO}/data/.moved-to"
resolve "$REPO"
expect_refusal "a dangling symbolic link named as the tombstone still refuses" "carries .moved-to"

# 12e2. The tombstone is the fact that the entry exists; what it holds is a
#       hint, and no kind of entry may turn the refusal into a wait, a crash
#       or a line of junk in the watcher's log (review of #700). A reader
#       still running after ten seconds is a failure here, not a hung harness.
resolve_bounded() {
    local repo="$1" pid waited=0
    : >"${WORK}/docker.log"
    RC=0
    env -u IDEALISTA_DATA_DIR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE DOCKER_BIN="$STUB" \
        "$SHELL_UNDER_TEST" "$LIB" "$repo" >"${WORK}/out" 2>"${WORK}/err" &
    pid=$!
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 100 ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        RC=124
        # Whatever it started is still waiting on the FIFO, and would outlive
        # this harness: a writer lets it read end-of-file and go. The writer
        # itself gets a second, in case nothing was reading.
        ( : >"${repo}/data/.moved-to" ) 2>/dev/null &
        local writer=$!
        sleep 1
        # Usually gone already; a failed kill must not end the harness here,
        # before it has said what failed.
        kill "$writer" 2>/dev/null || :
        wait "$writer" 2>/dev/null || :
    fi
    wait "$pid" 2>/dev/null || [ "$RC" = "124" ] || RC=$?
    OUT="$(cat "${WORK}/out")"
    ERR="$(cat "${WORK}/err")"
}
expect_tombstone_refusal() {
    # expect_tombstone_refusal <label> [text the stderr must also carry]
    [ "$RC" != "124" ] || fail "$1: the reader was still waiting on the tombstone after ten seconds"
    expect_refusal "$1" "carries .moved-to"
    case "$ERR" in *"${2:-}"*) ;; *) fail "$1: stderr does not mention '${2:-}'" ;; esac
    [ "$(printf '%s\n' "$ERR" | wc -l | tr -d ' ')" = "1" ] || fail "$1: the refusal is not one line"
}
new_repo
mkfifo "${REPO}/data/.moved-to"
resolve_bounded "$REPO"
expect_tombstone_refusal "a FIFO named as the tombstone refuses, and is not read" "an unrecorded directory"
rm -f "${REPO}/data/.moved-to"
mkfifo "${WORK}/a-fifo"
ln -s "${WORK}/a-fifo" "${REPO}/data/.moved-to"
resolve_bounded "$REPO"
expect_tombstone_refusal "a link to a FIFO named as the tombstone refuses" "an unrecorded directory"
rm -f "${REPO}/data/.moved-to"
printf '\377' >"${REPO}/data/.moved-to"
resolve_bounded "$REPO"
expect_tombstone_refusal "a tombstone that is not text refuses" "an unrecorded directory"
printf 'moved_to=%s\n' "$STATE" >"${REPO}/data/.moved-to"
chmod 000 "${REPO}/data/.moved-to"
resolve_bounded "$REPO"
if [ "$(id -u)" = "0" ]; then
    expect_tombstone_refusal "a tombstone that may not be read refuses"
else
    expect_tombstone_refusal "a tombstone that may not be read refuses" "an unrecorded directory"
fi
chmod 600 "${REPO}/data/.moved-to"
printf 'moved_to=/x\033[31m\ty\r\nmoved_at=1\n' >"${REPO}/data/.moved-to"
resolve_bounded "$REPO"
expect_tombstone_refusal "a tombstone with control characters refuses" "'/x[31my'"
case "$ERR" in *"$(printf '\033')"* | *"$(printf '\t')"* | *"$(printf '\r')"*) fail "a control character from the tombstone reached stderr" ;; esac
{ head -c 8192 /dev/zero | tr '\0' 'a'; printf '\nmoved_to=/late\n'; } >"${REPO}/data/.moved-to"
resolve_bounded "$REPO"
expect_tombstone_refusal "only the head of a large tombstone is read" "an unrecorded directory"
rm -f "${REPO}/data/.moved-to" "${WORK}/a-fifo"

# 12f. `--default` names the directory inside the checkout and reads nothing:
#      not .env, not the environment, not the tombstone, and not Compose.
new_repo
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
: >"${REPO}/data/.moved-to"
: >"${WORK}/docker.log"
RC=0
OUT="$(IDEALISTA_DATA_DIR="$OTHER" DOCKER_BIN="$STUB" "$SHELL_UNDER_TEST" "$LIB" --default "${REPO}/" 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_dir "--default is <repo>/data whatever is configured" "${REPO}/data"
asked && fail "--default asked Compose"
RC=0
OUT="$("$SHELL_UNDER_TEST" "$LIB" --default 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_refusal "--default without a repository root is refused" "usage"

# 13. The root has to be the checkout Compose runs from: absolute, existing.
new_repo
RC=0
OUT="$(cd "$REPO" && env -u IDEALISTA_DATA_DIR "$SHELL_UNDER_TEST" "$LIB" . 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_refusal "a relative root is refused" "absolute path"
resolve "${WORK}/no-such-checkout"
expect_refusal "a root that does not exist is refused" "not an existing directory"
[ ! -e "${WORK}/no-such-checkout" ] || fail "the missing root was created"
pass "a missing root is not created"

# 14. No repository root is a usage error, not `/data`.
RC=0
OUT="$(env -u IDEALISTA_DATA_DIR "$SHELL_UNDER_TEST" "$LIB" 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_refusal "no repository root is refused" "usage"

# 15. A linked git worktree is not the checkout the stack runs from. Git marks
# one with a `.git` FILE; the checkout's own `.git` is a directory. The
# worktree's data/ exists here, and holds a file, to show it is not followed.
new_repo
printf 'gitdir: /somewhere/.git/worktrees/issue-1\n' >"${REPO}/.git"
: >"${REPO}/data/.supervisor.idealista-app.utils.backfill_pool.lock"
resolve "$REPO"
expect_refusal "a linked git worktree is refused" "linked git worktree"
rm -f "${REPO}/.git"
mkdir "${REPO}/.git"
resolve "$REPO"
expect_dir "a checkout with a .git directory is answered" "${REPO}/data"
new_repo
resolve "$REPO"
expect_dir "a root that is no git checkout at all is answered" "${REPO}/data"

# 16. "Could not look" is not "not there". With no grep to search `.env` -
# a PATH stripped to nothing - the file has not been ruled out, so it is a
# refusal; read as zero matches it would answer for a file nobody searched.
# Without a `.env` there is nothing to search and no grep is needed.
new_repo
printf 'DB_PASSWORD=x\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO" "PATH=${WORK}/no-such-bin"
expect_refusal "a .env that cannot be searched is refused (variable named)" "could not be searched"
printf 'DB_PASSWORD=x\n' >"${REPO}/.env"
resolve "$REPO" "PATH=${WORK}/no-such-bin"
expect_refusal "a .env that cannot be searched is refused (variable not named)" "could not be searched"
rm -f "${REPO}/.env"
resolve "$REPO" "PATH=${WORK}/no-such-bin"
expect_dir "no .env needs no grep" "${REPO}/data"

# 17. A search that FAILED is not "not there" either, whatever it printed. The
# file passes the readable check and then cannot be read (a race, a revoked
# permission): grep ends 2. Here a grep that ends 2 having printed `0`, the
# way the second grep of a pipeline would over an empty input - read by its
# count alone, that is "the variable is not named" and <repo>/data.
mkdir -p "${WORK}/failing-grep"
printf '#!/bin/bash\necho 0\nexit 2\n' >"${WORK}/failing-grep/grep"
chmod +x "${WORK}/failing-grep/grep"
new_repo
printf 'DB_PASSWORD=x\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO" "PATH=${WORK}/failing-grep:${PATH}"
expect_refusal "a search that failed is refused, though it printed a zero" "could not be searched"
asked && fail "Compose was asked after a failed search"
printf '#!/bin/bash\necho 0\nexit 0\n' >"${WORK}/failing-grep/grep"
resolve "$REPO" "PATH=${WORK}/failing-grep:${PATH}"
expect_refusal "a status and a count that disagree are refused" "could not be searched"

# 18. No refusal quotes what a command read out of `.env`. stderr here is the
# deploy watcher's log, and `.env` holds every credential of the deployment.
# A grep that misbehaves - prints the lines it matched, to both streams, and
# then fails - must not have its output repeated; and what Compose rendered is
# not quoted either: with `${X}` in `.env` it can be another variable's value.
printf '#!/bin/bash\necho "DB_PASSWORD=hunter2"\necho "DB_PASSWORD=hunter2" >&2\nexit 2\n' >"${WORK}/failing-grep/grep"
resolve "$REPO" "PATH=${WORK}/failing-grep:${PATH}"
expect_refusal "a grep that printed a line of .env and failed is refused" "could not be searched"
case "${OUT}${ERR}" in *hunter2*) fail "the refusal repeats what grep printed out of .env" ;; esac
pass "what grep printed is in neither stream"
SECRET_PATH="${WORK}/hunter2-not-a-path"
for answer in "$SECRET_PATH" "relative/hunter2" "${OTHER}" "${WORK}/a\$hunter2"; do
    new_repo
    printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
    compose_says "[$(bind "$answer")]"
    resolve "$REPO"
    [ "$RC" = "1" ] || fail "an answer that is not the text of .env was taken"
    case "${OUT}${ERR}" in *"$answer"*) fail "a refusal quotes what Compose rendered: ${ERR}" ;; esac
done
pass "no refusal quotes what Compose rendered"

# 19. A control character in what Compose rendered (review of #694). The
# review's input is a line break inside the path, held against Compose itself
# in tests/test_data_dir_single_home.py. Here the two guards one at a time:
# a line break at the END of the source, which `$(...)` would silently drop,
# is the parser's to refuse; a tab inside the path is the reader's - and .env
# says exactly that path, so nothing else would stop it.
new_repo
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
printf '{"services":{"app":{"volumes":[{"type":"bind","source":"%s\\n","target":"/app/data"}]}}}\n' "$STATE" >"${WORK}/compose.json"
resolve "$REPO"
expect_refusal "a source that ends in a line break is refused" "exactly one host directory"
TABBED="${WORK}/with$(printf '\t')tab"
mkdir -p "$TABBED"
printf 'IDEALISTA_DATA_DIR=%s\n' "$TABBED" >"${REPO}/.env"
printf '{"services":{"app":{"volumes":[{"type":"bind","source":"%s/with\\ttab","target":"/app/data"}]}}}\n' "$WORK" >"${WORK}/compose.json"
resolve "$REPO"
expect_refusal "a source with a control character in it is refused" "control character"

printf '\nALL %d PASSED (%s %s)\n' "$passed" "$SHELL_UNDER_TEST" "$("$SHELL_UNDER_TEST" -c 'echo $BASH_VERSION')"
