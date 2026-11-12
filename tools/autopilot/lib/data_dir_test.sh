#!/bin/bash
# One home for the host path of the deployment's state (#639).
#
# Drives the real lib/data_dir.sh against throwaway repository roots. What is
# pinned here is what every host tool will rely on:
#   * the answer is `<repo>/data`, the path every caller spells by hand today;
#   * IDEALISTA_DATA_DIR is never followed in this commit, because
#     docker-compose.yml does not read it - and it is never ignored silently
#     either: named anywhere, in any spelling, it is a refusal;
#   * the reader parses neither docker-compose.yml nor `.env`, so no content of
#     either can talk it into another directory.
#
# No Docker is needed: nothing here asks Compose anything. That this commit's
# docker-compose.yml really mounts `<repo>/data`, variable or no variable, is
# shown against Compose itself in tests/test_data_dir_single_home.py.
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

# resolve <repo> [VAR=value ...]: runs the lib with a clean variable unless
# the caller sets one. Leaves OUT, ERR and RC.
resolve() {
    local repo="$1"
    shift
    RC=0
    OUT="$(env -u IDEALISTA_DATA_DIR "$@" "$SHELL_UNDER_TEST" "$LIB" "$repo" 2>"${WORK}/err")" || RC=$?
    ERR="$(cat "${WORK}/err")"
}

# write_compose <volumes block>: a docker-compose.yml for REPO whose app
# service has the given volumes.
write_compose() {
    printf 'services:\n  app:\n    image: busybox\n    volumes:\n%s\n' "$1" >"${REPO}/docker-compose.yml"
}

# A checkout as this commit ships it: the bind is the literal ./data.
new_repo() {
    REPO="$(mktemp -d "${WORK}/repo.XXXXXX")"
    mkdir -p "${REPO}/data"
    write_compose '      - ./data:/app/data'
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
mkdir -p "$STATE"

# 1. Nothing configured: the path every caller computes by hand today.
new_repo
resolve "$REPO"
expect_dir "no .env and no variable -> <repo>/data" "${REPO}/data"

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

# 5. The variable in .env, in the spelling it will one day be written in.
#    docker-compose.yml here binds ./data, so the app is in <repo>/data
#    whatever this line says: following it would be wrong, and so would
#    pretending it is not there.
new_repo
printf 'DB_PASSWORD=hunter2\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "the variable in .env is refused, not followed" "does not read the variable"
case "$ERR" in
    *"${REPO}/data"*"${REPO}/.env"*) pass "the refusal says where the state is and where the variable is named" ;;
    *) fail "the refusal does not name <repo>/data and the .env file" ;;
esac
case "${OUT}${ERR}" in
    *hunter2* | *"$STATE"*) fail "a line of .env, or the variable's value, was printed" ;;
esac
pass "neither another line of .env nor the variable's value is printed"

# 6. The variable in the environment.
new_repo
resolve "$REPO" "IDEALISTA_DATA_DIR=${STATE}"
expect_refusal "the exported variable is refused, not followed" "named in the environment"

# 7. Exported but empty is unset, to Compose and here.
resolve "$REPO" "IDEALISTA_DATA_DIR="
expect_dir "an empty exported variable is unset" "${REPO}/data"

# 8. Every other way .env can carry the name. None is interpreted: which of
#    them Compose would read as the variable is exactly the question this file
#    does not answer for itself.
BOM="$(printf '\357\273\277')"
MULTILINE="$(printf 'OTHER="first line\nIDEALISTA_DATA_DIR=%s\n"' "$STATE")"
for content in \
    "IDEALISTA_DATA_DIR=" \
    "export IDEALISTA_DATA_DIR=${STATE}" \
    "IDEALISTA_DATA_DIR = ${STATE}" \
    "  IDEALISTA_DATA_DIR=${STATE}" \
    "IDEALISTA_DATA_DIR: ${STATE}" \
    "IDEALISTA_DATA_DIR=\"${STATE}\"" \
    "${BOM}IDEALISTA_DATA_DIR=${STATE}" \
    "IDEALISTA_DATA_DIR_OLD=${STATE}" \
    "STATE_VARIABLE=IDEALISTA_DATA_DIR" \
    "DB_PASSWORD=x # IDEALISTA_DATA_DIR=${STATE}" \
    "$MULTILINE"; do
    new_repo
    printf 'DB_PASSWORD=x\n%s\n' "$content" >"${REPO}/.env"
    label="$(printf '%s' "$content" | tr '\n' ' ')"
    resolve "$REPO"
    expect_refusal "refused, whatever the spelling: ${label%%"${STATE}"*}" "does not read the variable"
done

# 8b. Bytes that make grep call a file binary - a NUL, a byte that is not
#     UTF-8 - in another line do not hide the name.
new_repo
printf 'SECRET=a\000b\377c\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "the name is seen in a .env with binary bytes" "does not read the variable"

# 9. A comment line is the one thing that does not name it.
new_repo
printf '# IDEALISTA_DATA_DIR=%s\n  # IDEALISTA_DATA_DIR=%s\nDB_PASSWORD=x\n' "$STATE" "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "a commented-out line is not the variable" "${REPO}/data"

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
new_repo
ln -s "${WORK}/nowhere" "${REPO}/.env"
resolve "$REPO"
expect_refusal "a .env that is a dangling link is refused" "cannot be read"

# 11. Nothing in docker-compose.yml is read. Whatever a compose file says
#     about the variable - mounting it, mentioning it after the real bind on
#     the same line, mounting it for another service or at another target - a
#     variable that is set is refused and an unset one answers <repo>/data.
#     There is no text in that file that turns the variable on.
compose_shape() {
    case "$1" in
        mounted-short) printf '      - ${IDEALISTA_DATA_DIR:-./data}:/app/data' ;;
        inline-comment) printf '      - ./data:/app/data # ${IDEALISTA_DATA_DIR:-./data}' ;;
        quoted-then-comment) printf '      - "./data:/app/data"  # was ${IDEALISTA_DATA_DIR}' ;;
        mounted-long) printf '      - type: bind\n        source: ${IDEALISTA_DATA_DIR:-./data}\n        target: /app/data' ;;
        other-target) printf '      - ./data:/app/data\n      - ${IDEALISTA_DATA_DIR:-./data}/logs:/app/logs' ;;
        other-service) printf '      - ./data:/app/data\n  worker:\n    image: busybox\n    volumes:\n      - ${IDEALISTA_DATA_DIR:-./data}:/app/data' ;;
    esac
}
for shape in mounted-short inline-comment quoted-then-comment mounted-long other-target other-service; do
    new_repo
    write_compose "$(compose_shape "$shape")"
    resolve "$REPO"
    expect_dir "compose file not read (${shape}): nothing set -> <repo>/data" "${REPO}/data"
    printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
    resolve "$REPO"
    expect_refusal "compose file not read (${shape}): the .env line is refused" "does not read the variable"
    rm -f "${REPO}/.env"
    resolve "$REPO" "IDEALISTA_DATA_DIR=${STATE}"
    expect_refusal "compose file not read (${shape}): the exported variable is refused" "does not read the variable"
done

# 12. No compose file at all changes nothing either.
REPO="$(mktemp -d "${WORK}/repo.XXXXXX")"
resolve "$REPO"
expect_dir "no compose file, nothing set -> <repo>/data" "${REPO}/data"
resolve "$REPO" "IDEALISTA_DATA_DIR=${STATE}"
expect_refusal "no compose file, variable refused" "does not read the variable"

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

printf '\nALL %d PASSED (%s %s)\n' "$passed" "$SHELL_UNDER_TEST" "$("$SHELL_UNDER_TEST" -c 'echo $BASH_VERSION')"
