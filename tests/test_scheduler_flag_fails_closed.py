"""Exactly one machine may ingest, and no default may hand that role out.

`config.py` has defaulted `AUTO_START_SCHEDULER` to false outside DEV_MODE for a
long time, and it made no difference: `docker-compose.yml` set the variable in
the container environment as `${AUTO_START_SCHEDULER:-true}`, so the code never
saw an unset variable, and `docker-compose.dev.yml` forced a flat `true` that
won the Compose merge outright. A dev checkout therefore polled the same Gmail
mailbox as the deployment — which is not the same work done twice but two
divergent databases plus a second Google bill per listing (#376: 306 listings
into the laptop in one morning, roughly $110 of credit nobody read).

These tests pin the three places that decide the default. What happens once the
flag is false is pinned elsewhere (tests/test_scheduler_belongs_to_the_web_process.py);
here the claim is narrower and is exactly the one that failed: an environment
that says nothing must not produce an ingester.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# `${VAR:-default}` is the only interpolation form these files use. Rendering the
# defaults is how "an unset environment" gets asserted rather than assumed.
_VAR_WITH_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")
# A YAML environment entry, as opposed to the word appearing in a comment. The
# comments in these files name the variable on purpose, so a test that merely
# grepped for the name would pass on a file that still forces it.
_ASSIGNMENT = re.compile(r"^\s*-\s*AUTO_START_SCHEDULER\s*=\s*(.*?)\s*$", re.MULTILINE)


def _render_with_defaults(text: str) -> str:
    return _VAR_WITH_DEFAULT.sub(lambda match: match.group(2), text)


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_compose_default_does_not_create_an_ingester():
    compose = _render_with_defaults(
        (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    assignments = _ASSIGNMENT.findall(_strip_comments(compose))

    assert assignments == ["false"], (
        "docker-compose.yml must render AUTO_START_SCHEDULER=false when the "
        f"environment says nothing; rendered {assignments!r}"
    )


def test_dev_compose_does_not_force_the_flag_on():
    dev = _strip_comments(
        (_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    assignments = _ASSIGNMENT.findall(dev)

    assert assignments == [], (
        "docker-compose.dev.yml must not assign AUTO_START_SCHEDULER at all — a "
        "later file's flat value wins the Compose merge and overrides the "
        f"machine's own .env; found {assignments!r}"
    )


def test_env_example_ships_the_flag_off():
    lines = [
        line.strip()
        for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("AUTO_START_SCHEDULER=")
    ]

    assert lines == ["AUTO_START_SCHEDULER=false"], (
        "README documents `cp .env.example .env`, so this file is what a fresh "
        f"clone becomes; it must not ship an ingester. Found {lines!r}"
    )


def test_config_default_agrees_with_compose():
    # The two layers disagreeing is the actual defect this ticket is about, so
    # the agreement is worth pinning rather than trusting.
    config = (_ROOT / "config.py").read_text(encoding="utf-8")
    match = re.search(
        r"AUTO_START_SCHEDULER\s*=\s*\(\s*os\.environ\.get\(\s*"
        r'"AUTO_START_SCHEDULER"\s*,\s*(.+?)\s*\)',
        config,
        re.DOTALL,
    )

    assert match is not None, (
        "could not find the AUTO_START_SCHEDULER default in config.py"
    )
    default_expr = match.group(1)
    assert '"false"' in default_expr, (
        "config.py must keep a false default for a non-DEV_MODE process; found "
        f"{default_expr!r}"
    )
