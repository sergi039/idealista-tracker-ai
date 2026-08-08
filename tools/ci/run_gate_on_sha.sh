#!/bin/bash
# Run the local CI gate against a clean snapshot of one commit.
#
#   tools/ci/run_gate_on_sha.sh <sha>
#
# Checks the SHA out into a throwaway `git worktree` and runs the snapshot's
# own tools/ci/local_ci.sh there, so the verdict reflects exactly that commit
# — never the working tree it was launched from. Shared by .githooks/pre-push
# (validates every pushed SHA) and tools/autopilot/merge_bot.sh (validates a
# PR head before merging); one primitive, one behaviour (issue #83).
#
# Exit code: local_ci.sh's own on a completed run, 1 on infrastructure
# failure (worktree could not be created, gate missing from the commit).

set -u

sha="${1:?usage: run_gate_on_sha.sh <sha>}"

# When invoked from inside a git hook, git exports repo-pinning variables
# (GIT_DIR and friends). Inherited into the snapshot they would point every
# git call there — including the test suite's own subprocesses — back at the
# OUTER repository instead of the snapshot. Drop them before entering.
for v in $(git rev-parse --local-env-vars 2>/dev/null); do
    unset "$v"
done

repo_root="$(git rev-parse --show-toplevel)" || exit 1

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/local-ci-gate.XXXXXX")" || {
    echo "run_gate_on_sha: could not create a temp dir for a snapshot of $sha" >&2
    exit 1
}
wt="${tmp_dir}/wt"

cleanup() {
    cd "$repo_root" 2>/dev/null || true
    git worktree remove --force "$wt" >/dev/null 2>&1
    rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

if ! git -C "$repo_root" worktree add --detach --quiet "$wt" "$sha" >/dev/null 2>&1; then
    echo "run_gate_on_sha: could not create a clean worktree for commit $sha" >&2
    exit 1
fi

if [ ! -x "${wt}/tools/ci/local_ci.sh" ]; then
    echo "run_gate_on_sha: commit $sha carries no tools/ci/local_ci.sh - cannot validate it" >&2
    exit 1
fi

( cd "$wt" && "${wt}/tools/ci/local_ci.sh" )
exit $?
