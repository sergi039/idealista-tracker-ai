#!/bin/bash
# One home for the host path of the deployment's state (#639).
#
# Drives the real lib/data_dir.sh against throwaway repository roots. What is
# pinned here is the contract every host tool and docker-compose.yml rely on:
#   * nothing configured -> `<repo>/data`, the path every caller used before;
#   * the `.env` line and the process environment are read with Compose's
#     precedence, and a disagreement between them is refused, not resolved;
#   * a value that is not an existing absolute directory is refused - never
#     created, never replaced by the default.
#
# Runs under the interpreter the LaunchAgent uses (/bin/bash, 3.2 on the mini)
# unless DATA_DIR_TEST_BASH says otherwise.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${SCRIPT_DIR}/data_dir.sh"
SHELL_UNDER_TEST="${DATA_DIR_TEST_BASH:-/bin/bash}"

WORK="$(mktemp -d)"
cleanup() { local rc=$?; rm -rf "$WORK"; exit "$rc"; }
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

new_repo() {
    REPO="$(mktemp -d "${WORK}/repo.XXXXXX")"
    mkdir -p "${REPO}/data"
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

# 1. Nothing configured: the path every caller computed before this file.
new_repo
resolve "$REPO"
expect_dir "no .env and no variable -> <repo>/data" "${REPO}/data"

# 2. A .env that says nothing about the key changes nothing - and its other
#    lines, which are secrets on the real machine, never reach the output.
new_repo
printf 'DB_PASSWORD=hunter2\nAPP_HOST_PORT=5001\n' >"${REPO}/.env"
resolve "$REPO"
expect_dir ".env without the key -> <repo>/data" "${REPO}/data"
case "${OUT}${ERR}" in
    *hunter2*) fail "a line of .env that is not the key was printed" ;;
esac
pass "no other line of .env is printed"

# 3. The default does not have to exist yet; callers create it on demand.
REPO="$(mktemp -d "${WORK}/repo.XXXXXX")"
resolve "$REPO"
expect_dir "the default is not required to exist" "${REPO}/data"

# 4. The .env line.
new_repo
printf 'DB_PASSWORD=x\nIDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "the .env line is read" "$STATE"

# 5. One pair of quotes, either kind, and a CRLF line ending.
new_repo
printf 'IDEALISTA_DATA_DIR="%s"\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "double quotes are stripped" "$STATE"
printf "IDEALISTA_DATA_DIR='%s'\n" "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "single quotes are stripped" "$STATE"
printf 'IDEALISTA_DATA_DIR=%s\r\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "a CRLF line ending is not part of the path" "$STATE"

# 6. A trailing slash is not a different directory.
printf 'IDEALISTA_DATA_DIR=%s/\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "a trailing slash is trimmed" "$STATE"

# 7. The process environment, alone.
new_repo
resolve "$REPO" "IDEALISTA_DATA_DIR=${STATE}"
expect_dir "the process environment is read" "$STATE"

# 8. Both, agreeing.
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO" "IDEALISTA_DATA_DIR=${STATE}"
expect_dir "environment and .env agreeing is fine" "$STATE"

# 9. Both, disagreeing: Compose would silently take the environment's.
resolve "$REPO" "IDEALISTA_DATA_DIR=${OTHER}"
expect_refusal "environment and .env disagreeing is refused" "Unset one of them"

# 10. Set-but-empty in the environment still outranks .env for Compose, where
#     `${IDEALISTA_DATA_DIR:-./data}` then renders ./data. That is the same
#     disagreement, spelled invisibly.
resolve "$REPO" "IDEALISTA_DATA_DIR="
expect_refusal "an empty environment variable over a .env value is refused" "Unset one of them"

# 11. Empty everywhere is unset.
new_repo
printf 'IDEALISTA_DATA_DIR=\n' >"${REPO}/.env"
resolve "$REPO"
expect_dir "an empty .env value is the default" "${REPO}/data"

# 12. A relative path names a different directory from every cwd.
new_repo
printf 'IDEALISTA_DATA_DIR=state\n' >"${REPO}/.env"
resolve "$REPO"
expect_refusal "a relative path is refused" "absolute path"

# 13. A directory that does not exist is refused and NOT created.
MISSING="${WORK}/missing"
printf 'IDEALISTA_DATA_DIR=%s\n' "$MISSING" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "a missing directory is refused" "not an existing directory"
[ ! -e "$MISSING" ] || fail "the missing directory was created"
pass "a missing directory is not created"

# 14. A file is not a directory.
: >"${WORK}/a-file"
printf 'IDEALISTA_DATA_DIR=%s\n' "${WORK}/a-file" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "a regular file is refused" "not an existing directory"

# 15. / would make every marker a file in the filesystem root.
printf 'IDEALISTA_DATA_DIR=/\n' >"${REPO}/.env"
resolve "$REPO"
expect_refusal "the filesystem root is refused" "must not be /"

# 16. Two lines: Compose takes the last, a reader of the first would differ.
printf 'IDEALISTA_DATA_DIR=%s\nIDEALISTA_DATA_DIR=%s\n' "$STATE" "$OTHER" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "the key twice is refused" "keep exactly one line"

# 17. Spellings Compose accepts and this reader does not interpret.
for line in \
    "export IDEALISTA_DATA_DIR=${STATE}" \
    "IDEALISTA_DATA_DIR = ${STATE}" \
    "  IDEALISTA_DATA_DIR=${STATE}"; do
    printf '%s\n' "$line" >"${REPO}/.env"
    resolve "$REPO"
    expect_refusal "refused spelling: ${line%%=*}=" "write the line as"
done
for value in "${STATE} # the state" "\${HOME}/state" "${STATE}\\x"; do
    printf 'IDEALISTA_DATA_DIR=%s\n' "$value" >"${REPO}/.env"
    resolve "$REPO"
    expect_refusal "refused value: ${value}" "must be a plain path"
done

# 18. A commented-out line is not the key.
printf '# IDEALISTA_DATA_DIR=%s\n' "$OTHER" >"${REPO}/.env"
resolve "$REPO"
expect_dir "a commented-out line is ignored" "${REPO}/data"

# 19. A longer key that merely starts the same is a different key.
printf 'IDEALISTA_DATA_DIR_OLD=%s\n' "$OTHER" >"${REPO}/.env"
resolve "$REPO"
expect_dir "a key that only shares the prefix is ignored" "${REPO}/data"

# 20. An unreadable .env cannot be ruled out, so it is not treated as silent.
if [ "$(id -u)" != "0" ]; then
    printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
    chmod 000 "${REPO}/.env"
    resolve "$REPO"
    chmod 600 "${REPO}/.env"
    expect_refusal "an unreadable .env is refused" "cannot be read"
fi

# 22. The tombstone. tools/move_data_dir.sh leaves `.moved-to` in <repo>/data
#     once the state lives elsewhere. Resolving to that directory means the
#     .env line was lost, and the directory holds no mail cursor: refused, with
#     the place the state went and both ways out.
new_repo
printf 'moved_to=%s\nmoved_at=20261112T000000Z\n' "$STATE" >"${REPO}/data/.moved-to"
resolve "$REPO"
expect_refusal "the default directory is refused once it carries the tombstone" "carries .moved-to"
case "$ERR" in
    *"${STATE}"*"--rollback"*) pass "the refusal names where the state went and how to undo it" ;;
    *) fail "the tombstone refusal does not name the moved directory and --rollback" ;;
esac

# 23. With the line in place the tombstone is exactly what is expected there.
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
resolve "$REPO"
expect_dir "the moved directory resolves while <repo>/data carries the tombstone" "$STATE"

# 24. A line that points back at the directory the state left is the same
#     mistake spelled explicitly.
printf 'IDEALISTA_DATA_DIR=%s\n' "${REPO}/data" >"${REPO}/.env"
resolve "$REPO"
expect_refusal "an explicit path to the tombstoned directory is refused" "carries .moved-to"

# 25. A tombstone that records nothing is still a tombstone.
new_repo
: >"${REPO}/data/.moved-to"
resolve "$REPO"
expect_refusal "an empty tombstone still refuses" "an unrecorded directory"

# 26. `--default` names the directory inside the checkout and reads nothing:
#     not .env, not the environment, not the tombstone. It is how the watcher's
#     deploy gate and the mover tell "still in the checkout" from "moved".
new_repo
printf 'IDEALISTA_DATA_DIR=%s\n' "$STATE" >"${REPO}/.env"
: >"${REPO}/data/.moved-to"
RC=0
OUT="$(IDEALISTA_DATA_DIR="$OTHER" "$SHELL_UNDER_TEST" "$LIB" --default "${REPO}/" 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_dir "--default is <repo>/data whatever is configured" "${REPO}/data"
RC=0
OUT="$("$SHELL_UNDER_TEST" "$LIB" --default 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_refusal "--default without a repository root is refused" "usage"

# 21. No repository root is a usage error, not `/data`.
RC=0
OUT="$(env -u IDEALISTA_DATA_DIR "$SHELL_UNDER_TEST" "$LIB" 2>"${WORK}/err")" || RC=$?
ERR="$(cat "${WORK}/err")"
expect_refusal "no repository root is refused" "usage"

printf '\nALL %d PASSED (%s %s)\n' "$passed" "$SHELL_UNDER_TEST" "$("$SHELL_UNDER_TEST" -c 'echo $BASH_VERSION')"
