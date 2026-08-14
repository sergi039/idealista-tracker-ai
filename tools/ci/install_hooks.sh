#!/bin/bash
# Idempotent installer: points this clone's git hooks at the versioned
# .githooks/ directory (issue #74).
#
# Refuses to overwrite a different pre-existing core.hooksPath: silently
# replacing it would disable whatever hooks already live there (e.g. a
# secret scanner). Clear it yourself first if the replacement is intended.
#
# Usage:
#   tools/ci/install_hooks.sh

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

current="$(git config --get core.hooksPath || true)"
if [ -n "$current" ] && [ "$current" != ".githooks" ]; then
    echo "install_hooks: core.hooksPath is already '$current' - refusing to overwrite it." >&2
    echo "install_hooks: if replacing it is intentional, run:" >&2
    echo "    git config --unset core.hooksPath && tools/ci/install_hooks.sh" >&2
    exit 1
fi

git config core.hooksPath .githooks
chmod +x .githooks/pre-push .githooks/post-merge tools/ci/local_ci.sh

echo "core.hooksPath set to .githooks (local CI gate active on git push,"
echo "container rebuilt on a merge that brings main into this clone)"
