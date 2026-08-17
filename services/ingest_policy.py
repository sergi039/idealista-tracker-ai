"""Which machine may pull the mailbox.

`AUTO_START_SCHEDULER` decides whether a machine ingests on a cron tick, and
#376 made it fail-closed everywhere that decides a default: two machines polling
one Gmail label is not the same work done twice but two divergent databases plus
a second Google bill per listing (measured 2026-08-16: 306 listings into a dev
checkout between 07:00 and 10:00, ~$110 of credit nobody read; measured
2026-08-17: 730 rows on the laptop against 408 on the deployment).

That flag governed the *scheduler* and nothing else. `POST /api/ingest/email/run`
— the **Manual Sync** button, present in the navbar of every page, CSRF-exempt
(`app.py`), and reachable with no authentication at all (CLAUDE.md: there is no
authentication) — read the same mailbox on one click regardless of it. So a
checkout that had correctly been told it must not ingest still had a one-click
path to ingesting, and the only friction was a 5/minute rate limit.

The rule this module owns, in one sentence: **a machine that does not ingest on
its own does not ingest on request either.**

It deliberately introduces no new flag. A second flag would mean a second thing
to set, a second thing to forget, and a machine could end up declaring the two
halves of one fact differently. The question "is this the machine that ingests?"
already has an answer in the configuration; this module gives it one name and two
readers — the endpoint and the navbar — so the button is absent exactly where the
endpoint would refuse.

It also reads that answer from `app.config`, where the scheduler reads it, rather
than from the `Config` class. Those are two separate readings of the environment
taken at different moments, and a guard that consulted the other one could refuse
a manual run on a machine whose scheduler is happily running — the same
disagreement, arriving through the back door of the guard meant to prevent it.

What this does **not** close, stated plainly because a guard presented as
complete is worse than a guard known to be partial: an ad-hoc script run through
`docker exec -i idealista-app python -` constructs the service directly and never
touches Flask. That is how 326 curated rows came to exist on the laptop, and no
HTTP-layer rule can see it. The boundary here is the interface, not the process.
"""

from typing import NamedTuple


class IngestVerdict(NamedTuple):
    """Whether this machine may run an ingestion, and why not when it may not."""

    allowed: bool
    reason: str


#: Returned to the caller so the page can say which machine refused and why,
#: rather than reporting an ingestion that silently did nothing.
REASON_INGESTER = "ingester"
REASON_NOT_AN_INGESTER = "not_an_ingester"


def ingest_verdict() -> IngestVerdict:
    """Read the machine's ingester role from the same place the scheduler reads it.

    `app.config` first, and that is the whole point rather than a preference.
    There are two independent readings of this flag in the codebase, built from
    the environment at different moments: `app.py` puts one into `app.config`
    when `create_app()` runs, and `config.py` computes `Config.AUTO_START_SCHEDULER`
    when the module is imported. The scheduler asks `app.config`
    (`services/scheduler_service.py`, `app.py`'s `should_start_scheduler`), so a
    guard reading `Config` could refuse a manual run on a machine whose scheduler
    is running — the two halves of one fact disagreeing, which is exactly what
    this module exists to prevent. Four existing tests set
    `app.config["AUTO_START_SCHEDULER"]` and none set the `Config` attribute.

    Outside an application context — a `docker compose run` sibling, a CLI
    import — there is no `app.config` to ask, and `Config` is then the only
    reading available.
    """
    from flask import current_app, has_app_context

    if has_app_context():
        allowed = bool(current_app.config.get("AUTO_START_SCHEDULER", False))
    else:
        from config import Config

        allowed = bool(getattr(Config, "AUTO_START_SCHEDULER", False))

    if allowed:
        return IngestVerdict(True, REASON_INGESTER)
    return IngestVerdict(False, REASON_NOT_AN_INGESTER)


def machine_is_ingester() -> bool:
    """Jinja-friendly reading of the same verdict, for hiding the control."""
    return ingest_verdict().allowed
