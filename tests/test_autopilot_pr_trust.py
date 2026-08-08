"""Only owner-authored in-repo PRs may have their code run on this machine.

The local merge gate (issue #83) executes the pull request's own code -
local_ci.sh, conftest.py, the test files - on the owner's Mac, beside .env
and the GitHub token. On a public repository that is remote code execution
for anyone who opens a PR, which is what the first independent review of
PR #90 returned as CRITICAL.

tools/autopilot/lib/pr_is_owner_authored.sh is the decision, isolated from
GitHub so it can be tested from real metadata shapes rather than mocked
around. Exit 0 means "this is the owner's own code, running it is fine".
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRUST_CHECK = REPO_ROOT / "tools" / "autopilot" / "lib" / "pr_is_owner_authored.sh"
OWNER = "sergi039"


def _decide(metadata, *, owner=OWNER):
    """Run the real script the way merge_bot does: JSON on stdin."""
    payload = metadata if isinstance(metadata, str) else json.dumps(metadata)
    return subprocess.run(
        ["bash", str(TRUST_CHECK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "AUTOPILOT_TRUSTED_AUTHOR": owner,
        },
    )


def test_owner_branch_in_this_repo_is_trusted():
    res = _decide(
        {
            "isCrossRepository": False,
            "author": {"login": OWNER},
            "headRepositoryOwner": {"login": OWNER},
        }
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_fork_pr_is_refused():
    """An outside contributor's branch lives in their fork."""
    res = _decide(
        {
            "isCrossRepository": True,
            "author": {"login": "outside-contributor"},
            "headRepositoryOwner": {"login": "outside-contributor"},
        }
    )
    assert res.returncode != 0, "fork PRs must never run their code here"


def test_dependabot_same_repo_pr_is_refused():
    """Dependabot pushes its branches INTO this repository, so the fork check
    alone would wave it through - it carries upstream code nobody reviewed."""
    res = _decide(
        {
            "isCrossRepository": False,
            "author": {"login": "dependabot[bot]"},
            "headRepositoryOwner": {"login": OWNER},
        }
    )
    assert res.returncode != 0, "bot-authored PRs must not run their code here"


def test_owner_login_from_a_fork_is_refused():
    """Same author login, but the head branch is not in this repository."""
    res = _decide(
        {
            "isCrossRepository": True,
            "author": {"login": OWNER},
            "headRepositoryOwner": {"login": OWNER},
        }
    )
    assert res.returncode != 0


def test_missing_metadata_is_untrusted():
    """An unanswerable question is not a yes."""
    assert _decide({}).returncode != 0
    assert _decide({"author": {"login": OWNER}}).returncode != 0
    assert _decide("").returncode != 0
    assert _decide("not json at all").returncode != 0


def test_impersonation_by_case_is_refused():
    """GitHub logins are case-insensitive to humans but the comparison is not;
    refusing the mismatch is the safe direction."""
    res = _decide(
        {
            "isCrossRepository": False,
            "author": {"login": OWNER.upper()},
            "headRepositoryOwner": {"login": OWNER},
        }
    )
    assert res.returncode != 0
