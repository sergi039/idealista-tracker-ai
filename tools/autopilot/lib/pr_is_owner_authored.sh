#!/bin/bash
# Decide whether a pull request may be validated by running its code on THIS
# machine. Reads the PR's GitHub metadata as JSON on stdin, exits 0 for yes.
#
#   gh pr view <n> --json isCrossRepository,author,headRepositoryOwner \
#       | tools/autopilot/lib/pr_is_owner_authored.sh
#
# Why this gate exists (issue #83, review round 1 - CRITICAL):
#
# The local CI gate runs the *pull request's own* code - tools/ci/local_ci.sh,
# conftest.py, every test file - on the owner's Mac, next to .env (IMAP and
# paid API credentials), the GitHub token and SSH keys. That is safe for code
# the owner wrote and about to be merged anyway; it is remote code execution
# for anything else, and this repository is public, so anyone can open a PR.
#
# Pinning the commands instead of running the PR's script would not close it:
# running the PR's pytest IS running the PR's code. So the decision has to be
# about WHOSE code it is, not about which command is invoked.
#
# Trusted means both of:
#   - the branch lives in this repository, not a fork (isCrossRepository), and
#   - the PR author is the owner login (AUTOPILOT_TRUSTED_AUTHOR).
#
# The fork check alone is not enough: Dependabot pushes its branches into the
# repository itself, so its PRs are same-repo but carry upstream code nobody
# reviewed. The author check alone is not enough either: a fork PR can be
# opened with any head branch. Unknown or unreadable metadata is untrusted -
# an unanswerable question is not a yes.

set -uo pipefail

TRUSTED_AUTHOR="${AUTOPILOT_TRUSTED_AUTHOR:-sergi039}"

payload="$(cat)"
[ -z "$payload" ] && { echo "pr-trust: no PR metadata on stdin" >&2; exit 1; }

command -v jq >/dev/null 2>&1 || { echo "pr-trust: jq is unavailable" >&2; exit 1; }

# `.isCrossRepository // "unknown"` would be wrong: jq's `//` treats false as
# absent, so the trusted case (false) would read as "unknown" and every PR
# would be refused. Ask whether the key exists instead.
cross="$(printf '%s' "$payload" \
    | jq -r 'if has("isCrossRepository") and (.isCrossRepository | type) == "boolean"
             then (.isCrossRepository | tostring) else "unknown" end' 2>/dev/null)"
author="$(printf '%s' "$payload" | jq -r '.author.login // "unknown"' 2>/dev/null)"
[ -z "$cross" ] && cross="unknown"
[ -z "$author" ] && author="unknown"

if [ "$cross" != "false" ]; then
    echo "pr-trust: head branch is not in this repository (isCrossRepository=${cross})" >&2
    exit 1
fi

if [ "$author" != "$TRUSTED_AUTHOR" ]; then
    echo "pr-trust: author '${author}' is not the trusted owner '${TRUSTED_AUTHOR}'" >&2
    exit 1
fi

exit 0
