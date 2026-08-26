"""Record the dossier written about one listing, so the tracker can link back.

A per-object dossier site links *into* this application -- that is where the
measurements are -- and until this existed the return path did not, so a row
with a dossier looked exactly like a row without one.

    python -m utils.set_property_dossier --id 1282 \\
        --url https://1282.cervantes50.com \\
        --title "Seiruga · Malpica de Bergantiños" --apply

`--id` on its own prints what the row currently points at and exits; that is
the only window onto a stored link, so it costs no arguments to look. With
`--url` and no `--apply` it reports what it would write and exits. `--apply`
writes, under the row's lock, through `services/dossier.record_dossier`.
`--clear` removes the pointer.

The URL is validated by the same function the property page reads it through
(`services.dossier.normalise_url`), so a link this tool accepts is a link the
page will render -- the two cannot drift apart into a value stored in the
database and refused everywhere else.
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def _describe(prop) -> str:
    from services.dossier import read_dossier

    dossier = read_dossier(prop)
    title = (getattr(prop, "title", None) or f"Property #{prop.id}")[:70]
    lines = [
        f"  id          {prop.id}",
        f"  title       {title}",
    ]
    if dossier is None:
        lines.append("  dossier     none -- nothing links back to this row")
    else:
        lines.append(f"  dossier     {dossier['url']}")
        lines.append(f"  label       {dossier['title']}")
        lines.append(f"  recorded    {dossier['recorded_at'] or 'unknown'}"
                     f" by {dossier['by'] or 'unknown'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True, help="Property id")
    parser.add_argument("--url", help="Absolute http(s) URL of the dossier")
    parser.add_argument("--title", help="Short label shown on hover")
    parser.add_argument(
        "--by", default="manual", help="Who recorded it (default: manual)"
    )
    parser.add_argument(
        "--clear", action="store_true", help="Remove the dossier link"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write. Without it, report only."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from app import create_app, db
    from models import Property
    from services.dossier import DossierError, clear_dossier, normalise_url, record_dossier

    app = create_app()
    with app.app_context():
        prop = db.session.get(Property, args.id)
        if prop is None:
            logger.error("no property with id %s", args.id)
            return 1

        logger.info("Before:\n%s", _describe(prop))

        if args.clear:
            if not args.apply:
                logger.info("\nWould clear the dossier link. Re-run with --apply.")
                return 0
            removed = clear_dossier(prop, commit=True)
            logger.info(
                "\nAfter:\n%s", _describe(db.session.get(Property, args.id))
            )
            return 0 if removed else 0

        if not args.url:
            # Looking costs no arguments; a *partial* set of them is an error
            # that names what is missing rather than quietly doing nothing.
            return 0

        if normalise_url(args.url) is None:
            logger.error(
                "\nrefused: %r is not an absolute http(s) URL with a host. "
                "The property page would not render it, so it is not stored.",
                args.url,
            )
            return 2

        if not args.apply:
            logger.info(
                "\nWould record:\n  url         %s\n  label       %s\n  by          %s"
                "\n\nRe-run with --apply to write.",
                args.url,
                args.title or "(host name)",
                args.by,
            )
            return 0

        try:
            record_dossier(
                prop, url=args.url, title=args.title, by=args.by, commit=True
            )
        except DossierError as exc:
            logger.error("\nrefused: %s", exc)
            return 2

        logger.info("\nAfter:\n%s", _describe(db.session.get(Property, args.id)))
        return 0


if __name__ == "__main__":
    sys.exit(main())
