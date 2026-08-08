#!/bin/bash
# Idempotent installer: points this clone's git hooks at the versioned
# .githooks/ directory (issue #74).
#
# Usage:
#   tools/ci/install_hooks.sh

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config core.hooksPath .githooks
chmod +x .githooks/pre-push tools/ci/local_ci.sh

echo "core.hooksPath set to .githooks (local CI gate active on git push)"
