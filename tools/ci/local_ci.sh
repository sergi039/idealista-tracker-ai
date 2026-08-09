#!/bin/bash
# Local CI gate — mirrors the checks .github/workflows/ci.yml runs on GitHub,
# so a red run is caught before it ever leaves this machine (issue #74).
#
# The repo is public, so Actions minutes are free; the real cost of a red CI
# run is an agent cycle (autopilot/AgentsRoom fix-push-wait loop). This script
# is the thing that catches it first, locally, for a few seconds of CPU.
#
# Runs standalone:
#   tools/ci/local_ci.sh
#
# Also invoked by .githooks/pre-push (bypass with SKIP_LOCAL_CI=1 git push).
#
# Steps, in order, each printed with a clear PASS/FAIL summary:
#   1. ruff check .
#   2. ruff format --check .
#   3. no-source-bundles (same globs as .github/workflows/ci.yml, issue #29)
#   4. uv run pytest tests/ -q
#
# Exits non-zero on the first failing step's class, but runs all steps first
# so the developer sees every problem in one pass instead of fixing them one
# at a time across repeated invocations.

set -uo pipefail

# Drop any inherited git environment before resolving the repository
# (defect in the gate as it shipped in PR #78).
# .githooks/pre-push already scrubs it, but this script is also
# a standalone entry point and can be invoked from other hook contexts: with
# GIT_DIR set, `git rev-parse --show-toplevel` degrades to "wherever I am"
# and `git ls-files` below would read a foreign index, so the gate would
# report on a tree it never checked. See tests/test_local_ci_hook.py.
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE \
    GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
    GIT_NAMESPACE GIT_PREFIX GIT_QUARANTINE_PATH

cd "$(git rev-parse --show-toplevel)" || exit 1

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

overall_rc=0
step_num=0

run_step() {
    local name="$1"
    shift
    step_num=$((step_num + 1))
    printf '\n%b[%d/4] %s%b\n' "$YELLOW" "$step_num" "$name" "$NC"
    if "$@"; then
        printf '%b  PASS: %s%b\n' "$GREEN" "$name" "$NC"
        return 0
    else
        printf '%b  FAIL: %s%b\n' "$RED" "$name" "$NC"
        overall_rc=1
        return 1
    fi
}

check_source_bundles() {
    local hits
    hits="$(git ls-files -- '*.zip' '*.tar.gz' '*.tgz' '*all_code.txt')"
    if [ -n "$hits" ]; then
        echo "Source bundles must never be committed (issue #29):"
        echo "$hits"
        return 1
    fi
    echo "OK: no source bundles tracked"
    return 0
}

run_pytest() {
    # tests/test_postgres_migrations.py is the only thing that executes the
    # repository's (PostgreSQL-only) migration SQL, and it skips without a
    # server. CI always has one, so say so rather than letting a green local
    # run imply the migrations were covered.
    if [ -z "${TEST_DATABASE_URL_POSTGRES:-}" ]; then
        printf '  note: TEST_DATABASE_URL_POSTGRES unset -> the PostgreSQL '
        printf 'migration tests will skip (CI runs them; see CONTRIBUTING.md)\n'
    fi
    uv run pytest tests/ -q
}

echo "== local CI gate =="

run_step "ruff check ."            uv run ruff check .
run_step "ruff format --check ."   uv run ruff format --check .
run_step "no-source-bundles"       check_source_bundles
run_step "uv run pytest tests/ -q" run_pytest

echo
if [ "$overall_rc" -eq 0 ]; then
    printf '%b== local CI gate: PASS ==%b\n' "$GREEN" "$NC"
else
    printf '%b== local CI gate: FAIL ==%b\n' "$RED" "$NC"
fi

exit "$overall_rc"
