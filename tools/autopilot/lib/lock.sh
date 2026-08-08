#!/bin/bash
# Shared single-instance lock for the autopilot scripts.
#
# macOS ships no flock(1), and the obvious mkdir-plus-stale-PID substitute is
# racy: two ticks that both see a stale lock can both reclaim it, the second
# one deleting the directory the first just created. Measured, not theorised —
# lock_race_test.sh produced two simultaneous winners against that design.
#
# So use the real thing: flock(2) on a file descriptor, via python3. The kernel
# releases the lock when the last descriptor on the open file description
# closes, which happens automatically when the process dies. That removes the
# entire stale-lock problem — there is no PID to check, nothing to reclaim, and
# nothing to leak when a build is killed or the machine sleeps.
#
# The descriptor is opened by the shell (fd 9) and inherited by python, so the
# lock outlives the short-lived python process and lasts exactly as long as the
# script holding fd 9.
#
# Usage:
#   source "${HERE}/lib/lock.sh"
#   autopilot_acquire_lock "/tmp/some.lock" || { echo "busy"; exit 0; }

# Descriptor 9: high enough not to collide with anything the scripts redirect.
AUTOPILOT_LOCK_FD=9

autopilot_acquire_lock() {
    local lock_file="$1"

    if ! command -v python3 >/dev/null 2>&1; then
        # Fail closed. Running two deploys or two merge passes concurrently is
        # worse than not running one.
        echo "autopilot: python3 is required for locking, refusing to run unlocked" >&2
        return 1
    fi

    eval "exec ${AUTOPILOT_LOCK_FD}>\"\$lock_file\"" || {
        echo "autopilot: cannot open lock file ${lock_file}" >&2
        return 1
    }

    python3 -c '
import fcntl
import sys

try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
' 9>&"$AUTOPILOT_LOCK_FD" 2>/dev/null
}
