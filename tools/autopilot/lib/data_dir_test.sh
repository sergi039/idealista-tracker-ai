#!/bin/bash
# One home for the host path of the deployment's state (#639).
#
# Drives the real lib/data_dir.sh against throwaway repository roots. What is
# pinned here is what every host tool relies on:
#   * with the variable named nowhere the answer is `<repo>/data`, and Docker
#     is not asked;
#   * with IDEALISTA_DATA_DIR named anywhere the answer is what Compose binds
#     at /app/data - whatever `.env` or docker-compose.yml look like to a
#     reader of text, because this one does not read them;
#   * an answer from Compose that is not exactly one existing host directory
#     is refused - never created, never replaced by `<repo>/data`.
#
# Compose is played here by a stub that prints the configuration a scenario
# puts in its mouth, so every answer Compose could give can be staged and no
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
    OUT="$(env -u IDEALISTA_DATA_DIR -u COMPOSE_ENV_FILES DOCKER_BIN="$STUB" "$@" "$SHELL_UNDER_TEST" "$LIB" "$repo" 2>"${WORK}/err")" || RC=$?
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

# 5. The variable in .env: the answer is Compose's, and Compose was asked the
#    way the deploy watcher runs it - from the checkout, with the one file.
new_repo
printf 'DB_PASSWORD=hunter2\nIDEALISTA_DATA_DIR=%s\n' "$OTHER" >"${REPO}/.env"
resolve "$REPO"
expect_dir "the variable in .env -> what Compose binds, not what .env seems to say" "$STATE"
grep -q "^compose -f docker-compose.yml config --format json | cwd=${REPO} | IDEALISTA_DATA_DIR=<unset>$" "${WORK}/docker.log" \
    || fail "Compose was not asked from the checkout with the one compose file: $(cat "${WORK}/docker.log")"
[ "$(wc -l <"${WORK}/docker.log" | tr -d ' ')" = "1" ] || fail "Compose was asked more than once"
pass "Compose is asked once, from the checkout, with -f docker-compose.yml"
case "${OUT}${ERR}" in
    *hunter2*) fail "a value of the rendered configuration was printed" ;;
esac
pass "nothing of the rendered configuration is printed"

# 6. The variable exported in this process's environment is refused, whatever
#    Compose would make of it here: an export is seen by this process alone,
#    and the watcher under launchd would name another directory. `.env` naming
#    the same directory does not redeem it.
new_repo
resolve "$REPO" "IDEALISTA_DATA_DIR=${OTHER}"
expect_refusal "an exported variable is refused" "exported"
asked && fail "Compose was asked about an exported variable"
printf 'IDEALISTA_DATA_DIR=%s\n' "$OTHER" >"${REPO}/.env"
resolve "$REPO" "IDEALISTA_DATA_DIR=${OTHER}"
expect_refusal "an exported variable is refused beside the same .env" "exported"
rm -f "${REPO}/.env"

# 7. Exported but empty is unset, to Compose and here.
resolve "$REPO" "IDEALISTA_DATA_DIR="
expect_dir "an empty exported variable is unset" "${REPO}/data"
asked && fail "Docker was asked for an empty exported variable"

# 8. Every way .env can carry the name sends the question to Compose. None is
#    interpreted here: which of them Compose reads as the variable, and as
#    what value, is Compose's to say. A comment counts too - asking once too
#    often costs a moment, not asking once too rarely costs the state.
BOM="$(printf '\357\273\277')"
MULTILINE="$(printf 'OTHER="first line\nIDEALISTA_DATA_DIR=%s\n"' "$OTHER")"
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
    "$MULTILINE" \
    "$BINARY"; do
    new_repo
    printf 'DB_PASSWORD=x\n%s\n' "$content" >"${REPO}/.env"
    label="$(printf '%s' "$content" | LC_ALL=C tr -c '[:print:]' ' ')"
    resolve "$REPO"
    expect_dir "Compose decides: ${label%%"${OTHER}"*}" "$STATE"
    asked || fail "Compose was not asked for: ${label}"
done

# 9. COMPOSE_ENV_FILES points Compose at other env files, where the variable
#    may be set without .env ever naming it - for this process only.
new_repo
resolve "$REPO" "COMPOSE_ENV_FILES=${WORK}/elsewhere.env"
expect_refusal "COMPOSE_ENV_FILES in the environment is refused" "exported"

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
#     directory bound at /app/data. Everything else is a refusal: nothing is
#     created and nothing falls back to <repo>/data.
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

printf '\nALL %d PASSED (%s %s)\n' "$passed" "$SHELL_UNDER_TEST" "$("$SHELL_UNDER_TEST" -c 'echo $BASH_VERSION')"
