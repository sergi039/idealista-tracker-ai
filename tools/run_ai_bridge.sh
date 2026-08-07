#!/bin/bash
# Launcher for the subscription AI bridge.
#
# Reads AI_BRIDGE_TOKEN (and friends) from the repo .env so the secret lives in
# exactly one place instead of being copied into a LaunchAgent plist.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ ! -f .env ]]; then
  echo "ai-bridge: .env not found in $REPO_DIR" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

# LaunchAgents start with a minimal PATH; the CLIs live in the user's dirs.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

exec /usr/bin/python3 tools/ai_bridge.py
