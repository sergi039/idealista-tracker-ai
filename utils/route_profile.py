"""Route one subscription's listings into another — the safe way, by hand.

`SearchProfileService.route_profile()` is the ONE writer of
`search_profiles.routed_to`, and it is the only correct one: it locks both
rows `FOR UPDATE` in ascending id order and moves the stub's existing
listings inside the same transaction. Until this file existed it was
reachable from no CLI, no route and no template — so the only *available*
way to re-point a stub by hand was raw SQL through `docker exec`, and that
path loses data (issue #527).

WHY RAW SQL LOSES A LISTING, since the reason is not obvious and the fix is
not to be careful. A bare `UPDATE search_profiles SET routed_to = ...` takes
`FOR NO KEY UPDATE`, which does not conflict with the `FOR KEY SHARE` the
canonicalization trigger holds — so it is never blocked by an insert in
flight. Reproduced on PostgreSQL 15.18 against the deployed trigger: a
curation transaction that does everything right, re-routing the stub AND
moving its listings in one transaction, still strands a row. Its `UPDATE
properties` reported 0 rows because the listing had not committed yet, and
the listing then committed onto the old target with nothing left to collect
it. The writer cannot see what has not committed, so there is no raw-SQL
recipe that avoids this. `route_profile()`'s `FOR UPDATE` does conflict, so
an in-flight insert blocks it and the race cannot happen.

    python -m utils.route_profile --source 25 --target 24
    python -m utils.route_profile --source 25 --target 24 --apply

Without `--apply` it reports what it would do and writes nothing. It spends
no money and reaches no external service.
"""

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# `route_profile()` answers a refusal with a named reason rather than a
# guess. Each one is a defect somebody reproduced first, so the CLI says what
# it means instead of printing the identifier and leaving the reader to grep.
REFUSALS = {
    "self_route": "a subscription cannot route to itself",
    "no_such_profile": "one of those ids is not a subscription",
    "catch_all_never_routes": (
        "the catch-all may not be routed, in either direction: it receives "
        "every email that matches nothing else, so routing it would move all "
        "unmatched mail silently"
    ),
    "target_is_routed": (
        "the target is itself routed somewhere — that would be a chain. "
        "Route into the final target instead"
    ),
    "source_already_routed": (
        "the source is already routed. Re-pointing it here would leave its "
        "old target's listings behind and send future ones elsewhere — a "
        "split wearing a success message"
    ),
    "source_is_a_route_target": (
        "another subscription already routes INTO this source, which would "
        "be a chain. Re-point that route first, explicitly"
    ),
    "source_carries_a_pattern": (
        "the source carries an auto-route pattern, which belongs on a target "
        "rather than on a stub (the database CHECK would refuse it anyway)"
    ),
}


def _describe(profile) -> str:
    bits = [f"#{profile.id} {profile.name!r}"]
    if not profile.is_active:
        bits.append("archived")
    if getattr(profile, "is_hidden", False):
        bits.append("hidden")
    if profile.is_default:
        bits.append("catch-all")
    if profile.routed_to is not None:
        bits.append(f"already routed to #{profile.routed_to}")
    return " · ".join(bits)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=int, required=True, help="The stub whose listings move"
    )
    parser.add_argument(
        "--target", type=int, required=True, help="The subscription they move to"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write. Without it, report only."
    )
    args = parser.parse_args(argv)

    # Reuse a context the caller already opened rather than standing up a
    # second application over a second database. A CLI that insists on its own
    # `create_app()` cannot be driven in-process, which is how a CLI ends up
    # with no tests except one that shells out and asserts an exit code.
    from flask import current_app, has_app_context

    # `has_app_context()` alone is the wrong predicate: it is true inside ANY
    # Flask app's context, including one this project's `db` was never
    # registered with, and `_route()` would then raise instead of standing up
    # the application it needs. Ask whether THIS app is ours.
    if has_app_context() and "sqlalchemy" in current_app.extensions:
        return _route(args)

    from app import create_app

    with create_app().app_context():
        return _route(args)


def _route(args) -> int:
    from app import db
    from models import Property, SearchProfile
    from services.search_profile_service import SearchProfileService

    # The WHOLE read phase runs with autoflush off, not just the obvious
    # queries. A dry run promises to write nothing, and an ORM read flushes
    # whatever the caller left pending in the session first — including the
    # implicit SELECT that `_describe()` triggers on an attribute expired by an
    # earlier commit. Guarding `get()` and `count()` alone left exactly that
    # hole.
    with db.session.no_autoflush:
        source = db.session.get(SearchProfile, args.source)
        target = db.session.get(SearchProfile, args.target)
        if source is None or target is None:
            missing = [
                str(i)
                for i, p in ((args.source, source), (args.target, target))
                if p is None
            ]
            logger.error("No such subscription: %s", ", ".join(missing))
            return 2

        listings = (
            db.session.query(Property)
            .filter(Property.search_profile_id == source.id)
            .count()
        )
        logger.info("source: %s", _describe(source))
        logger.info("target: %s", _describe(target))
        logger.info(
            "%d listing(s) sit on the source and would move; future ones follow "
            "the route from the moment this commits.",
            listings,
        )

    if not args.apply:
        logger.info("\nNothing written. Re-run with --apply.")
        return 0

    outcome = SearchProfileService.route_profile(source.id, target.id)
    if outcome.get("status") == "ok":
        logger.info(
            "\nRouted #%d -> #%d. %d listing(s) moved.",
            source.id,
            target.id,
            outcome.get("moved", 0),
        )
        return 0

    reason = outcome.get("reason", "")
    logger.error(
        "\nRefused (%s): %s",
        reason or "unknown",
        REFUSALS.get(reason, "no explanation is recorded for this reason"),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
