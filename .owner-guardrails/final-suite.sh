#!/bin/bash
# The repository-owned full-suite runner the Tier 2 final gate executes.
#
# Pinned by path, mode, argv and SHA256 in `.owner-guardrails/final-gate.json`
# and in the Skills-owned registry, so the gate runs a verified private copy of
# exactly these bytes and a swapped or edited runner cannot mint evidence.
# Changing this file means re-pinning both, deliberately.
#
# It is NOT `tools/ci/local_ci.sh`: that gate is bypassable with
# `SKIP_LOCAL_CI=1` and carries a shared-`.git/config` canary meant for a
# developer's push, and a full-suite runner whose author can switch it off is
# not evidence of anything.
set -euo pipefail

if [[ $# -ne 1 || $1 != "full" ]]; then
  echo "final-suite: expected exactly: full" >&2
  exit 2
fi

# The gate executes this with a fixed minimal environment, so nothing may be
# assumed from the caller's shell. `uv` lives in the owner's ~/.local/bin and
# is the only interpreter contract this project has (`uv.lock` pins ruff and
# pytest alike, which is why CI, the pre-push hook and this runner agree).
export PATH="/Users/ss/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The same three checks `.github/workflows/ci.yml` makes required on `main`,
# in the same order, with the locked ruff rather than whatever is on PATH.
uv run ruff check .
uv run ruff format --check .
exec uv run pytest tests/ -q
